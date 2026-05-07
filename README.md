# AI Chess Harness

UCI chess engines and harness scripts for testing LLM-backed chess play.

## Engines

- `engines/codex-chess`: a UCI engine that asks the local Codex app-server for legal moves.
- `engines/codex-chess-learner`: a separate UCI launcher for the learner engine. It initially runs the same implementation as Codex-chess, but has its own `MEMORY.md` and `skills/` folder for learner-specific durable context.
- `engines/llm-chess-engine`: a UCI engine that calls OpenRouter Chat Completions. It defaults to `moonshotai/kimi-k2.6` and can be changed with `OPENROUTER_MODEL` or the UCI `Model` option.

Both AI engines repair the first two model-produced illegal moves in a game by falling back to a legal move. On the third model-produced illegal move in the same game, the engine returns `bestmove 0000` so the tournament runner can adjudicate the engine as losing instead of silently continuing.

## Setup

```powershell
python -m pip install -r requirements.txt
```

For `llm-chess-engine`, set the API key in the environment. Do not commit keys.

```powershell
$env:OPENROUTER_API_KEY = "..."
$env:OPENROUTER_MODEL = "moonshotai/kimi-k2.6"
```

## UCI commands

Codex-chess:

```powershell
.\engines\codex-chess\codex-chess.cmd
```

Codex-chess-learner:

```powershell
.\engines\codex-chess-learner\codex-chess-learner.cmd
```

Codex-chess exposes configurable UCI options:

- `UseMemory`: tell the engine to use its engine-local `MEMORY.md`.
- `UseSkills`: tell the engine to use Agent Skills from its engine-local `skills/` folder.
- `LearningMode`: tell the engine to update `MEMORY.md` or create/update skills when reusable chess-learning improvements are found.

The learner launcher defaults all three to `true`; the baseline launcher defaults them to `false`.

OpenRouter LLM engine:

```powershell
.\engines\llm-chess-engine\llm-chess-engine.cmd
```

## Match harness

Run a short LLM-vs-Stockfish match:

```powershell
$env:OPENROUTER_MODEL = "moonshotai/kimi-k2.6"
python .\tools\play_engine_match.py --max-plies 8
```

Outputs are written under `out/`, which is intentionally ignored.

## FastChess Codex-vs-Codex run

Install FastChess into a repo-local ignored cache and run ten unattended games between two identical Codex-chess instances. The second instance is named `Codex-chess-learner` in the tournament output. The default run has no `-maxmoves` adjudication cap and uses a five-minute time control (`300+0`), which FastChess writes to config JSON as `time: 300000`.

```powershell
.\install-and-run-fastchess-codex.ps1
```

By default the script sets both engine processes to `CODEX_CHESS_MODEL=gpt-5.3-codex-spark` and `CODEX_CHESS_EFFORT=low`. This is an environment override for the tournament run only; it does not modify the installed Codex app or CLI configuration. Results are written to `out\fastchess`.

The script prints the exact PGN path before the match starts. FastChess writes it under `out\fastchess\codex-vs-codex-learner-<timestamp>.pgn`; the file is created during the run and populated as games finish. Matching FastChess config and log files are written beside it.

Codex engines emit a neutral `info depth 0 score cp 0 nodes 0 time 0` line before `bestmove` so FastChess can parse a score field for reports. Without that normal `info ... score ...` line, FastChess prints `Warning; Last info string with score not found...`.

For a short smoke run, add a move cap explicitly:

```powershell
.\install-and-run-fastchess-codex.ps1 -Games 2 -MaxMoves 2
```

## en-croissant registration

Use the helper to upsert local engine entries in en-croissant without storing credentials in the repo:

```powershell
Get-Process en-croissant -ErrorAction SilentlyContinue | Stop-Process
python .\tools\register_encroissant_engines.py
```

The helper updates `%APPDATA%\org.encroissant.app\engines\engines.json` and writes a timestamped backup beside it. Close en-croissant before running it; the app can otherwise rewrite the file from its stale in-memory engine list.

en-croissant supports UCI engines and engine-vs-engine games through its Play Chess setup. Current client behavior is one match at a time: an engine-vs-engine game proceeds without human moves until game over or abort, but the client does not expose an auto-rematch loop for endless consecutive games.
