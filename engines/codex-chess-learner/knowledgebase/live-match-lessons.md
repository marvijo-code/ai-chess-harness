# Live Match Lessons

Generated: 2026-05-12 10:30:20
Completed games: 5 / 6
Learner score: 3.5 (70.0%)

## Result Shape
- mate: 4
- threefold repetition: 1

## Durable Move Rules
- Copy `uci` exactly from `legal_moves`; never output `0000` while legal moves exist.
- The main failure mode is repeated positions: avoid moves listed as repetition risks unless a draw is the only safe result.
- A 15-ply threefold loop is not learning; choose a legal capture, check, pawn break, or development move that changes the position when available.
- Manage the clock while still choosing a move intentionally; there is no fallback or client-picked move.
- In winning endings, convert material with forcing moves and pawn promotion plans; do not shuffle the king until the clock collapses.

## Repeated Lines To Avoid
- 4 games, score 3.0: 1. Nf3 Nf6 2. d4 d5 3. c4 dxc4 4. e3 e6 5. Bxc4 c5
- 1 games, score 0.5: 1. Nf3 d5 2. d4 Nf6 3. c4 e6 4. Nc3 Be7 5. Bf4 O-O

## Learner Mate Losses
- Game 4 as White: Black mates after 42 plies; 1. Nf3 Nf6 2. d4 d5 3. c4 dxc4 4. e3 e6 5. Bxc4 c5
