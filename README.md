# AI Chess Harness

UCI chess engines and harness scripts for testing LLM-backed chess play.

## Engines

- `engines/codex-chess`: a UCI engine that asks the local Codex app-server for legal moves.
- `engines/codex-chess-learner`: a separate UCI launcher for the learner engine. It initially runs the same implementation as Codex-chess, but has its own `MEMORY.md` and `skills/` folder for learner-specific durable context.
- `engines/codex-chess-zero`: a separate fast first-principles learner. It uses the same UCI implementation in Zero mode, keeps its own `MEMORY.md` and `knowledgebase/`, uses no skills by default, and learns only from compact post-game feedback plus current-position prompts.
- `engines/llm-chess-engine`: a UCI engine that calls OpenRouter Chat Completions. It defaults to `moonshotai/kimi-k2.6` and can be changed with `OPENROUTER_MODEL` or the UCI `Model` option.

Codex-chess does not choose fallback legal moves for the model. Empty responses, non-JSON responses, and moves outside `legal_moves` count as consecutive invalid model responses; after three consecutive invalid responses in a game, the engine returns `bestmove 0000` so the tournament runner adjudicates that engine as losing instead of silently continuing. If the GUI clock says the side to move has no time left, Codex-chess forfeits immediately without starting a new Codex app-server turn; OpenRouter-backed models do the same without sending an API request. Codex app-server turn failures, such as usage limits, are logged separately and forfeit immediately because retrying the same unavailable model does not test chess strength.

Codex UCI output is normalized to ASCII before it is written to the GUI/FastChess pipe. Optional model comments are non-fatal: a smart quote or other Unicode text in an `info string` must not block the actual `bestmove` line. Move selection also uses the clock-aware timeout settings in `chess-harness.config.json`; the default FastChess run remains 5+0, normal mid-clock moves can wait long enough for the local app-server to answer, and timeout/invalid retries switch to low-effort urgent context-free prompts with empty comments instead of repeating full learner context. Learner prompts use lean embedded memory/knowledgebase context for 5-minute games so routine moves do not spend tens of seconds on oversized context.

## Setup

```powershell
python -m pip install -r requirements.txt
```

For `llm-chess-engine`, set the API key in the environment. Do not commit keys.

```powershell
$env:OPENROUTER_API_KEY = "..."
$env:OPENROUTER_MODEL = "moonshotai/kimi-k2.6"
```

Search available OpenRouter models without editing engine code:

```powershell
python .\tools\search_openrouter_models.py grok 4.3
python .\tools\search_openrouter_models.py grok 4.3 --first-id
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

Codex-chess-zero:

```powershell
.\engines\codex-chess-zero\codex-chess-zero.cmd
```

Codex-chess exposes configurable UCI options:

- `UseMemory`: tell the engine to use its engine-local `MEMORY.md`.
- `UseSkills`: tell the engine to use Agent Skills from its engine-local `skills/` folder.
- `LearningMode`: tell the engine to update `MEMORY.md` or create/update skills when reusable chess-learning improvements are found.
- `ZeroMode`: use the fast Zero prompt profile, which keeps context lean, avoids memorized openings, and selects from current FEN, legal moves, clocks, and material-safety feedback.

The learner launcher defaults `UseMemory`, `UseSkills`, and `LearningMode` to `true`; the Zero launcher defaults `UseMemory` and `LearningMode` to `true`, `UseSkills` to `false`, and `ZeroMode` to `true`; the baseline launcher defaults learning options to `false`.

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

Run an OpenRouter model directly against the Codex learner UCI engine:

```powershell
python .\tools\play_engine_match.py `
  --openrouter-model x-ai/grok-4.3 `
  --codex-learner-black `
  --black-movetime-ms 30000 `
  --max-plies 8
