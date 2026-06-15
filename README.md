# AI Chess Harness

UCI chess engines and harness scripts for testing LLM-backed chess play.

## Engines

- `engines/codex-chess`: a UCI engine that asks the local Codex app-server for legal moves.
- `engines/codex-chess-learner`: a separate UCI launcher for the learner engine. It initially runs the same implementation as Codex-chess, but has its own `MEMORY.md` and `skills/` folder for learner-specific durable context.
- `engines/codex-chess-zero`: a separate fast first-principles learner. It uses the same UCI implementation in Zero mode, keeps its own `MEMORY.md`, `knowledgebase/`, `skills/`, and `tools/`, uses no skills during move selection by default, and learns from compact post-game feedback plus current-position reasoning.
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
- `ZeroLocalPuct`: in Zero mode, use the local first-principles deliberative controller with PUCT as bounded calculation support instead of asking the Codex app-server during move selection.

The learner launcher defaults `UseMemory`, `UseSkills`, and `LearningMode` to `true`; the Zero launcher defaults `UseMemory` and `LearningMode` to `true`, `UseSkills` to `false`, and `ZeroMode` to `true`; the baseline launcher defaults learning options to `false`.

## First-principles Zero research

`Codex-chess-zero` now has a local research skeleton under `engines\codex-chess-zero\zero_research.py`. It is deliberately not intended to become a Leela/AlphaZero clone. The local move path uses a deliberative human-style controller that generates candidate buckets, labels plan intent, checks opponent refutations, treats checking/checkmate replies as tactical refutation risk, suppresses forcing-move bonuses when the local reply scan marks them watch/unsafe, downgrades quiet material-up conversion stalls, rejects tactical risk, and uses PUCT/network values as fast calculation support. It also includes board-plane encoding, legal-move masks, compact self-play records, replay-buffer dedupe, a trainer, a promotion gate, anti-memorization checks, and a benchmark ladder. If the local path fails, the engine forfeits with `bestmove 0000` instead of picking a fallback legal move.
For sprint runtime, the expensive local opponent-refutation scan is bounded to the strongest current-position candidate shortlist from MCTS/network features instead of every legal root move. This keeps the calculation first-principles and deterministic without importing external labels.

Run the utilities through the repo wrapper:

```powershell
python .\tools\run_zero_research.py summary
python .\tools\run_zero_research.py self-play --games 1 --visits 8 --max-plies 80
python .\tools\run_zero_research.py train --epochs 1
python .\tools\run_zero_research.py promote --games 2 --visits 8 --threshold 0.55
python .\tools\run_zero_climb.py --cycles 1 --zero-visits 8 --self-play-games 2 --promotion-games 4
```

For immediate back-to-back climb training without waiting for a Codex automation wakeup, run the repo-local loop:

```powershell
.\run-zero-climb-loop.ps1
```

The loop runs `tools\run_zero_climb.py --profile gm-sprint`, starts the next bounded round as soon as the previous one exits, and writes `zero-climb-loop-state.json`, `zero-climb-loop-log.jsonl`, and `zero-climb-loop.lock` under `engines\codex-chess-zero\research\climb`. It refuses overlapping loop instances by default; if a crashed process leaves a stale lock after you have verified no loop is running, restart with `-ForceStaleLock`. For validation or a short supervised burst, use:

```powershell
.\run-zero-climb-loop.ps1 -MaxRounds 1 -- --cycles 1 --zero-visits 8 --self-play-games 2 --self-play-visits 8 --self-play-max-plies 80 --train-epochs 1 --promotion-games 2 --promotion-visits 8
```

The `zero-stockfish-climb` automation runs every 15 minutes as a lightweight reporter/watchdog for that loop, not the trainer. It should read its automation memory, run this command, inspect current climb artifacts, append a concise memory entry, report the current loop/stage/score/promotion/external-label status, and exit:

```powershell
python .\tools\ensure_zero_climb_loop.py --profile gm-sprint --wait-seconds 20
```

