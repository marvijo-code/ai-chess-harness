# AI Chess Harness

UCI chess engines and harness scripts for testing LLM-backed chess play.

## Engines

- `engines/codex-chess`: a UCI engine that asks the local Codex app-server for legal moves.
- `engines/codex-chess-learner`: a separate UCI launcher for the learner engine. It initially runs the same implementation as Codex-chess, but has its own `MEMORY.md` and `skills/` folder for learner-specific durable context.
- `engines/llm-chess-engine`: a UCI engine that calls OpenRouter Chat Completions. It defaults to `moonshotai/kimi-k2.6` and can be changed with `OPENROUTER_MODEL` or the UCI `Model` option.

Codex-chess does not choose fallback legal moves for the model. Empty responses, non-JSON responses, and moves outside `legal_moves` count as consecutive invalid model responses; after three consecutive invalid responses in a game, the engine returns `bestmove 0000` so the tournament runner adjudicates that engine as losing instead of silently continuing. Codex app-server turn failures, such as usage limits, are logged separately and forfeit immediately because retrying the same unavailable model does not test chess strength.

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

Run Codex App-server against Stockfish without paid inference API calls:

```powershell
python .\tools\play_codex_vs_stockfish.py --max-plies 8
```

That runner writes timestamped final PGN/JSON/PNG files under `out\` and also keeps a live-updating PGN at `out\live\codex-vs-stockfish-live.pgn`. The live PGN is rewritten before the game starts, after every ply, and again when the final result is known, so local PGN followers such as Lichess Broadcaster can watch `out\live`.

Follow that PGN locally with no Lichess account or external upload:

```powershell
python .\tools\live_pgn_viewer.py
```

Then open `http://127.0.0.1:8765/`. The viewer polls `out\live\codex-vs-stockfish-live.pgn`, renders the current board and move list, and stays entirely on localhost. To watch a different PGN:

```powershell
python .\tools\live_pgn_viewer.py --pgn .\out\live\some-other-live.pgn --port 8766
```

The local viewer opens in dark theme by default. The board uses black/white pieces on grey/white squares, and the top toolbar can toggle light/dark theme, Stockfish analysis, and Follow Live mode. The left and right arrow keys, or the arrow buttons in the toolbar, move backward and forward through the PGN and automatically switch Follow Live off so the board stays on the selected move.

Stockfish analysis is on by default in the local viewer. It reads the enabled Stockfish entry from `%APPDATA%\org.encroissant.app\engines\engines.json`, analyzes the current PGN position locally, and shows the top PV lines without any Lichess account or external upload. This analysis is for the human viewer only; Codex-chess and Codex-chess-learner never receive Stockfish PVs or scores in their move-selection prompts. Tune or disable it at startup with:

```powershell
python .\tools\live_pgn_viewer.py --analysis-movetime-ms 500 --analysis-multipv 4
python .\tools\live_pgn_viewer.py --no-analysis
```

The viewer also exposes local maintenance panels for:

- editing `%APPDATA%\org.encroissant.app\engines\engines.json` through structured controls by default, with raw JSON shown only when the Raw JSON toggle is enabled, and with a timestamped backup written before each save;
- completed-game stats from `out\**\*.pgn`, sorted by most points per engine;
- optional date filters for those stats, with no filter applied by default.

## FastChess Codex-vs-Codex run

Install FastChess into a repo-local ignored cache and run ten unattended games between two identical Codex-chess instances. The second instance is named `Codex-chess-learner` in the tournament output. The default run has no `-maxmoves` adjudication cap and uses a five-minute time control (`300+0`), which FastChess writes to config JSON as `time: 300000`.

```powershell
.\install-and-run-fastchess-codex.ps1
```

By default the script sets both engine processes to `CODEX_CHESS_MODEL=gpt-5.5` and `CODEX_CHESS_EFFORT=low`. This is an environment override for the tournament run only; it does not modify the installed Codex app or CLI configuration. The script preflights the selected Codex model before starting FastChess, so a usage-limit or unavailable-model error fails before a long match can produce zero-ply forfeits. Results are written to `out\fastchess`.

The script prints the exact PGN path before the match starts. FastChess writes it under `out\fastchess\codex-vs-codex-learner-<timestamp>.pgn`; the file is created during the run and populated as games finish. Matching FastChess config and log files are written beside it.

To start the local viewer before FastChess and open it against the exact PGN path for that run:

```powershell
.\watch-fastchess-live-match.ps1
```

For the short command form, use `play-games.ps1`; `-n` is an alias for `-Games`:

```powershell
.\play-games.ps1 -n 100
```

The FastChess live wrapper uses the single dedicated local viewer URL `http://127.0.0.1:8766/`. If an older live-viewer process is already on that port, the wrapper restarts that viewer on the same port instead of moving to another URL. The wrapper does not expose a port selector. While FastChess is still inside a game, the wrapper starts `tools\mirror_fastchess_live_pgn.py` and points the viewer at `out\live\<run>-live.pgn` so the board has a current position before FastChess writes the final `-pgnout` game.