```

`play_engine_match.py` accepts `--white-option NAME=VALUE`, `--black-option NAME=VALUE`, `--white-env NAME=VALUE`, and `--black-env NAME=VALUE` for one-off UCI and environment overrides. `llm-chess-engine` exposes a `Model` UCI option and also reads `OPENROUTER_MODEL`. Missing OpenRouter keys, API failures, invalid JSON, and illegal model moves forfeit with `bestmove 0000`; the engine does not pick a heuristic fallback move.

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
- completed-game stats from active `out\**\*.pgn` files, sorted by most points per engine, excluding live feeds and `out\backups` archives;
- optional date filters for those stats, with no filter applied by default.

To reset the active leaderboard before a new run, back up generated game artifacts first:

```powershell
.\backup-all-games.ps1
```

The script moves active artifacts from `out\fastchess`, `out\live`, `out\codex-chess-logs`, `out\llm-chess-engine-logs`, and root-level game PGN companions into `out\backups\<timestamp>`, writes a `manifest.json`, recreates the active output folders, and leaves learner memory, learner skills, learner knowledgebase, validation output, and existing backups untouched. Use `-DryRun` to print the planned backup without moving files.

## FastChess Codex-vs-Codex run

Install FastChess into a repo-local ignored cache and run ten unattended games between two Codex-chess instances. The second instance is named `Codex-chess-learner` by default, or `Codex-chess-zero` when `-LearningEngine zero` is selected. The default run has no `-maxmoves` adjudication cap and uses a five-minute time control (`300+0`), which FastChess writes to config JSON as `time: 300000`.

Runner defaults live in `chess-harness.config.json`. Use that file for normal defaults such as Codex model, effort, Codex move-time budgets, learner context thresholds, FastChess game count, FastChess `concurrency`, time control, viewer port, analysis settings, learner autolearn, and preflight behavior. CLI arguments still work as one-off overrides for a run.

```powershell
.\install-and-run-fastchess-codex.ps1
```

Run the same harness against Zero instead of the learner:

```powershell
.\play-games.ps1 -LearningEngine zero -n 20
```

With the checked-in config, the script sets both engine processes to `CODEX_CHESS_MODEL=gpt-5.3-codex` and `CODEX_CHESS_EFFORT=high`. This is an environment override for the tournament run only; it does not modify the installed Codex app or CLI configuration. The script preflights the selected Codex model before starting FastChess, so a usage-limit or unavailable-model error fails before a long match can produce zero-ply forfeits. Results are written to `out\fastchess`.

The script prints the exact PGN path before the match starts. FastChess writes it under `out\fastchess\codex-vs-codex-learner-<timestamp>.pgn` by default, or the configured Zero run name when `-LearningEngine zero` is selected; the file is created during the run and populated as games finish. Matching FastChess config and log files are written beside it.

To start the local viewer before FastChess and open it against the exact PGN path for that run:

```powershell
.\watch-fastchess-live-match.ps1
```

For the short command form, use `play-games.ps1`; `-n` is an alias for `-Games`:

```powershell
.\play-games.ps1 -n 100
```

Use FastChess wording for parallel games. `-Concurrency` is the public launcher option and is passed through to FastChess as `-concurrency`; there are no `threads`, `-t`, or `-c` aliases:

```powershell
.\play-games.ps1 -Games 20 -Concurrency 2
```

The FastChess live wrapper uses the single dedicated local viewer URL `http://127.0.0.1:8766/`. If an older live-viewer process is already on that port, the wrapper restarts that viewer on the same port instead of moving to another URL. The wrapper does not expose a port selector. While FastChess is still inside a game, the wrapper starts `tools\mirror_fastchess_live_pgn.py` and points the viewer at `out\live\<run>-live.pgn` so the board has a current position before FastChess writes the final `-pgnout` game.

The wrapper starts the viewer with hot reload by default through `viewer.hotReload` in `chess-harness.config.json`. When `tools\live_pgn_viewer.py` or the viewer docs/config change, the local viewer server restarts and the browser page reloads against the updated UI instead of continuing to show stale in-memory HTML. For direct viewer work, use `python .\tools\live_pgn_viewer.py --hot-reload`.