If the loop is already running, the helper reports the existing process. If the loop is absent, it starts `tools\run_zero_climb_loop.py` detached, repairs a stale lock only after no loop process is found, waits briefly for verification, writes `zero-climb-loop-ensure-log.jsonl`, and exits. The automation report should also inspect `zero-climb-loop-state.json`, `zero-climb-loop-log.jsonl`, `climb-state.json`, `climb-log.jsonl`, `latest-wisdom-delta.md`, and `research\networks` when present.

For an isolated viewer game against a fixed Stockfish depth, use the helper:

```powershell
.\run-zero-vs-stockfish-depth.ps1 -Depth 1
```

It starts the dedicated local viewer on `http://127.0.0.1:8877/`, opens a `#zero-vs-stockfish-depth-...--live-game-1` URL while the game is being written, and still writes the completed archive PGN under `out\zero-depth-matches` with a matching `#zero-vs-stockfish-depth-...--game-1` archive URL. The live URL reads `out\live\...-live.pgn` immediately instead of waiting for the slower stats scan.

The climb command starts with weak local gates (`random-legal`, `capture-greedy`, `one-ply-material`) and then moves toward installed Stockfish depth/full-strength gates when available. Failed gates trigger self-play, training, and the promotion gate before retrying the same stage; weak engines and Stockfish are evaluation opponents only and are never used as training labels. Failed-gate self-play uses bounded seeded exploration from Zero's own legal candidate list so the replay buffer can discover new Zero-only positions instead of repeating one deterministic game. Capped non-terminal self-play games use deterministic material adjudication from the final self-play board so training still gets a self-play-only outcome signal, drawn self-play trajectories receive a small material-aware non-win penalty that is stronger for the side that failed to convert a large material edge, and duplicate replay entries can be refreshed by later Zero-only outcome signal when the older entry has no signal or the new signal is stronger in the same direction. Each failed-gate training result records whether self-play added or updated replay-buffer positions, whether trajectories duplicated, whether promotion failed, and whether any forbidden external training source was used. Each failed cycle also writes a human-readable wisdom delta so the run leaves inspectable concepts, evidence counts, promotion status, and active-vs-candidate lesson labels rather than only network weights.
Failed-gate self-play also carries a human-readable novelty profile for each selected move. The profile records a class-level novelty key, archive/game repetition counts, safe-or-risky refutation status, role tags, and plan intent, so repeated `stockfish-depth-2` failures can be separated into stale-plan failures versus weak-calculation failures. Novelty pressure is allowed only as a self-play exploration guide and diagnostics signal; it must stay class/concept based, avoid exact FEN-to-move memorization, and keep Stockfish, Lc0, Maia, human games, opening books, and tablebases evaluation-only.
Draw non-win penalties train conversion and safety features without globally lowering the network bias, so repeated drawn self-play can remain corrective without making every candidate network broadly worse before promotion.
Training uses a bounded deterministic replay sample from the accumulated buffer: recent failed-gate records stay represented, and older high-outcome-signal records are retained without letting the full replay history stall a sprint.
Within that bounded sample, failed-conversion stalls and locally refutable forcing non-wins receive extra self-play-only priority so the current failed-gate weakness is not diluted by older neutral records.
When material-up self-play repeatedly draws, training also marks quiet non-progress moves with a current-position `conversion_stall` feature so failed conversion is corrected without external engine labels.
Learned feature weights are clamped to bounded sprint-safe ranges before candidate networks are saved or loaded, preventing one draw-heavy or forcing-heavy cycle from over-amplifying checks, value terms, or conversion penalties enough to destabilize the next gate.
Zero replay records now use a schema-versioned full-state identity, including side to move, castling, en-passant, halfmove clock, and repetition bucket. Self-play records also persist terminal kind, WDL targets, MCTS root visit counts, and the root visit-policy target used for policy training. Self-play exploration is seed-logged and training-only: root prior noise, early-ply temperature, and deterministic visit-budget jitter are enabled for Zero self-play, while evaluation and promotion keep deterministic no-noise search. Training caps repeated first-8-ply self-generated signatures so one repeated line cannot dominate a batch, and each failed-gate round reanalyzes a small stale replay slice with the current champion to refresh policy targets without changing terminal outcomes or importing external labels.
The candidate promotion order is internal first, external second: a candidate must clear the champion-vs-candidate promotion gate before the same failed external ladder stage is retried. Every climb round appends `engines\codex-chess-zero\research\climb\climb-metrics.jsonl` with external WDL, true/capped/repetition draw counts, opening-signature diversity, replay add/update/skip counts, policy/value loss, internal gate score, and external ladder score. Hand-authored chess motif warnings such as early edge-knight moves or direct shuffles are excluded from Zero feature weights; when used, they live only as legal-move-bounded learner prompt advisories.

