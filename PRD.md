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

## Validation Requirements

1. Python files compile.
2. PowerShell wrappers parse.
3. The `chess-harness-codex` source and installed skills validate.
4. Browser E2E verifies:
   - 64 board squares render.
   - Bot Thinking has fixed height and includes move number text.
   - Engine Analysis appears in the left column and shows the analysis engine name.
   - Previous Matches appears below Leaderboard and paginates to 5 rows.
   - Clicking Previous Matches loads the archived game and matching bot logs.
   - Bot Thinking does not auto-refresh while replaying or viewing archived games.
   - Completed winner text uses `<player> (<colour>) won`.
   - No horizontal overflow at desktop width.
