# Hosting the live AI-chess viewer (relay and pusher)

The tournament runs on the laptop. The local viewer (`tools/llm_tournament_viewer.py`,
http://127.0.0.1:8770) has all the live data. These pieces copy that data to a public
site (marvijo.com/ai-chess) without opening the laptop to the internet:

```
laptop                                   VPS (already paid)                    Azure SWA
viewer :8770 <- aichess_push.py -> ssh -L 18781 -> relay ingest 127.0.0.1:8781
                                                   relay public 0.0.0.0:8780 <- function (server to server, http)
                                                                                  <- hosted page
```

No cloud resource is created and nothing costs money.

## Thinking belongs to the current tournament

Production uses the viewer and pusher on the VPS beside the runner. The public relay caches
only the current tournament's thinking. Board IDs such as `r1b2` repeat in each tournament;
they do not identify a tournament by themselves.

`thinking.meta.json` persists the tournament association. Each cached record also carries its
tournament ID. A tournament change clears the old in-memory cache and its scoped cache files.
A restart ignores legacy records without a matching association. The full source thinking
files under the runner's `out/live` directory are preserved. New uploads carry `tournament_id`;
an upload naming another tournament is rejected.

This prevents an old board's highest 40 move numbers from immediately pruning a new board's
lower move numbers. Before the fix, a live DeepSeek file kept growing while the public API
reported that no thinking existed for the current move.

## Files

| File | Role |
| --- | --- |
| `tools/aichess_relay.py` | The relay. Runs on the VPS. Python standard library only. |
| `tools/aichess_push.py` | The pusher. Runs on the laptop. Python standard library only. |
| `run-aichess-push.ps1` | Starts, stops and shows the pusher and its ssh tunnel (hidden, no console windows). |
| `tools/aichess_relay_vps/deploy.ps1` | Copies the relay to the VPS, installs the token, runs it in tmux. |
| `tools/aichess_relay_vps/run-relay.sh` | The restart loop that tmux runs on the VPS. |
| `tests/test_aichess_relay.py`, `tests/test_aichess_push.py` | Tests (no network beyond 127.0.0.1). |

## Ports

| Where | Port | Bound to | What |
| --- | --- | --- | --- |
| VPS | 8780 | 0.0.0.0 | Public, read only, GET and HEAD. |
| VPS | 8781 | 127.0.0.1 | Ingest, POST, token required. Never public. |
| Laptop | 18781 | 127.0.0.1 | ssh forward to VPS 127.0.0.1:8781. |
| Laptop | 8770 | 127.0.0.1 | The local tournament viewer (not part of this). |

## Public API (relay port 8780)

Same shapes as the local viewer, so the hosted page can reuse the viewer code.

- `GET /api/tournament` - the latest state. Gzip when the client sends `Accept-Encoding: gzip`.
  `?since=<updated_epoch_ms>` returns `{"unchanged": true, "state_age_s": .., "server_now_ms": .., "hosted": true}`
  when nothing new arrived. Patched on every response: `state_age_s` (age at push plus time since the
  relay received it), `server_now_ms` (relay clock), `hosted: true`, `relay_received_epoch_ms`.
  `pgn_path` keys are removed. Note: `since` compares `updated_epoch_ms` only. The viewer adds
  `analysis` and `annotations` between runner updates, so a client that relies on `since` alone sees
  those at the next runner update.
- `GET /api/commentary?after=N[&game=ID]` - `{"enabled": true, "clips": [...], "quiet_s": 0, "held": false}`.
  Each clip's `age_s` is its age at push plus the time since the relay received it.
- `GET /api/commentary/audio/clip-N.wav` - the WAV bytes, `Cache-Control: public, max-age=86400, immutable`.
- `GET /api/thinking?game=&ply=&since=` - same JSON and offset rules as the local viewer.
  Served from the stored tail (newest 64 KB per move); asking for older bytes returns what is kept,
  flagged `truncated`.
- `GET /api/analyze` - `{"enabled": false}`.
- `GET /api/tournaments` - `{"current": <live id>, "tournaments": [summary, ...]}`, newest first, without the live one.
  A summary is `{id, number, title, finished, champion, players, games, rounds, created_at, updated_at, updated_epoch_ms}`.
- `GET /api/tournament?id=<id>` - one earlier tournament from the archive, as it ended, with `"archived": true`
  (gzip when accepted). The live id gives the live state; an unknown id is 404.
- `GET /api/memory` - what each AI has learnt: `{"repo_url", "players": [{name, folder, text, bytes, updated_epoch_ms, notes, games, url, playing}]}`
  (`text` is the agent's `MEMORY.md`). `{"players": []}` until the pusher sent one.
- `GET /api/viewer-version` - the version the pusher sent, or `"relay"`.
- `GET /healthz` - `ok`, `state_age_s`, `state_updated_epoch_ms`, `tournament_id`, `last_clip_seq`,
  `clips`, `clip_wavs`, `disk_mb` (relay data), `disk_free_mb`.
- Anything else is 404. JSON has `Cache-Control: no-store`. POST on this port is 404.

The marvijo.com function proxies to `http://<vps>:8780` (setting `AICHESS_RELAY_URL`).

## Ingest API (relay port 8781, 127.0.0.1 only)

Every POST needs header `X-Ingest-Token` (compared with `hmac.compare_digest`). Bodies are at most 12 MB.

- `/ingest/state` - the local `/api/tournament` body, gzip or plain. Refused without `id`.
- `/ingest/clip-audio/clip-N.wav` - raw WAV bytes, at most 4 MB, must start with RIFF/WAVE.
- `/ingest/clips` - JSON list of clips as the local `/api/commentary` returned them. Idempotent by `seq`.
- `/ingest/thinking` - `{game, ply, size, text, from}`.
- `/ingest/version` - `{version}`.
- `/ingest/archive` - `{"summary": {...}, "state": {...}}`, gzip or plain: one earlier tournament. Idempotent by `state.id`
  (a second post replaces the first). Only the known summary fields are kept.
- `/ingest/memory` - the local viewer's `/api/memory` body, at most 2 MB.
- `GET /ingest/archive-index` - `{id: updated_epoch_ms}` of every archived tournament (the pusher reads it once after a start).
- `GET /healthz` - same as public; the pusher reads it through the tunnel after a restart.

## The pusher

Every second it reads the local viewer and posts what changed:

- state: when it changed (ignoring `state_age_s` and `server_now_ms`), at most every 1.5 s, gzipped;
- commentary: audio first, then metadata. On a (re)start it asks the relay for `last_clip_seq` and
  sends only newer clips. It never sends more than the newest 20 at once, so a restart never
  uploads the whole archive;
- thinking: for each live board's current ply when the size changed, at most every 2 s per board,
  plus the final text of the move that just ended;
- viewer version: once a minute when it changed;
- earlier tournaments: every 20 s it reads the local `/api/tournaments` and sends each one that is not the live one and
  that the relay does not hold yet (or holds with an older `updated_epoch_ms`), at most 2 per pass, each with its
  Stockfish scores (`eval_track`). So a tournament is in the public archive about 20 s after the next one starts;
- agent memory: once a minute it reads the local `/api/memory` and sends it when it changed.

A relay or a viewer without these paths is skipped: the live state is never held up by them.

Both ends may go down: it backs off (1 s doubling to 30 s) and never crashes. Every 2 s it writes
`<live dir>\<tournament id>-publish-status.json`:
`{"ok", "relay_up", "local_up", "last_state_push_epoch_ms", "last_clip_seq", "clips_pushed", "state_bytes_gz", "tournament_id", "updated_epoch_ms", "error"}`.
The local viewer chip reads this file.

## Start and stop

On the laptop (the tunnel and the pusher, both hidden, both self-restarting):

```powershell
.\run-aichess-push.ps1 -Start     # token file, tunnel, pusher
.\run-aichess-push.ps1 -Status    # PIDs, status file, relay health through the tunnel
.\run-aichess-push.ps1 -Stop      # stops ONLY the PIDs in out\aichess-push\*.pid and their children
```

Options: `-Vps user@host`, `-LiveDir` (default `C:\dev\chess-harness-codex\out\live`, the checkout
that runs the tournament), `-LocalUrl`, `-TunnelPort`.
Logs: `out\aichess-push\tunnel.log`, `out\aichess-push\pusher.log`.
PID files: `out\aichess-push\tunnel.pid`, `out\aichess-push\pusher.pid` (the cmd.exe loops).

The relay on the VPS:

```powershell
.\tools\aichess_relay_vps\deploy.ps1            # copy code, install token, start tmux "aclrelay" if missing
.\tools\aichess_relay_vps\deploy.ps1 -Restart   # same, and recreate ONLY tmux session "aclrelay" (new code)
.\tools\aichess_relay_vps\deploy.ps1 -Status    # session, last log lines, public /healthz from the laptop
```

On the VPS everything is in `~/acl-chess-relay/`: `aichess_relay.py`, `run-relay.sh`, `ingest.token`,
`relay.log` (rotated at 20 MB), `relay.err`, `data/`. No sudo, no nginx change, no other tmux session.

The VPS login comes from `-Vps`, else the laptop user environment variable `ROUND_RECORDER_VPS`,
else the gitignored `out\aichess-push\vps.txt` that the last run with `-Vps` saved.
No host name or address is written into the repository.

## Token handling

- The token lives in `C:\Users\<you>\.aichess-ingest-token` (64 hex characters, created with a
  cryptographic random generator when missing).
- `deploy.ps1` pipes it over ssh stdin to `~/acl-chess-relay/ingest.token` (mode 600) and prints only
  its length and a SHA-256 prefix for both copies.
- The relay refuses to start without a token file. The pusher reads the file; neither prints it.
- To rotate: delete the laptop file, run `deploy.ps1 -Restart`, then `run-aichess-push.ps1 -Stop` and `-Start`.

## Disk limits (relay)

- Clip WAVs: oldest pruned beyond 400 MB in total (about 700 clips).
- Clip metadata: the newest 5000 kept.
- Thinking: newest 64 KB per move, newest 40 moves per game, 100 MB in total.
- State: one gzipped copy (`state.json.gz`).
- Archive: one gzipped state per earlier tournament (`archive/<id>.json.gz`, about 100 KB each) plus `archive/index.json`;
  the newest 300 are kept. Agent memory: one file (`memory.json`).
- Files are written atomically (temporary file, then rename). After a restart the relay reloads
  everything from `data/`. If a disk write fails, state, clips metadata and thinking stay served from memory.

## The page: earlier tournaments, logos, livestream link, what each AI has learnt

All four live in the viewer page (`tools/llm_tournament_viewer.py`), so the local viewer and marvijo.com/ai-chess get them
together (re-export the page after any change, see `tools/aichess_export_page.py`). The stream layout (`?stream=1`) hides
the three header controls and the panel; it keeps the logos.

- **Earlier tournaments.** The header list "Tournament" has "Live now" and every earlier tournament (`/api/tournaments`).
  Picking one loads `/api/tournament?id=<id>` once and shows it as it ended: boards of the last round, tables, bracket and
  every game under Rounds, replayable move by move with the saved move marks and engine scores. No page reload; the address
  gets `?id=<id>` (a link to it works, Back and Forward work). No commentary, no auto-focus and no round shows there. On the
  hosted page the thinking text is hidden for earlier tournaments (the relay keeps it for the live one only).
  The local viewer serves any `<id>-tournament.json` in its live dir this way; nothing is sent to the engine.
- **Maker logos** (`tools/aichess_logos.py`, one hidden SVG sprite). A logo sits in front of every player name. The maker
  comes from the player's name and model id (`MAKER_RULES` in the page), never from the route: DeepSeek on another
  company's plan is still DeepSeek's model. A name that matches no rule gets no logo.
- **Livestream link.** `live_url` in `/api/tournament` (viewer `--live-url`, env `AICHESS_LIVE_URL`, `""` = no link).
- **What each AI has learnt.** A collapsed panel under the boards, one collapsible row per model family with its
  `MEMORY.md` (`/api/memory`). The viewer reads the agent memory repository: `--memory-repo` (env `AICHESS_MEMORY_REPO`),
  else `forever.memoryRepo` in the tournament state; the public link comes from `--memory-url` (env `AICHESS_MEMORY_URL`),
  else from `forever.memoryRemote`.