The old time-boxed sprint prompt has been replaced by a fast `zero-stockfish-climb` automation reporter/watchdog that ensures `.\run-zero-climb-loop.ps1` is alive, updates automation memory from current artifacts, reports status, and then exits. The underlying `gm-sprint` profile is intentionally stronger than the quick smoke command: two cycles, 16 Zero visits, six self-play games, 120-ply self-play caps, two training epochs, and an eight-game promotion gate. Reduce settings only when the machine is overloaded, and increase bounded visits/self-play/promotion only when there is clear headroom. Each round should focus on the current failed gate and make at most one small safe research/evaluator/training improvement from current wisdom evidence before rerunning validation.

Research artifacts are generated under `engines\codex-chess-zero\research\` and are ignored by git. Climb state is persisted under `engines\codex-chess-zero\research\climb\climb-state.json` with append-only attempts in `climb-log.jsonl`; the latest readable lesson report is written to `engines\codex-chess-zero\research\wisdom\latest-wisdom-delta.md` with JSON and JSONL companions. Stockfish, Lc0, Maia, human games, opening books, and tablebases are evaluation/reference only for this first-principles track; they are not training labels or prompt context for Zero move selection. LLM reasoning belongs in post-game concept discovery, failure clustering, curriculum design, and human-readable strategy formation; those ideas are compiled into fast local evaluators, skills, tools, and tests rather than per-node search calls. The local viewer has a `Research` screen that reads the same real research state, including a separate `Climb Progress` panel that does not alter the live match tracker.
Online research and ChatGPT Deep Research may inform Zero's human-readable curriculum design, but any accepted idea must be translated into local deterministic current-position checks, self-play novelty metrics, or tests before it affects training. A report or engine reference is never a move label.

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

Install FastChess into a repo-local ignored cache and run unattended games between two learning engines. The default players are `player1: learner` and `player2: zero`, which run the prompt-memory `Codex-chess-learner` against the first-principles `Codex-chess-zero`. Use `-Player1 codex -Player2 zero` when you want the baseline `Codex-chess` against Zero. The older `-LearningEngine zero` shortcut still works and maps to `-Player2 zero`. The default run has no `-maxmoves` adjudication cap and uses the configured time control.

Runner defaults live in `chess-harness.config.json`. Use that file for normal defaults such as `fastChess.player1`, `fastChess.player2`, Codex model, effort, Codex move-time budgets, learner context thresholds, FastChess game count, FastChess `concurrency`, time control, viewer port, analysis settings, learner autolearn, and preflight behavior. CLI arguments still work as one-off overrides for a run.

```powershell
.\install-and-run-fastchess-codex.ps1
```

Run the default learner-vs-Zero harness explicitly:

```powershell
.\play-games.ps1 -Player1 learner -Player2 zero -n 20
```

Run baseline Codex against Zero instead:

```powershell
.\play-games.ps1 -Player1 codex -Player2 zero -n 20
```

The legacy Zero form is still supported:

```powershell
.\play-games.ps1 -LearningEngine zero -n 20
```

With the checked-in config, the script sets both engine processes to `CODEX_CHESS_MODEL=gpt-5.3-codex` and `CODEX_CHESS_EFFORT=high`. This is an environment override for the tournament run only; it does not modify the installed Codex app or CLI configuration. During learner training, the engine still uses that model but caps prompt payloads and uses the configured faster per-turn learner/critical/Zero efforts in `chess-harness.config.json` so active games are not dominated by oversized high-effort turns. The script preflights the selected Codex model before starting FastChess, so a usage-limit or unavailable-model error fails before a long match can produce zero-ply forfeits. Results are written to `out\fastchess`.

The script prints `Player1`, `Player2`, and the exact PGN path before the match starts. FastChess writes learner-vs-Zero default runs under `out\fastchess\codex-learner-vs-codex-zero-<timestamp>.pgn`, uses the configured learner run name for `codex` versus `learner`, uses the configured Zero run name for `codex` versus `zero`, and uses a generated `<player1>-vs-<player2>` run name for other player ordering. Matching FastChess config and log files are written beside it.

To start the local viewer before FastChess and open it against the exact PGN path for that run:

```powershell
.\watch-fastchess-live-match.ps1
```

For the short command form, use `play-games.ps1`; `-n` is an alias for `-Games`:

```powershell
.\play-games.ps1 -n 100
```

Use FastChess wording for parallel games. `-Concurrency` is the public launcher option and is passed through to FastChess as `-concurrency`; the existing `-p` shorthand remains a compatibility alias for `-Concurrency`, but there are no `threads`, `-t`, or `-c` aliases:

```powershell
.\play-games.ps1 -Games 20 -Concurrency 2
.\play-games.ps1 -n 100 -p 2
```

If CPU becomes the bottleneck, keep `-Concurrency` low and rely on the fast training profile instead of starting more Codex app-server processes. The checked-in defaults cap history, material-safety rows, learner memory, FEN lessons, strategy lessons, knowledgebase files, and skills per move, while leaving the no-fallback legal-move boundary intact.

The FastChess live wrapper uses the single dedicated local viewer URL `http://127.0.0.1:8766/`. If an older live-viewer process is already on that port, the wrapper restarts that viewer on the same port instead of moving to another URL. The wrapper does not expose a port selector. While FastChess is still inside a game, the wrapper starts `tools\mirror_fastchess_live_pgn.py` and points the viewer at `out\live\<run>-live.pgn` so the board has a current position before FastChess writes the final `-pgnout` game.

