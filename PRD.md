# Chess Live Viewer PRD

## Goal

Keep the local FastChess viewer useful during live play and replay without leaking analysis or client shortcuts into engine move selection.

## Product Requirements

0. Future non-trivial chess-harness tasks should update `PRD.md` and `PRD_CHECKLIST.md` before implementation and keep checklist status current while work proceeds.
1. The board page uses a three-column desktop layout.
2. The left column contains fixed-height `Bot Thinking` and `Engine Analysis`.
3. `Bot Thinking` shows observable prompt/comment/bestmove logs, syncs to the replayed move when Follow Live is off, and includes the move number for the selected move/log context.
4. `Engine Analysis` replaces `Stockfish Analysis`, shows the selected local engine name, stays viewer-only, and must never be sent to Codex-chess or Codex-chess-learner prompts.
5. The right column shows `Leaderboard`, then `Matches`, then moves and config.
6. `Matches` lists in-progress live FastChess games and completed games below the leaderboard, with status labels and 5-row pagination.
7. Clicking a completed `Matches` row loads that archived game into the board viewer, including its move list, result, analysis position, and matching bot logs.
8. When Follow Live is off, `Bot Thinking` stays steady unless the user intentionally selects another move, match, side filter, or re-enables live following.
9. Completed games show the winner as `<player> (<colour>) won`; draws show `Draw`.
10. The learner must always choose its own move from the prompt context. There is no fallback legal move, no client-picked shortcut, and no Stockfish-provided move advice.
11. Repetition/threefold detection belongs to the chess client/logging layer and must not be injected as a `repetition_risk` prompt field.
12. The FEN learner curriculum must test position interpretation with hidden locally graded answers, including square occupancy, side to move, check state, material counts, piece locations, castling rights, en-passant state, legal move recognition, and basic position facts.
13. FEN curriculum prompts must forbid online search and tool use. Learning happens through local writeback of concise missed-question lessons into the learner memory and knowledgebase after grading.
14. The curriculum should contain at least 50 multiple-choice tests and should repeat only the missed concepts until the selected Codex model can generalise across the full held-out set or a clear model/app-server blocker is recorded.
15. Every game loaded in the viewer, live or archived, shows the tournament slug derived from its PGN path.
16. The viewer provides a compact copy icon that copies the full absolute viewer URL for the currently loaded game.
17. The viewer URL hash mirrors the active match slug, for example `#codex-vs-codex-learner-standard5-patched-20260512-100210`, and replay or archived game selections include a stable `--game-N` suffix.
18. The viewer defaults to the live match URL hash when opened without a hash, preserves archived `#slug--game-N` deep links during startup, and shows a compact copy icon on the active archived match row.
19. Runner defaults that make sense to persist, including Codex model/effort, preflight timeout, FastChess defaults, viewer defaults, and learner autolearn settings, live in root `chess-harness.config.json`; generated per-run FastChess config JSON remains an artifact, not the source of defaults.
20. Live mirrored PGNs store player clock times in standard PGN `[%clk ...]` move comments, while keeping live ticking clock headers for the viewer.
21. OpenRouter-backed chess models have a first-class UCI path that can be searched by model name/provider and launched into a match without editing engine code.
22. OpenRouter match play must reflect model output only. Missing keys, API failures, invalid JSON, or illegal moves must forfeit with `bestmove 0000` instead of using heuristic fallback moves.
23. Bot Thinking includes a multi-select message-type filter that composes with the side filter, defaults to only `Comment`, persists in `localStorage`, and lets the `All` control toggle between all-selected and none-selected.
24. The board view includes flip-board functionality above the Leaderboard, persists the selected orientation in `localStorage`, and keeps coordinates, player bars, and last-move highlighting consistent with the chosen orientation.
25. Frequent PRD/checklist changes are a repo workflow rule and should be recorded in `AGENTS.md` so future work updates `PRD.md` and `PRD_CHECKLIST.md` before implementation.
26. Previous Matches rows keep the same compact height, with the match date in the top/header line and the winner shown on the next line.
27. Learner autolearn continuously collects neutral frozen-self-play observations, uses Codex app-server to synthesize generalized concepts and value adjustments into compact bounded `strategy-lessons` knowledgebase files, and feeds those model-discovered concepts into future learner prompts without hardcoding FEN-specific move answers.
28. Initial continuous strategy learning uses only `Codex-chess` with memory/skills/learning disabled as the opponent; Stockfish analysis stays viewer-only and must not seed learner strategy lessons in this phase.
29. The dedicated FastChess viewer workflow supports hot reload for viewer source/config/doc changes so UI edits become visible without manually finding and restarting stale `live_pgn_viewer.py` processes.
30. FastChess parallel play uses the native `Concurrency` setting and CLI option, backed by FastChess `-concurrency`; no `threads`, `-t`, or `-c` aliases are part of the public runner contract.
31. During concurrent FastChess runs, the live mirror stays fixed on one active game for the board while the right-column match list shows other in-progress games.
32. `Engine Config` is collapsed by default, persists explicit open or closed user preference in `localStorage`, and preserves the structured controls plus raw JSON toggle when opened.
33. Concurrent live mirror state must follow one monotonic move-line track and must not switch board positions just because another concurrent engine log updated later.
34. In-progress match rows are clickable and should switch the followed live board intentionally instead of requiring a page reload or wrapper restart.
35. A repo-local PowerShell reset script can back up all generated game artifacts into a timestamped archive and leave the leaderboard at zero completed games without deleting learner memory, learner skills, learner knowledgebase, validation artifacts, or existing backups.
36. Viewer stats ignore archive folders such as `out\backups` so backed-up games remain available for inspection without contributing to the active leaderboard.
37. The default Codex game-playing model is `gpt-5.3-codex` with reasoning effort `high`; lower efforts are explicit one-off overrides, not the runner default.
38. If a GUI or FastChess clock says the side to move has no time remaining, LLM-backed engines must not start a new model/app-server/API turn; they should immediately forfeit with `bestmove 0000` so the runner can end the game.
39. The live board should display the timed-out side, winner, and reason on the board header, stop the visible clock at zero, and write the timeout result into the live PGN when FastChess has not emitted its final line yet.
40. Optional UCI comment output must be ASCII-safe and non-fatal so model comments with smart punctuation or other Unicode text cannot prevent the engine from writing the actual `bestmove` line.
41. Codex UCI engines must enforce a bounded per-move app-server timeout derived from the remaining GUI clock, and learner prompts must reduce embedded memory/knowledgebase context as time drops so 5+0 FastChess games do not spend tens of seconds on routine moves.
42. An explicit live selection file or `#slug--live-game-N` URL pins that game until the operator chooses another game, even after the selected game completes; automatic advancement to the next in-progress game is only allowed for unpinned/default live watching.
43. A bare live URL hash such as `#codex-vs-codex-learner-live-...` must keep Follow Live enabled for that live PGN; archived or replayed games require an explicit `--game-N` hash suffix.
44. The live mirror must keep repeated opening lines from separate FastChess games as separate engine tracks, so a later game that starts with the same moves does not inherit stale clocks or positions from an earlier game.
45. A stale or non-matching archived hash such as `#old-live-slug--game-1` must not leave the viewer in replay mode against the current live PGN; Follow Live should remain on until the matching archived row is found.
46. The FastChess live mirror must ignore engine logs older than the current run's launch stdout file, so earlier runs cannot make a new live board show stale positions or clock timeouts while the current game is still underway.
47. If a live mirrored clock expires by wall-clock elapsed time after the last engine-log clock update, the mirror must write the timeout result into both the live PGN and live status JSON even when FastChess stdout has not printed `Finished game`.
48. A completed or stale bare live URL hash must resolve to the newest fresh in-progress live match even when that active match slug sorts before the stale hash, while explicit `#slug--game-N` links stay archived replay links.
49. The live mirror must reconcile stale FastChess launch stdout with the real FastChess `*.pgn` output and current engine tracks, must not pin an unfinished current game to stale locked moves from an older repeated-opening track, and stale daemon mirrors must stop refreshing live status when their own run artifacts are no longer fresh.
50. FastChess learner runs stay at 5+0 by default, but Codex move budgeting must avoid self-inflicted timeouts: normal mid-clock moves get enough app-server wait budget, and retry prompts after a timeout or invalid move must drop learner context, drop comments, and use a lower-effort urgent turn instead of restarting multiple full-context high-effort turns that burn the clock.
51. If FastChess still reports a single unfinished game while current-run engine logs contain multiple move-line tracks, the live mirror must show the freshest active track for that game instead of keeping an older stale track whose wall-clock timeout would make the board look completed while engines keep playing.
52. Non-urgent Codex-chess and Codex-chess-learner moves must request and surface a short observable move comment; blank model comments should be visible as an explicit no-comment marker instead of leaving the viewer's Comment filter apparently empty.
53. Learner lesson timestamps should update only when lesson content changes. Watcher cycles that find no new live-match lessons or no new strategy evidence must preserve the prior lesson timestamp instead of rewriting a fresh date.
54. The active in-progress board game must have a game-specific live URL hash, using `#slug--live-game-N`, so concurrent FastChess games are distinguishable in the address bar while archived replay links keep the existing `#slug--game-N` form.
55. FastChess live mirror board artifacts must be written as one live PGN file per board game, with the slug timestamp derived from that game's own start time instead of the run start time, while a stable run-level status/control file keeps live selection working.
56. The board page must show a bold current-game header near the top of the page, naming the active game number and the engines playing it.
57. Arrow-key and toolbar move navigation from an active `#slug--live-game-N` game must keep replaying that same current-game PGN after `Follow live` is disabled, and the URL/copy target must remain a `--live-game-N` hash instead of switching to archived `--game-N`; it must not fall back to the viewer startup PGN or any archived/random game.
58. When Follow Live is off, selecting another archived game must update the board, move navigation, logs, and analysis to that selected game, even when the previous and selected games are different game indexes inside the same PGN file.
59. Learner prompts must include a deterministic material-safety audit from the current FEN and legal moves, warning about legal moves that put a queen, rook, or other moved piece on an immediately capturable square or allow a large one-ply material swing. This audit is advisory context only; the model still chooses from `legal_moves`, and Stockfish analysis remains viewer-only.
60. `Codex-chess-zero` must exist as a separate UCI engine based on the shared Codex chess implementation but with its own `engines\codex-chess-zero` memory, knowledgebase, and wrapper command. Zero should learn from post-game feedback in its own files, reason from the current FEN/legal moves/material safety rather than inherited learner strategy, and use a fast prompt profile with lean context and short move timeouts.
61. Learner and Zero post-game feedback should share the same autolearn code path through a target engine/name/context option, so the watcher can update either engine after games without mixing their memories.
62. Learner `MEMORY.md` autolearn updates should only rewrite the block when score, reasons, or rule content changes; watcher cycles must not dirty the file with only a newer `Last updated` value.
63. The live viewer should reduce fixed browser polling for game, stats, learner, and hot-reload updates by using the existing Python viewer process as a push source. SignalR is not the preferred implementation unless a future .NET host or bidirectional hub contract is introduced; the current account-free one-way file-change workflow should use a lightweight server-sent events stream with polling fallback.
64. Learner training should reduce active-game Codex latency without weakening the no-fallback contract: move prompts should cap history, material-safety rows, memory, FEN lessons, strategy lessons, knowledgebase, and skills to compact configurable limits, and training/critical/Zero turns may use lower per-turn effort while preserving the configured default model.
65. Learner autolearn watchers should avoid competing with active games for Codex app-server and CPU time. During a live FastChess run, the watcher may defer concept synthesis while still collecting evidence and updating lightweight lesson summaries; after FastChess exits, the wrapper should run one final synthesis pass so generalized concepts are still written.
66. Learner improvement claims need a repeatable proof artifact, not only passing unit tests. A controlled proof may use a temporary learner context, run the same UCI engine before and after adding a knowledgebase lesson, and pass only when the learned-context score is higher while the no-fallback legal-move boundary is preserved.
67. FastChess runner engine selection must expose a simple `player1` / `player2` contract in `chess-harness.config.json` and matching `-Player1` / `-Player2` CLI overrides, while preserving existing `-LearningEngine learner|zero` behavior as a compatibility shortcut.
68. The short launcher must preserve the existing `-p` compatibility shorthand for FastChess concurrency, so `.\play-games.ps1 -n 100 -p 2` remains valid even after adding `-Player1` and `-Player2`.
69. If the live mirror infers a timeout before FastChess writes a completed PGN row, that inferred finish must persist for the current run so the board can advance to the next in-progress concurrent game instead of pinning the viewer to a finished or missing PGN.
70. When the run-level control PGN is intentionally absent because the mirror writes per-game live PGNs, a default `/api/game` request must resolve through the live status sidecar to the current board PGN so first page load does not show a false missing-PGN state.
71. `Codex-chess-zero` is the first-principles research engine. Its core learning path must use current board state, legal moves, self-play outcomes, and promoted Zero weights, not Stockfish/Lc0/Maia PVs, opening books, tablebases, or human-game move imitation.
72. Zero research must expose local calculation infrastructure without making Zero a Leela/AlphaZero replica: board-plane encoding, legal-move masking, a policy/value evaluator, PUCT/MCTS calculation support, compact self-play records, a replay buffer, a trainer, and a promotion gate against the previous Zero weights.
73. Zero's UCI move path may use the local PUCT engine when enabled. If that local engine fails or cannot produce a legal model-selected move, it must forfeit with `bestmove 0000`; the client must not choose a fallback legal move after invalid model output.
74. The LLM must stay out of Zero's inner-loop search. Codex app-server may be used for post-run concept synthesis, failure clustering, and short public explanations, but not for MCTS node expansion or per-node evaluation.
75. Human-like Zero output means public candidate lists, threat maps, plan continuity, opponent best-reply checks, tactical blunder checks, clock-aware choice summaries, and concise visible comments. It does not mean Maia-style human move imitation unless explicitly requested later.
76. Zero research artifacts must include anti-memorization evidence: exact FEN-to-move rules are rejected, repeated positions are deduplicated in training data, and strategy lessons must remain feature/concept based instead of move-answer based.
77. The viewer must expose a Research screen showing the current Zero network identity, self-play/training/promotion counts, benchmark ladder entries, latest promoted score, concept summaries, and anti-memorization status.
78. Stockfish parity is not a completed milestone until Zero has real policy/value training, MCTS, large self-play volume, and measurable Elo growth across multiple promoted generations.
79. The checked-in short FastChess default should target `Codex-chess-learner` versus `Codex-chess-zero`, so `.\play-games.ps1 -n 100 -p 2` compares the prompt-memory learner against the first-principles Zero engine unless a caller explicitly overrides `-Player1` or `-Player2`.
80. Zero's primary identity is a deliberative human-reasoning engine with fast calculation support. It should generate candidate move classes like checks, captures, threats, development, king safety, center control, and pawn breaks; attach plan intent; calculate plausible opponent refutations; reject tactically unsafe candidates; and use PUCT/network outputs as one verifier rather than as the whole decision-maker.
81. Zero may use LLM reasoning for post-game concept discovery, failure clustering, curriculum design, and human-readable strategy formation, then compile those ideas into fast local evaluators, skills, tools, and tests. Per-node search and forced tactical verification must remain local and deterministic enough for time controls.
82. Learner self-improvement should include guarded self-extension: post-game autolearn may create or update engine-local Agent Skills and tool artifacts under the selected engine context when they are derived from self-play concepts and pass anti-cheating checks. These artifacts must be generic, feature/concept based, and forbidden from containing exact FEN-to-move answers, opening books, tablebase facts, Stockfish/Lc0/Maia PVs, or human-game imitation labels.
83. Learner-created tools are allowed only as local transparent aids that compute or summarize current-position features, concept triggers, tactical safety, or self-play evidence. They must not call online services, query external engines for labels, or choose fallback moves for the engine.
84. Zero must have a measurable climb loop toward Stockfish strength. The loop starts with weak non-cheating local opponents, advances only after Zero clears a gate, and eventually evaluates against installed Stockfish depth/node stages and full installed Stockfish. Opponent moves and Stockfish analysis are evaluation gates only and must never become Zero training labels.
85. When Zero fails a ladder gate, the climb loop should generate more Zero self-play, train a candidate from self-play records, run the existing candidate-vs-incumbent promotion gate, and then retry the current ladder stage. It must persist climb state, match logs, stage attempts, pass/fail thresholds, and current stage under ignored Zero research artifacts.
86. "Stockfish level" is a terminal evaluation state, not a promise from one run. The system may keep playing/training until it reaches the installed Stockfish stages, but it must report honest gate status, sample sizes, and current blocker instead of claiming parity without a passed gate.
87. The web UI must expose Zero climb progress inside the existing Research view without changing the live match tracker's board-following, live hash, PGN mirror, or match-row behavior. Climb data should be read from persisted Zero research artifacts and show current gate, latest score, passed stages, promotion/training result, and no-external-label evidence.
88. Prompt-readable Zero strategy knowledgebase artifacts must not persist raw exact FEN-to-move evidence. Store hashed evidence IDs and generalized observation summaries in `strategy-lessons.json`/`.md`; raw self-play positions may be used only transiently during local extraction or concept synthesis and must not become move-selection context.
89. Failed Zero climb gates must diagnose whether the follow-up self-play actually added new replay-buffer signal, whether self-play repeated the same trajectory, and whether the candidate failed promotion. This diagnosis is part of the self-improvement loop, not just operator reporting.
90. Zero self-play should use bounded, seeded early-game exploration from its own candidate list so failed-gate training can discover new self-play positions without using weak-engine, Stockfish, Lc0, Maia, human-game, opening-book, or tablebase labels.
91. Capped non-terminal Zero self-play games must still produce a self-play-only learning signal through deterministic material adjudication. This is allowed only from the final Zero self-play board state and must not use external engine labels.
92. The replay buffer should preserve dedupe by position while still updating stale duplicate self-play records when a later Zero-only run supplies an outcome signal for a position previously stored with no signal.
93. Drawn Zero self-play games should count as a small self-play-only non-win penalty for both sides so repeated drawing trajectories create a corrective signal instead of zero-gradient stagnation.
94. Every failed Zero climb cycle must write a human-readable wisdom delta alongside network artifacts. The delta should summarize what the cycle learned, what evidence supports it, whether it promoted, and whether each lesson is active or only a candidate hypothesis.
95. Wisdom deltas must be concept/evidence based and safe for prompt use: no exact FEN-to-move rules, no opening-book lines, no external-engine labels, and no human-game imitation labels.
96. The `zero-stockfish-climb` automation should run as a GM-track research sprint rather than a tiny static smoke test: use the `run_zero_climb.py --profile gm-sprint` defaults or the strongest bounded visits/self-play/promotion settings the local machine can handle, time-box each run, focus every cycle on the current failed gate, and turn recurring failure/wisdom evidence into one safe implementation or evaluator improvement before rerunning validation.
97. The automation must stay honest about strength: it may target Grandmaster/Stockfish strength, but it must not claim GM progress unless promoted generations and ladder results prove it. Weak engines, Stockfish, Lc0, Maia, human games, opening books, and tablebases remain evaluation or paper/research reference only, never training labels.
98. Drawn Zero self-play should remain a non-win signal while becoming material-aware: when a drawn self-play game ends with one side holding a large material edge, that side should receive a stronger self-play-only failed-conversion penalty, without using external engines, tablebases, opening books, or human-game labels.
99. Zero's local deliberative evaluator should treat opponent checking replies, especially checkmates or checking captures, as refutation risk in the existing best-reply scan so forcing-looking moves do not outrank basic king safety. This must remain deterministic and current-position-only, with no Stockfish, Lc0, Maia, tablebase, opening-book, or human-game labels.
100. Draw/failure penalties from Zero self-play should train conversion and safety features without globally depressing the network bias. Repeated drawn self-play is a useful signal, but it must not make every candidate network broadly worse before promotion.
101. Failed Zero self-play records for checks, captures, promotions, or threats with a local refutation should apply a stronger self-play-only training penalty to the risky forcing features so repeated Stockfish-depth-1 failures attack the forcing-move weakness without importing external move labels.
102. Zero's deliberative evaluator should suppress forcing-move bonuses when the current-position local reply scan marks a move as watch/unsafe, so repeated checks, captures, promotions, or threats do not outrank king safety and material safety just because they look forcing.
103. Zero failed-gate training should bound each sprint's replay sample to a deterministic recent-plus-outcome-signal subset so accumulated replay history does not stall the automation while fresh failed-gate evidence remains represented.
104. Zero's expensive local opponent-refutation scan should run over a bounded current-position candidate shortlist from MCTS/network features rather than every legal root move, preserving deterministic no-external-label reasoning while keeping Stockfish-depth-1 sprint self-play and promotion within practical local runtime.
105. Zero failed-conversion training should distinguish quiet material-up stalling from low-risk conversion progress using only current-position self-play features, so repeated drawn self-play can penalize non-progress without external engine labels.
106. Zero's deliberative evaluator should immediately downgrade quiet material-up conversion stalls, so self-play wisdom about failed conversion affects move selection before another promotion cycle compounds the same draw pattern.
107. Zero replay training should clamp learned feature weights to bounded sprint-safe ranges so draw-heavy or forcing-heavy self-play cannot over-amplify checking, value, or conversion features enough to destabilize the next Stockfish-depth gate.
108. Zero bounded replay sampling should prioritize self-play records that expose failed conversion stalls or locally refutable forcing non-wins, so the sprint keeps the current failed-gate weakness represented without importing external labels.
109. Isolated Zero-vs-Stockfish depth runs must open a live-follow URL such as `#zero-vs-stockfish-depth-1-...--live-game-1` while the match is being written, keep that board independent of the slower real stats scan, and still provide archive URLs such as `#zero-vs-stockfish-depth-1-...--game-1` that load the completed PGN on first page open.
110. Zero's bounded deliberative root shortlist should reserve a few low-risk quiet/safety candidates in addition to the strongest cheap candidates, so repeated failed Stockfish-depth gates can choose safe conversion or king-safety moves instead of only over-weighted forcing moves.
111. Zero replay-buffer duplicate handling should refresh an existing self-play position when a later Zero-only record carries a stronger same-direction outcome signal, so repeated failed-conversion or risky-forcing evidence is not discarded just because the position already exists.
112. Zero climb can run as a repo-local continuous loop instead of depending on Codex automation cadence. The loop must start the next bounded climb round immediately after the previous round exits, prevent overlapping loop instances with a lock file, persist loop state/logs under Zero research climb artifacts, and keep all training-source guardrails identical to `run_zero_climb.py`.
113. The `zero-stockfish-climb` automation should act as a frequent fast reporter/watchdog: every sub-30-minute cadence, verify the repo-local climb loop is running, start it detached if absent, repair stale loop locks only after confirming no loop process is alive, inspect the latest climb state/log/wisdom artifacts, update automation memory with the current stage/latest score/promotion/external-label guard status when available, report those facts, then exit without running climb training or editing research code itself.
114. Live game following must not be blocked by expensive historical stats or Engine Analysis work. `/api/stats` should scan completed PGNs from headers only, and the board refresh path should render `/api/game` from PGN state before requesting optional Engine Analysis for the selected position.

