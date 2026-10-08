# Non-stop AI chess on the VPS (forever tournament)

The VPS plays one AI chess tournament after another, with no laptop involved:
round robin, then semifinals and a final (the same format as the laptop tournaments), then a
short pause, then the next numbered tournament. Every AI player keeps its own memory, and
Stockfish 19 climbs a depth ladder whenever an AI beats it.

```
tools/ai_chess_forever.py  -- runs tournaments forever, writes out/live/<slug>-tournament.json
        |                      and the pointer out/live/current.json {"state_path", "id", "number"}
        |-- engines/llm-chess-engine (one process per AI player, subscription routes only)
        |-- Stockfish 19 (Linux binary, depth from the ladder)
        `-- memory repo /home/marvijo/ai-chess-agent-memory  -- git push after every game
tools/llm_tournament_viewer.py --follow out/live/current.json  (port 8770, follows each new tournament)
tools/aichess_push.py  -- reads the viewer, so it follows the new tournament too
```

## Files

| File | Role |
| --- | --- |
| `tools/ai_chess_forever.py` | The forever loop, game hooks (memory context, ladder, reflection), `--preflight`, `--segment`. |
| `tools/ai_chess_memory.py` | Memory repo layout, edit validation, ladder, standings and cache stats files, background git push. |
| `tools/play_llm_swiss.py` | Tournament engine (pairings, knockouts, clocks). Now with game hooks, notes, usage and limit waits. |
| `engines/llm-chess-engine/subscription_providers.py` | Subscription routes, cache-friendly prompts, per-call usage, limit waits, Linux CLI lookup. |
| `configs/ai-chess-vps.json` | Roster, time control and the `forever` settings. |
| `run-ai-chess-forever.sh` | Linux launcher: runner and viewer, logs and PID files in `out/live`. |
| `tests/test_ai_chess_forever.py` | Ladder, memory edits, prompt order, usage, limit waits, pointer follow, the loop. |

## Roster (configs/ai-chess-vps.json)

| Player | Route | Notes |
| --- | --- | --- |
| Sonnet 5.5 | `claude` CLI, `claude-sonnet-5-5`, effort high | Claude subscription |
| GPT-6.1 Sol | `codex` CLI, `gpt-6.1-sol`, effort high | ChatGPT subscription |
| DeepSeek V4.1 Flash | `opencode-go`, `deepseek-v4.1-flash` | OpenCode Go subscription |
| GLM 5.3 Flash | `opencode-go`, `glm-5.3-flash` | The Z.ai Coding Plan route (`zai`) answered HTTP 429 code 1113 (insufficient balance) on 2026-10-08 |
| MiMo V2.6 Pro | `opencode-go`, `mimo-v2.6-pro` | Stand-in for Gemini 3.8 Flash |
| Gemini 3.8 Flash | `enabled: false` | Waits for a Gemini CLI subscription route |
| Stockfish 19 | Linux binary, ladder from depth 4 | `Threads 1`, `Hash 16` |

No player uses `openrouter-chat` or any other metered route. Time control 10 min + 10 s,
3 tries per move, `showLegalMoves: true`, no board image (the FEN and the diagram carry the
position; an image would be uncached input on every move). No fallback move ever: a failed
move after 3 tries is a forfeit, as before.

## Tournament loop and pointer

- Tournament #N gets the slug `aichess-NNNN-<YYYYmmdd-HHMMSS>` and the title
  `Chess Tournament but with AI Players #N`. Elo starts at 1500 in every tournament.
- When it finishes, the runner waits `forever.pauseSeconds` (120 s) and starts #N+1.
- `out/live/current.json` always names the live state: `{"state_path", "id", "number", "title", "updated_epoch_ms"}`.
  It is written when a tournament starts. During the pause it still names the finished one, so the
  viewer keeps showing the final standings.
- After a restart (crash, reboot, `restart`), the runner resumes the tournament the pointer names
  (unfinished games start again from move 1, the existing `--resume` rule).