The wrapper starts the viewer with hot reload by default through `viewer.hotReload` in `chess-harness.config.json`. When `tools\live_pgn_viewer.py` or the viewer docs/config change, the local viewer server restarts and the browser page reloads against the updated UI instead of continuing to show stale in-memory HTML. For direct viewer work, use `python .\tools\live_pgn_viewer.py --hot-reload`.

The viewer uses a three-column desktop layout: the left pane shows fixed-height bot thinking logs and Engine Analysis, the center pane shows the board with the move list directly below it, and the right pane keeps the leaderboard, matches, and collapsed-by-default Engine Config. The move list highlights the currently displayed live or replay ply, shows only the latest five move-number rows around that ply by default, has a separate `All Moves` / `Latest 5` toggle for the rest of the history, and keeps the persisted `Hide` / `Show` button for collapsing only the move-list body. In Follow Live mode the thinking pane shows recent live entries with the current move number. During replay navigation, the thinking pane follows the selected ply and shows the prompt/comment/bestmove entries that match the move being replayed, but it does not auto-refresh while Follow Live is off so the text stays copyable. Bot Thinking has persisted side and multi-select message-type filters; message type defaults to only `Comment`, and clicking `All` while all types are selected clears all selected types. Completed games show the winner as `<player> (<colour>) won`, for example `Codex-chess-learner (White) won`. Matches appear below the leaderboard, paginate 5 rows per page, show `In progress` for live concurrent games, and keep the board fixed on the selected live mirror game instead of jumping to the newest concurrent game. Clicking another in-progress row writes a live selection request and switches the followed board intentionally. Completed rows show the date on the compact header line and the winner on the next line, and each completed row can be clicked to load that archived game in the board viewer with matching bot logs. Each active live or archived game shows the tournament slug derived from the PGN filename, opening the viewer without a hash defaults to the live match `#slug`, stale bare live hashes resolve to the fresh in-progress match even when that active slug sorts earlier, replay or archived games use a stable `#slug--game-N` hash, stale mirror daemon status expires instead of advertising old slugs as active, the live mirror reconciles stale launch stdout with FastChess `*.pgn` output plus active engine logs, the header copy icon copies the full absolute viewer URL for the active match, and the active archived match row shows its own copy icon. The board defaults to placing `Codex-chess-learner` at the bottom whether it is White or Black; the `Flip Board` button above the Leaderboard switches to manual orientation while keeping coordinates, clocks, and player bars aligned. Engine Analysis stays switchable from the top toolbar and the analysis panel; if the server was started with analysis disabled, the UI controls show that disabled state instead of claiming analysis is on.

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

