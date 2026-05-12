# Live Match Lessons

Generated: 2026-05-12 15:08:12
Completed games: 2 / 100
Learner score: 0.0 (0.0%)

## Result Shape
- time: 2

## Durable Move Rules
- Copy `uci` exactly from `legal_moves`; never output `0000` while legal moves exist.
- The main failure mode is repeated positions: avoid moves listed as repetition risks unless a draw is the only safe result.
- A 15-ply threefold loop is not learning; choose a legal capture, check, pawn break, or development move that changes the position when available.
- Manage the clock while still choosing a move intentionally; there is no fallback or client-picked move.
- In winning endings, convert material with forcing moves and pawn promotion plans; do not shuffle the king until the clock collapses.

## Repeated Lines To Avoid
- 1 games, score 0.0: 1. e4 c5 2. Nf3 Nc6 3. Bb5 e6 4. O-O Nge7 5. d4 cxd4
- 1 games, score 0.0: 1. e4 e5 2. Nf3 Nc6 3. Bc4 Nf6 4. O-O Bc5 5. c3 O-O

## Learner Time Losses
- Time losses are move-selection failures. Improve time management, but still choose a move intentionally from the position.
- Prefer a forcing capture, check, passed-pawn push, king move toward passed pawns, or simple recapture when that is the best evaluated plan.
- Game 1 as Black: Black loses on time after 21 plies; 1. e4 e5 2. Nf3 Nc6 3. Bc4 Nf6 4. O-O Bc5 5. c3 O-O
- Game 2 as White: White loses on time after 18 plies; 1. e4 c5 2. Nf3 Nc6 3. Bb5 e6 4. O-O Nge7 5. d4 cxd4