The viewer uses a three-column desktop layout: the left pane shows fixed-height bot thinking logs and Engine Analysis, the center pane shows the board, and the right pane keeps the leaderboard, matches, moves, and collapsed-by-default Engine Config. In Follow Live mode the thinking pane shows recent live entries with the current move number. During replay navigation, the thinking pane follows the selected ply and shows the prompt/comment/bestmove entries that match the move being replayed, but it does not auto-refresh while Follow Live is off so the text stays copyable. Bot Thinking has persisted side and multi-select message-type filters; message type defaults to only `Comment`, and clicking `All` while all types are selected clears all selected types. Completed games show the winner as `<player> (<colour>) won`, for example `Codex-chess-learner (White) won`. Matches appear below the leaderboard, paginate 5 rows per page, show `In progress` for live concurrent games, and keep the board fixed on the selected live mirror game instead of jumping to the newest concurrent game. Clicking another in-progress row writes a live selection request and switches the followed board intentionally. Completed rows show the date on the compact header line and the winner on the next line, and each completed row can be clicked to load that archived game in the board viewer with matching bot logs. Each active live or archived game shows the tournament slug derived from the PGN filename, opening the viewer without a hash defaults to the live match `#slug`, stale bare live hashes resolve to the fresh in-progress match even when that active slug sorts earlier, replay or archived games use a stable `#slug--game-N` hash, stale mirror daemon status expires instead of advertising old slugs as active, the live mirror reconciles stale launch stdout with FastChess `*.pgn` output plus active engine logs, the header copy icon copies the full absolute viewer URL for the active match, and the active archived match row shows its own copy icon. The board can be flipped from the `Flip Board` button above the Leaderboard and persists the chosen white/black orientation while keeping coordinates, clocks, and player bars aligned. Engine Analysis stays switchable from the top toolbar and the analysis panel; if the server was started with analysis disabled, the UI controls show that disabled state instead of claiming analysis is on.

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

The mirror also writes standard PGN clock comments such as `{ [%clk 0:04:58.125] }` on completed moves. It keeps `WhiteClockMs`, `BlackClockMs`, `ClockUpdatedAtEpochMs`, and `ClockRunningSide` headers from the latest UCI `go wtime ... btime ...` line as live-viewer metadata, so the viewer can show running clocks above and below the board while still preserving per-move clock times in PGN notation. If a live clock reaches zero before FastChess has printed its final result line, the mirror writes the timeout winner and `lost on time` reason into the live PGN, and the viewer shows `Game over` with the winning side and stopped clock. For concurrent FastChess runs, the mirror writes `out\live\<run>-live.status.json` so the viewer can show other active games as `In progress` while the board PGN remains fixed on one selected live game. The viewer writes `out\live\<run>-live.selection.json` when an operator clicks another in-progress row, and the mirror follows that requested game on the next poll while still refusing unrelated latest-log flicker.

The viewer has a `Learner` screen on the top toolbar and also mirrors the latest bot log rows into the board view's left pane. It reads the learner's real `MEMORY.md`, `skills/`, `knowledgebase/`, and recent `out\codex-chess-logs` entries. Bot log rows are colored by source: learner and baseline entries are separated by their active engine context, and engine processes log observable prompt snapshots, repetition-risk counts, invalid-move repairs, bestmove lines, and returned short move comments without claiming hidden chain-of-thought access. The same persisted side and message-type filters apply to the board Bot Thinking pane and the Learner Bot Logs pane.

Learner improvement is handled by `tools\update_learner_knowledgebase.py`. It reads the FastChess PGN plus the redirected launch stdout, writes the selected engine's `knowledgebase\live-match-lessons.md`, writes compact `strategy-lessons.md/json`, and refreshes that engine's `MEMORY.md` autolearn block. The updater collects neutral frozen-self-play observations without Stockfish PVs or hardcoded move answers, then asks Codex app-server to synthesize model-discovered concepts and value adjustments from that evidence. Move prompts also include deterministic `material_safety` context from the current FEN and legal moves so immediate queen/rook hangs and large one-ply material swings are explicit without the client selecting a move. Run it once or as a watcher:

```powershell
python .\tools\update_learner_knowledgebase.py `
  --pgn .\out\fastchess\<run>.pgn `
  --stdout .\out\fastchess\<run>-launch.out.log `
  --watch
```

For Zero autolearn, target its separate context explicitly; the live wrapper does this automatically when started with `-LearningEngine zero`:

```powershell
python .\tools\update_learner_knowledgebase.py `
  --engine-name Codex-chess-zero `
  --context-dir .\engines\codex-chess-zero `
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