## Validation Requirements

1. Python files compile.
2. PowerShell wrappers parse.
3. The `chess-harness-codex` source and installed skills validate.
4. FEN curriculum offline validation proves the 50 hidden-answer questions are generated, graded, and written to learner output files without calling Codex.
5. Config validation proves PowerShell and Python runners read defaults from `chess-harness.config.json`, and direct Codex model preflight succeeds without Windows shim launch errors.
6. OpenRouter model search can find a target model such as `x-ai/grok-4.3`, and the generic UCI match runner can pass that model into `llm-chess-engine` against `Codex-chess-learner`.
7. Continuous strategy learning validation proves the autolearn script collects neutral self-play evidence, deduplicates repeated evidence, writes `strategy-lessons.md/json` with model-discovered concepts when synthesis is available, and includes those concepts in learner prompt context without Stockfish PVs.
8. Game backup reset validation proves the PowerShell script parses, moves active game artifacts into a timestamped backup folder, preserves learner state, and leaves viewer stats with zero completed games.
9. Runner default validation proves launcher and Codex-vs-Stockfish game-playing paths resolve `gpt-5.3-codex` with effort `high` from `chess-harness.config.json` or fallback defaults.
10. Browser E2E verifies:
   - 64 board squares render.
   - Bot Thinking has fixed height and includes move number text.
   - Engine Analysis appears in the left column and shows the analysis engine name.
   - Matches appears below Leaderboard, shows in-progress and completed status rows, and paginates to 5 rows.
   - Clicking completed matches loads the archived game and matching bot logs.
   - Bot Thinking does not auto-refresh while replaying or viewing archived games.
   - Completed winner text uses `<player> (<colour>) won`.
   - Active games show a tournament slug and a copy control that copies the full absolute viewer URL.
   - The browser URL hash updates to the active match slug for live games and to a stable `--game-N` hash for replay or archived match selections.
   - The active archived match row has a copy icon that copies its absolute viewer URL without changing the selected match.
   - Bot Thinking type filters default to only `Comment`, can select multiple message kinds, let `All` toggle all-selected to none-selected, survive reload through `localStorage`, and update board and learner log counts.
   - The flip-board control above Leaderboard reverses board coordinates, preserves exactly 64 board squares, keeps the side-to-move board state intact, and survives reload through `localStorage`.
   - Previous Matches rows show the date on the first line, then the winner on the next line, without adding row height.
   - Concurrent FastChess status rows show `In progress` without switching the fixed live board away from the selected live game.
   - Clicking an in-progress row intentionally requests that live game as the followed board while non-clicked concurrent updates do not flicker the board.
   - `Engine Config` starts collapsed and can be reopened without losing structured/raw config behavior.
   - The viewer launched by the FastChess wrapper advertises hot reload through `/api/viewer-version`, restarts after a viewer source/config/doc change, and the browser reloads to the updated UI.
   - Live mirrored PGNs contain `[%clk ...]` comments on completed moves when FastChess clock state is available.
   - When a live mirrored clock reaches zero, the board shows the winner and `lost on time`, the active clock stops at zero, and no additional LLM turn is requested by the timed-out engine.
   - An explicit completed live selection stays on the selected game, while an unpinned/default live board can advance to the next in-progress FastChess game.
   - Loading the exact live `#slug` URL keeps the viewer in Follow Live instead of selecting a completed archived game with the same tournament slug.
   - Repeated opening lines across separate games remain separate live mirror tracks and the selected board game uses the latest matching game track.
   - Loading a stale `#old-live-slug--game-N` URL without a matching archive row keeps the current live game in Follow Live.
   - Engine log collection for a live mirror is scoped to the current run start so older runs cannot provide the displayed board clock.
   - Live mirror timeout inference uses wall-clock elapsed time from `ClockUpdatedAtEpochMs` and marks the game complete in PGN and live status JSON when the displayed clock reaches zero.
   - Loading a completed or stale bare live `#slug` URL switches the viewer hash and board to the newest fresh in-progress live match.
   - A stale FastChess launch stdout is reconciled with the real `*.pgn` output and active engine logs so the live board advances to the current in-progress game.
   - Stale live mirror daemon outputs expire from the live match list instead of advertising old slugs as active.
   - An unfinished current game is allowed to move from stale locked repeated-opening moves to the current active track when older tracks have fallen out of the log window.
   - Loading an explicit archived `#slug--game-N` URL still opens that archived game when a matching completed row exists.
   - Clicking or following an in-progress match updates the browser hash and active copy URL to the game-specific live hash `#slug--live-game-N`.
   - Live mirrored board PGNs are per-game files whose URL slug timestamp matches the selected game's start timestamp rather than the overall run timestamp.
   - A bold current-game header at the top of the page shows the game number and engines currently playing.
   - Pressing left or right from an active live game disables Follow Live but keeps the current-game PGN, `--live-game-N` hash, tournament slug, and engine labels.
   - Selecting an older archived game while Follow Live is off moves the board and subsequent arrow navigation to that game instead of continuing to navigate the previously displayed game.
   - The learner material-safety audit flags queen-for-minor and rook/queen hanging moves such as `Qxd4` when an immediate legal reply can capture the moved queen or cause a large material swing.
   - `Codex-chess-zero` starts as a separate UCI engine name with its own context path and fast zero-mode prompt settings, and the FastChess/autolearn wrappers can target Zero without writing to learner memory.
   - UCI output sanitization proves non-ASCII model comments do not raise and do not block `bestmove`.
   - Codex move-time budgeting is covered by focused tests for the initial, mid-clock, and critical-clock budgets.
   - Timeout/invalid retry handling is covered by focused tests proving retries use an urgent context-free prompt and bounded retry budget while preserving the 5+0 FastChess time control.
   - No horizontal overflow at desktop width.
