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
42. A live selection file pins the board only while that selected game is still in progress; once the selected game is completed and another FastChess game is running, the live mirror should advance to the next in-progress game instead of leaving the board on a stale completed or timeout state.
43. A bare live URL hash such as `#codex-vs-codex-learner-live-...` must keep Follow Live enabled for that live PGN; archived or replayed games require an explicit `--game-N` hash suffix.
44. The live mirror must keep repeated opening lines from separate FastChess games as separate engine tracks, so a later game that starts with the same moves does not inherit stale clocks or positions from an earlier game.
45. A stale or non-matching archived hash such as `#old-live-slug--game-1` must not leave the viewer in replay mode against the current live PGN; Follow Live should remain on until the matching archived row is found.
46. The FastChess live mirror must ignore engine logs older than the current run's launch stdout file, so earlier runs cannot make a new live board show stale positions or clock timeouts while the current game is still underway.
47. If a live mirrored clock expires by wall-clock elapsed time after the last engine-log clock update, the mirror must write the timeout result into both the live PGN and live status JSON even when FastChess stdout has not printed `Finished game`.
48. A completed or stale bare live URL hash must resolve to the newest fresh in-progress live match even when that active match slug sorts before the stale hash, while explicit `#slug--game-N` links stay archived replay links.
49. The live mirror must reconcile stale FastChess launch stdout with the real FastChess `*.pgn` output and current engine tracks, must not pin an unfinished current game to stale locked moves from an older repeated-opening track, and stale daemon mirrors must stop refreshing live status when their own run artifacts are no longer fresh.
50. FastChess learner runs stay at 5+0 by default, but Codex move budgeting must avoid self-inflicted timeouts: normal mid-clock moves get enough app-server wait budget, and retry prompts after a timeout or invalid move must drop learner context, drop comments, and use a lower-effort urgent turn instead of restarting multiple full-context high-effort turns that burn the clock.
51. If FastChess still reports a single unfinished game while current-run engine logs contain multiple move-line tracks, the live mirror must show the freshest active track for that game instead of keeping an older stale track whose wall-clock timeout would make the board look completed while engines keep playing.

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
   - A stale completed live selection advances to the next in-progress FastChess game while an unfinished selected game remains fixed.
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
   - UCI output sanitization proves non-ASCII model comments do not raise and do not block `bestmove`.
   - Codex move-time budgeting is covered by focused tests for the initial, mid-clock, and critical-clock budgets.
   - Timeout/invalid retry handling is covered by focused tests proving retries use an urgent context-free prompt and bounded retry budget while preserving the 5+0 FastChess time control.
   - No horizontal overflow at desktop width.