The viewer uses a three-column desktop layout: the left pane shows fixed-height bot thinking logs and Engine Analysis, the center pane shows the board, and the right pane keeps the leaderboard, previous matches, moves, and config. In Follow Live mode the thinking pane shows recent live entries with the current move number. During replay navigation, the thinking pane follows the selected ply and shows the prompt/comment/bestmove entries that match the move being replayed, but it does not auto-refresh while Follow Live is off so the text stays copyable. Completed games show the winner as `<player> (<colour>) won`, for example `Codex-chess-learner (White) won`. Previous matches appear below the leaderboard, paginate 5 completed games per page, and each row can be clicked to load that archived game in the board viewer with matching bot logs. Each active live or archived game shows the tournament slug derived from the PGN filename, the browser URL hash mirrors that slug, and the header copy icon copies the full absolute PGN path. The board shows the black-side player above the board and the white-side player below the board, matching the normal board orientation. Engine Analysis stays switchable from the top toolbar and the analysis panel; if the server was started with analysis disabled, the UI controls show that disabled state instead of claiming analysis is on.

For a short viewer smoke run, pass the same match options through the wrapper:

```powershell
.\watch-fastchess-live-match.ps1 -Games 2 -MaxMoves 2
```

FastChess `-pgnout` is not a current-game live feed. In FastChess v1.8.0-alpha, `-pgnout` is written after each game finishes, and `-autosaveinterval` saves tournament state every N games. The wrapper handles the mirror automatically; to do the same manually for an already-running FastChess match, mirror the current game from the engine logs into `out\live` and point the viewer at that mirror PGN:

```powershell
python .\tools\mirror_fastchess_live_pgn.py `
  --fastchess-stdout .\out\fastchess\<run>-launch.out.log `
  --engine-log-dir .\out\codex-chess-logs `
  --output .\out\live\<run>-live.pgn

python .\tools\live_pgn_viewer.py --pgn .\out\live\<run>-live.pgn --port 8766
```

The mirror also writes `WhiteClockMs`, `BlackClockMs`, `ClockUpdatedAtEpochMs`, and `ClockRunningSide` headers from the latest UCI `go wtime ... btime ...` line, so the viewer can show live running clocks above and below the board.

The viewer has a `Learner` screen on the top toolbar and also mirrors the latest bot log rows into the board view's left pane. It reads the learner's real `MEMORY.md`, `skills/`, `knowledgebase/`, and recent `out\codex-chess-logs` entries. Bot log rows are colored by source: learner and baseline entries are separated by their active engine context, and engine processes log observable prompt snapshots, repetition-risk counts, invalid-move repairs, bestmove lines, and returned short move comments without claiming hidden chain-of-thought access.

Learner improvement is handled by `tools\update_learner_knowledgebase.py`. It reads the FastChess PGN plus the redirected launch stdout, writes `engines\codex-chess-learner\knowledgebase\live-match-lessons.md`, writes a JSON copy, and refreshes the learner `MEMORY.md` autolearn block. Run it once or as a watcher:

```powershell
python .\tools\update_learner_knowledgebase.py `
  --pgn .\out\fastchess\<run>.pgn `
  --stdout .\out\fastchess\<run>-launch.out.log `
  --watch
```

## FEN interpretation curriculum

Use the FEN curriculum when the learner needs to improve raw position interpretation before or between match runs. It generates 50 deterministic hidden-answer multiple-choice tests covering square occupancy, side to move, check state, material and total piece counts, king locations, castling rights, en-passant state, promotion syntax, and legal-move recognition.

The model is prompted through local Codex app-server auth with explicit no-online-search and no-tool-use rules. Answers are graded locally with `python-chess`; after each cycle, missed-question lessons are written to `engines\codex-chess-learner\knowledgebase\fen-curriculum-lessons.md`, a JSON result is written beside it, and the learner `MEMORY.md` gets a concise FEN curriculum summary. The model itself is not given the hidden answers before answering.

Run the intended GPT-5.3 Codex curriculum:

```powershell
python .\tools\run_fen_curriculum.py --model gpt-5.3-codex --effort medium --max-cycles 4
```

Run the offline validator without Codex app-server:

```powershell
python .\tools\run_fen_curriculum.py --offline-validate --no-write-memory --json .\out\fen-curriculum-offline.json --markdown .\out\fen-curriculum-offline.md
```

Use `tools\play_codex_vs_stockfish.py` when a native move-by-move live PGN is required without FastChess log mirroring.

Codex engines emit a neutral `info depth 0 score cp 0 nodes 0 time 0` line before `bestmove` so FastChess can parse a score field for reports. Without that normal `info ... score ...` line, FastChess prints `Warning; Last info string with score not found...`.

FastChess PGN headers often say `Termination "normal"` even when the console gives the useful reason, such as `Draw by 3-fold repetition`, `Black mates`, or `White makes an illegal move`. When reviewing completed matches, summarize reasons from the PGN plus the redirected launch stdout:

```powershell
python .\tools\summarize_fastchess_reasons.py `
  --pgn .\out\fastchess\<run>.pgn `
  --stdout .\out\fastchess\<run>-launch.out.log `
  --json .\out\fastchess\<run>-reasons.json
```

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