- The tournament number comes from `tournaments/index.json` in the memory repo (and is never lower
  than the pointer's number + 1).

## Viewer follow

`python3 tools/llm_tournament_viewer.py --port 8770 --follow out/live/current.json` reads the pointer
on every request (cached by file time), so `/api/tournament`, `/api/thinking` and `/api/analyze`
switch to the new tournament as soon as the pointer changes. With `--commentary` the viewer also
stops the old commentator and starts one for the new state. `tools/aichess_push.py` already resets
its bookkeeping when the tournament id changes (state fingerprint, thinking offsets, clip sequence).

Known gap for the stream/relay owner: the relay (`tools/aichess_relay.py`) stores clips by `seq`
only. A new tournament's commentator starts at seq 1 again, so the relay drops the new clips as
duplicates until its stored seqs are passed. It needs a tournament-aware clip store or a reset on id change.

## Stockfish ladder

- The ladder player is `forever.ladderPlayer` ("Stockfish 19"); it starts at `forever.ladderStartDepth` (4).
- Before every game the runner sets Stockfish's depth from the ladder and stores it in the game
  (`stockfish_depth`). After every game, an AI win against Stockfish raises the depth by 1 for every
  LATER game. Draws and Stockfish wins change nothing.
- `ladder.json` in the memory repo holds `depth`, `start_depth` and every step:
  `{"from", "to", "tournament", "number", "game", "winner", "color", "played_at_depth", "termination", "at"}`.
  It carries across tournaments and restarts. The state has `ladder` for the viewer, which shows
  "Stockfish 19 · depth N" next to the name.

## Agent memory (github.com/marvijo-code/ai-chess-agent-memory)

```
agents/<slug>/MEMORY.md                    index + key lessons, at most 6144 bytes
agents/<slug>/notes/<topic>.md             optional, at most 4096 bytes each, at most 8 files
agents/<slug>/games/<tournament>-<game>.md result, my notes, memory changes, input tokens, PGN
ladder.json
tournaments/index.md (+ index.json)        every tournament, number and champion
tournaments/<slug>.md                      round robin table and games
tournaments/cache-stats.md (+ .json)       input cache hit rate per player, this tournament and all
```

- At the start of each game the runner writes a context file (`out/live/contexts/<id>-<game>-<side>.json`
  with the player's MEMORY.md and the game header) and sends `setoption name GameContextFile`. The
  engine reads it once: the memory is frozen for the game and sent with every move.
- Move replies may carry `"note"` (at most 200 characters). Notes are stored per game
  (`games[id].notes`), the latest per player is `latest_notes` in the state, and they go into the game file.
- After every game, one reflection call per AI player on its own route (fresh request, same model
  and effort) gets the PGN, its notes, the result and the viewer's Stockfish marks (?? ? ?! !) when
  the viewer analysed the game (waits up to `forever.marksWaitSeconds`). It returns
  `{"summary", "edits": [{"path", "content"} | {"path", "delete": true}]}`. Edits are validated:
  only `MEMORY.md` and `notes/<lowercase-topic>.md`, no other path, size caps enforced by rejecting
  (never truncating), at most 6 edits, at most 8 note files, em and en dashes turned into " - ".
- Then the runner commits and pushes in the background (one commit per finished game, plain message,
  no attribution). A failed push retries from 30 s doubling to 10 min and never blocks a game.
- Stockfish has no memory.
- First start: the runner clones `forever.memoryRemote` (`git@github-aichess:marvijo-code/ai-chess-agent-memory.git`)
  into `forever.memoryRepo` when it is missing, and sets a repo-local commit identity when none exists.

## Input cache

Request order for every move: static rules (`RULES_TEXT`, byte-identical for every player, game
and move) + the frozen MEMORY.md + the game header and the moves so far (append-only) + the
position block (FEN, diagram, legal moves, clocks) at the very end. Nothing that changes from move
to move comes before the position block.

One provider "session" per game, so each move only adds what changed:

| Route | How the game continues | Why |
| --- | --- | --- |
| codex | `codex exec` (not ephemeral) for the first move, then `codex exec resume <thread id>` | Measured: fresh `codex exec --ephemeral` calls only reuse the 6,912-token Codex base prompt (61%); resume reaches 91 to 95%. |
| claude | One fresh `claude -p` per move with stream-json content blocks: first turn, then every later turn and reply as fixed blocks, the new turn last with the only cache breakpoint | Measured: `claude -p --resume` wrote the whole prompt again on every chess move (0 reads); a fresh request with a breakpoint on its last block reads everything before it. |
| opencode-go, zai | Client-side message list (system, first turn, reply, next turn ...), reasoning never resent | Provider prefix caching; the earlier messages are byte-identical. |

Cached tokens are read from every response: claude `cache_read_input_tokens` (input = uncached +
cache writes + cache reads), codex `cached_input_tokens` (a resumed session reports totals; the
engine stores the difference), OpenAI-compatible `prompt_tokens_details.cached_tokens` or DeepSeek
`prompt_cache_hit_tokens`. Every move stores `usage {calls, input, cached, output}`; the state has
`cache_stats` per player (`hit_rate` and `warm_hit_rate`, the latter without each game's first 3
moves of the player); the memory repo has `tournaments/cache-stats.md`. Session files of the CLI
routes are deleted when the game ends.

## Usage limits

`limitWait: true` in the config. A usage or rate limit (HTTP 429, "usage limit", "rate limit",
"hit your limit", quota, the Claude "usage limit reached|<epoch>" reset time, Z.ai 1113, a lost
login) makes the engine wait and send the SAME request again: 60 s, doubling to 15 min, or until
the reset time when the CLI states it. The engine prints `info string limitwait <s> <reason>`; the
runner moves its own deadline out, the player bar shows "waiting for usage limit", and the wait is
never charged to the chess clock. No forfeit, no fallback move. Real model errors (bad JSON, illegal
moves, timeouts while thinking) keep the existing 3-tries forfeit rule.

## Commands (on the VPS)

```bash
cd /home/marvijo/chess-harness-codex          # or wherever the branch is checked out
./run-ai-chess-forever.sh start               # runner + viewer in the background
./run-ai-chess-forever.sh status              # PIDs, pointer, last log lines
./run-ai-chess-forever.sh stop                # stops only the process groups in out/live/forever-*.pid
./run-ai-chess-forever.sh restart
```

For systemd (Type=simple, one unit each; both run in the foreground and inherit nothing special):

```
ExecStart=/home/marvijo/chess-harness-codex/run-ai-chess-forever.sh run-runner
ExecStart=/home/marvijo/chess-harness-codex/run-ai-chess-forever.sh run-viewer
WorkingDirectory=/home/marvijo/chess-harness-codex
User=marvijo
Restart=always
RestartSec=10
KillMode=control-group
```

Environment the launcher understands: `AI_CHESS_CONFIG`, `AI_CHESS_ENV_FILE` (default
`~/.config/ai-chess/env`, `KEY=VALUE` lines, mode 600, holds `OPENCODE_GO_API_KEY` and
`ZAI_API_KEY`), `AI_CHESS_VIEWER_HOST` (127.0.0.1), `AI_CHESS_VIEWER_PORT` (8770),
`AI_CHESS_ENGINE` (Stockfish for the viewer's eval bar and move marks, default
`~/sf19/stockfish/stockfish-linux-x86-64-universal`), `AI_CHESS_COMMENTARY=1`, `PYTHON`.
The launcher puts `~/.local/bin` (claude) and `/usr/bin` (codex) first on PATH.

Logs: `out/live/forever-runner.log`, `out/live/forever-viewer.log` (rotated at 50 MB),
engine logs in `out/llm-chess-engine-logs/`. PID files: `out/live/forever-runner.pid`,
`out/live/forever-viewer.pid` (each is a process group leader; stop checks the command line first).

Manual checks without the loop:

```bash
python3 tools/ai_chess_forever.py --preflight --memory-repo /tmp/mem --no-push --live-dir /tmp/live
python3 tools/ai_chess_forever.py --segment "Sonnet 5.5,GPT-6.1 Sol" --plies 16 --memory-repo /tmp/mem --no-push --live-dir /tmp/live
```

`--memory-repo` with a local path never clones or pushes the public repo; `--no-push` only commits.
