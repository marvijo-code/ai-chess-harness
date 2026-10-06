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
- `GET /healthz` - same as public; the pusher reads it through the tunnel after a restart.

## The pusher

Every second it reads the local viewer and posts what changed:

- state: when it changed (ignoring `state_age_s` and `server_now_ms`), at most every 1.5 s, gzipped;
- commentary: audio first, then metadata. On a (re)start it asks the relay for `last_clip_seq` and
  sends only newer clips. It never sends more than the newest 20 at once, so a restart never
  uploads the whole archive;
- thinking: for each live board's current ply when the size changed, at most every 2 s per board,
  plus the final text of the move that just ended;
- viewer version: once a minute when it changed.

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
- Files are written atomically (temporary file, then rename). After a restart the relay reloads
  everything from `data/`. If a disk write fails, state, clips metadata and thinking stay served from memory.