Learner improvement is handled by `tools\update_learner_knowledgebase.py`. It reads the FastChess PGN plus the redirected launch stdout, writes the selected engine's `knowledgebase\live-match-lessons.md`, writes compact `strategy-lessons.md/json`, refreshes that engine's `MEMORY.md` autolearn block, and can maintain guarded engine-local self-extension artifacts under `skills\self-play-concepts\` and `tools\`. The updater collects neutral frozen-self-play observations without Stockfish PVs or hardcoded move answers, then asks Codex app-server to synthesize model-discovered concepts and value adjustments from that evidence. Generated skills/tools must stay generic and current-position feature based: no exact FEN-to-move rules, opening books, tablebases, Stockfish/Lc0/Maia PVs, or human-game imitation labels. Move prompts also include deterministic `material_safety` context from the current FEN and legal moves so immediate queen/rook hangs and large one-ply material swings are explicit without the client selecting a move. Run it once or as a watcher:

```powershell
python .\tools\update_learner_knowledgebase.py `
  --pgn .\out\fastchess\<run>.pgn `
  --stdout .\out\fastchess\<run>-launch.out.log `
  --watch
```

The live wrapper defers concept synthesis during active games by default (`learner.conceptSynthesisDuringWatch=false`) and runs one final synthesis pass after FastChess exits (`learner.conceptSynthesisAfterRun=true`). That keeps the watcher cheap while games are running, preserves pending evidence in `strategy-lessons.json`, and still writes generalized concepts at the end of the run.

To prove the learner can actually use newly written context and improve, run the isolated before/after UCI proof:

```powershell
python .\tools\prove_learner_improvement.py --model gpt-5.3-codex --effort medium
```

The proof creates temporary learner contexts under `out\learner-proof\<timestamp>`, asks the same UCI engine to move before and after a new proof lesson is present, and passes only when the learned-context score improves. It does not edit the real learner memory or knowledgebase.

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

## Lichess master-game learner wisdom

Use `tools\master_wisdom.py` when the prompt learner should extract generalized human-readable principles from Lichess-derived master games. This is a `Codex-chess-learner` workflow only. Learner-facing wisdom must stay Codex-authored and principle-only: no opening-family win rates, side scores, evidence-count suffixes, exact FEN-to-move rules, opening-book move lists, tablebases, or Stockfish PVs. Human-game data must not feed `Codex-chess-zero`, whose training remains first-principles self-play only.

The downloadable source is the Lichess Elite Database at `https://database.nikonoel.fr/`. It is a filtered elite/master export built from Lichess standard games and is the practical bulk-download source for this workflow; Lichess does not publish the opening-explorer Masters corpus as an official bulk PGN download.

Typical flow:

```powershell
python .\tools\master_wisdom.py manifest --refresh
python .\tools\master_wisdom.py download --limit 1
python .\tools\master_wisdom.py learn --batch-size 500
python .\tools\master_wisdom.py cycle --cycles 1
```

Downloads and ladder state are ignored under `out\lichess-master`. If a requested batch cannot be satisfied from local archives, `learn`/`cycle` attempts to download the next missing Lichess Elite archive from the manifest before stopping short. The learner context is written to `engines\codex-chess-learner\knowledgebase\master-wisdom.md/json` and `engines\codex-chess-learner\skills\master-game-wisdom\SKILL.md`; once those files are authored, later learn/evaluate/cycle runs preserve them instead of replacing them with generated counters. Learner move prompts pin `master-wisdom.md` and `master-game-wisdom/SKILL.md` into context so capped prompt selection cannot skip the authored wisdom. The ladder learns one batch, plays up to 10 games against the current Stockfish depth, requires an 80% score to advance, stops early once the target points are mathematically unreachable, increases batch size on failure, and targets depth 8. Active ladder games mirror to `out\live\master-wisdom-live.pgn` with live clock headers, standard PGN `[%clk ...]` comments, and a viewer status sidecar so the in-app browser can follow training live with ticking clocks. The viewer exposes this state in the `Master Wisdom` tab with a separate depth leaderboard and a current match leaderboard for the active 10-game attempt.

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
