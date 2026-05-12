# Chess Live Viewer PRD

## Goal

Keep the local FastChess viewer useful during live play and replay without leaking analysis or client shortcuts into engine move selection.

## Product Requirements

0. Future non-trivial chess-harness tasks should update `PRD.md` and `PRD_CHECKLIST.md` before implementation and keep checklist status current while work proceeds.
1. The board page uses a three-column desktop layout.
2. The left column contains fixed-height `Bot Thinking` and `Engine Analysis`.
3. `Bot Thinking` shows observable prompt/comment/bestmove logs, syncs to the replayed move when Follow Live is off, and includes the move number for the selected move/log context.
4. `Engine Analysis` replaces `Stockfish Analysis`, shows the selected local engine name, stays viewer-only, and must never be sent to Codex-chess or Codex-chess-learner prompts.
5. The right column shows `Leaderboard`, then `Previous Matches`, then moves and config.
6. `Previous Matches` lists completed games below the leaderboard and paginates to 5 matches per page.
7. Clicking a `Previous Matches` row loads that archived game into the board viewer, including its move list, result, analysis position, and matching bot logs.
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

## Validation Requirements

1. Python files compile.
2. PowerShell wrappers parse.
3. The `chess-harness-codex` source and installed skills validate.
4. FEN curriculum offline validation proves the 50 hidden-answer questions are generated, graded, and written to learner output files without calling Codex.
5. Config validation proves PowerShell and Python runners read defaults from `chess-harness.config.json`, and direct Codex model preflight succeeds without Windows shim launch errors.
6. Browser E2E verifies:
   - 64 board squares render.
   - Bot Thinking has fixed height and includes move number text.
   - Engine Analysis appears in the left column and shows the analysis engine name.
   - Previous Matches appears below Leaderboard and paginates to 5 rows.
   - Clicking Previous Matches loads the archived game and matching bot logs.
   - Bot Thinking does not auto-refresh while replaying or viewing archived games.
   - Completed winner text uses `<player> (<colour>) won`.
   - Active games show a tournament slug and a copy control that copies the full absolute viewer URL.
   - The browser URL hash updates to the active match slug for live games and to a stable `--game-N` hash for replay or archived match selections.
   - The active archived match row has a copy icon that copies its absolute viewer URL without changing the selected match.
   - Live mirrored PGNs contain `[%clk ...]` comments on completed moves when FastChess clock state is available.
   - No horizontal overflow at desktop width.
