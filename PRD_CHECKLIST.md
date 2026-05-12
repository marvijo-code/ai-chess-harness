# PRD Checklist

- [x] Create PRD and checklist for the viewer and engine-boundary work.
- [x] Keep engine prompts free of Stockfish analysis, fallback moves, client-picked shortcuts, and `repetition_risk` fields.
- [x] Move Engine Analysis to the left column, rename it from Stockfish Analysis, and show the selected analysis engine name.
- [x] Make Bot Thinking fixed height and show the move number for live/replay log context.
- [x] Add Previous Matches below Leaderboard with 5-row pagination.
- [x] Make Previous Matches rows clickable so archived games load in the board viewer with matching bot logs.
- [x] Keep Bot Thinking steady during non-live replay/archived viewing until the user changes move, match, or filter.
- [x] Show completed game winners as `<player> (<colour>) won`.
- [x] Update README and chess-harness skill guidance for the new UI and engine-boundary contract.
- [x] Validate compile/parser/skill checks and browser E2E behavior.
- [x] Update durable memory, commit, and push.