11. Focused engine tests verify non-urgent move requests require a non-empty comment while urgent retries can still use an empty comment.
12. Focused autolearn tests verify unchanged lesson summaries preserve their previous generated timestamp.
13. Focused autolearn tests verify unchanged `MEMORY.md` autolearn content does not rewrite only the `Last updated` value.
14. Focused viewer tests verify the push update stream advertises game, stats, learner, and viewer-version changes, and browser E2E proves the viewer uses the stream without continuing fixed game/stats/learner polling when the stream is connected.
15. Focused engine tests verify fast learner training caps prompt payload size and uses configured lower per-turn effort for learner, critical-clock, and Zero moves without changing the default model or fallback boundary.
16. Focused autolearn tests verify deferred concept synthesis preserves pending evidence during watch cycles and a later final pass can synthesize from that pending evidence.
17. A learner-improvement proof run writes JSON/Markdown artifacts showing the exact before/after prompts, model, expected legal moves, observed UCI moves, score delta, and pass/fail verdict.
18. Focused runner tests verify default `player1`/`player2` resolution, legacy `-LearningEngine zero` compatibility, custom player ordering, conflict handling, and PowerShell parser validity.
19. Focused runner tests verify `-p` resolves to `Concurrency` and is not ambiguous with `Player1` or `Player2`.
20. Focused mirror tests verify an inferred timeout in one concurrent game does not keep the live board pinned away from the next running game.
21. Focused viewer tests verify a missing run-level control PGN resolves to the status sidecar's current board PGN.
22. Focused Zero research tests verify board-plane encoding, legal-move masking, policy/value evaluation, PUCT value backup, self-play record schema, replay-buffer dedupe, training updates, promotion gates, and exact-FEN lesson rejection.
23. Regression tests verify Stockfish, Lc0, Maia, tablebases, opening books, and human-game move imitation are allowed only as evaluation/reference metadata and never enter Zero training labels or learner prompts.
24. Focused UCI tests verify enabled Zero PUCT search returns a legal move without starting Codex app-server and forfeits with `0000` if the local Zero engine fails.
25. Focused viewer tests verify the Research screen renders network identity, benchmark ladder rows, promotion status, and anti-memorization status without horizontal overflow.
26. Focused Zero tests verify deliberative reasoning output includes candidate generation buckets, plan intent, calculation/refutation notes, selected-move consistency, and a calculation-support role for PUCT instead of presenting Zero as a pure AlphaZero/Lc0 clone.
27. Focused autolearn tests verify learner/Zero self-extension writes engine-local Agent Skills and tool artifacts from generalized concepts, rejects exact FEN/move-answer content, and keeps generated artifacts out of move-selection training labels unless they are current-position feature tools.
28. Focused climb tests verify weak-stage evaluation, stage advancement, persisted climb state, self-play-only training on gate failure, and Stockfish evaluation-only flags.
29. Focused viewer tests verify `/api/research` includes persisted climb state and the Research page can render climb progress separately from the live match tracker.
30. Focused autolearn/viewer tests verify persisted Zero strategy lesson JSON stays anti-memorization safe after self-play evidence is collected.
31. Focused Zero climb tests verify failed-gate training records replay-add/duplicate-self-play diagnostics and keeps all external-label training-source flags false.
32. Focused Zero self-play tests verify seeded exploration can choose from Zero's own legal candidate list and records whether a move was greedy or exploratory.
33. Focused Zero self-play tests verify non-terminal capped games can produce self-play-only material outcome signals without external labels.
34. Focused replay-buffer tests verify stale duplicate positions with zero outcome can be updated by later Zero-only outcome signals without admitting external labels.
35. Focused Zero self-play tests verify drawn self-play is treated as a non-winning signal without using external labels.
36. Focused Zero wisdom tests verify failed cycles write readable lesson deltas with evidence counts, promotion status, and forbidden external source flags set false.
37. Focused Zero self-play tests verify material-aware draw outcomes penalize the side that failed to convert a large material edge while keeping all draw outcomes non-winning and self-play-only.
38. Focused Zero training tests verify risky forcing non-wins get stronger local feature penalties than quiet non-wins without using external labels.
39. Focused Zero deliberative tests verify risky forcing moves are downgraded when local reply scans mark them watch/unsafe.
40. Focused Zero training tests verify replay training selection is bounded, deterministic, and preserves recent plus high-signal self-play records.
41. Focused Zero deliberative tests verify expensive refutation scans are bounded to the current-position candidate shortlist.
42. Focused Zero deliberative tests verify quiet material-up conversion stalls score below low-risk conversion progress.
43. Focused replay-buffer tests verify duplicate self-play positions can update to stronger same-direction outcome signals without replacing opposite-sign outcomes or importing external labels.
44. Focused loop-runner tests verify the repo-local Zero climb loop builds bounded round commands, persists state/log rows, and refuses overlapping loop instances.
45. Focused watchdog tests verify the automation helper detects existing loop processes, builds the detached loop command, and can run in dry-run mode without starting training.
46. Automation TOML validation verifies the live and checked-in `zero-stockfish-climb` configs use the same sub-30-minute reporter cadence, prompt, active status, and low reasoning effort.
47. Focused viewer tests verify completed stats collection does not parse full PGN movetext, live depth-match pages render board updates without waiting for stats, and Engine Analysis remains available without delaying the board render.
