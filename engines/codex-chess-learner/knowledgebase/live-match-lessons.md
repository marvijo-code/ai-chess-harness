# Live Match Lessons

Generated: 2026-05-12 13:06:06
Completed games: 4 / 10
Learner score: 0.5 (12.5%)

## Result Shape
- illegal move: 2
- mate: 1
- threefold repetition: 1

## Durable Move Rules
- Copy `uci` exactly from `legal_moves`; never output `0000` while legal moves exist.
- The main failure mode is repeated positions: avoid moves listed as repetition risks unless a draw is the only safe result.
- A 15-ply threefold loop is not learning; choose a legal capture, check, pawn break, or development move that changes the position when available.
- Manage the clock while still choosing a move intentionally; there is no fallback or client-picked move.
- In winning endings, convert material with forcing moves and pawn promotion plans; do not shuffle the king until the clock collapses.

## Repeated Lines To Avoid
- 1 games, score 0.0: 1. Nf3 Nf6 2. d4 d5 3. c4 dxc4 4. e3 e6 5. Bxc4 c5
- 1 games, score 0.0: 1. Nf3 Nf6 2. d4 d5 3. c4 e6 4. Nc3 Be7 5. Bf4 O-O
- 1 games, score 0.5: 1. Nf3 d5 2. d4 Nf6 3. c4 e6 4. Nc3 Be7 5. Bg5 O-O
- 1 games, score 0.0: 1. e4 e5 2. Nf3 Nc6 3. Bc4 Nf6 4. O-O Bc5 5. c3 O-O

## Learner Illegal-Move Losses
- A `0000` illegal move after real plies usually means the model timed out or returned invalid JSON three consecutive times. Treat it as a clock/format failure, not a chess tactic.
- Game 1 as Black: Black makes an illegal move after 125 plies; 1. Nf3 Nf6 2. d4 d5 3. c4 dxc4 4. e3 e6 5. Bxc4 c5
- Game 4 as White: White makes an illegal move after 108 plies; 1. e4 e5 2. Nf3 Nc6 3. Bc4 Nf6 4. O-O Bc5 5. c3 O-O

## Learner Mate Losses
- Game 3 as Black: White mates after 43 plies; 1. Nf3 Nf6 2. d4 d5 3. c4 e6 4. Nc3 Be7 5. Bf4 O-O
