# Live Match Lessons

Generated: 2026-05-18 11:27:58
Completed games: 13 / 100
Learner score: 6.0 (46.15%)

## Result Shape
- illegal move: 6
- threefold repetition: 4
- mate: 3

## Durable Move Rules
- Copy `uci` exactly from `legal_moves`; never output `0000` while legal moves exist.
- The main failure mode is repeated positions: avoid moves listed as repetition risks unless a draw is the only safe result.
- A 15-ply threefold loop is not learning; choose a legal capture, check, pawn break, or development move that changes the position when available.
- Manage the clock while still choosing a move intentionally; there is no fallback or client-picked move.
- In winning endings, convert material with forcing moves and pawn promotion plans; do not shuffle the king until the clock collapses.

## Repeated Lines To Avoid
- 4 games, score 2.0: 1. e4 e5 2. Nf3 Nc6 3. Bb5 a6 4. Ba4 Nf6 5. O-O Be7
- 3 games, score 0.5: 1. e4 e5 2. Nf3 Nc6 3. Bc4 Nf6 4. O-O Bc5 5. d3 O-O
- 1 games, score 0.5: 1. e4 c5 2. Nf3 Nc6 3. Bb5 a6 4. Ba4 b5 5. Bb3 Nf6
- 1 games, score 1.0: 1. e4 c5 2. Nf3 Nc6 3. d4 cxd4 4. c3 dxc3 5. bxc3 Nf6
- 1 games, score 1.0: 1. e4 e5 2. Nf3 Nc6 3. Bc4 Nf6 4. O-O Be7 5. Nc3 O-O
- 1 games, score 1.0: 1. e4 e5 2. Nf3 Nc6 3. Bc4 Nf6 4. O-O Be7 5. Re1 O-O
- 1 games, score 0.0: 1. e4 e5 2. Nf3 Nc6 3. Bc4 Nf6 4. O-O Be7 5. d3 O-O
- 1 games, score 0.0: 1. e4 e5 2. Nf3 Nc6 3. Nc3 Nf6 4. Bc4 Bc5 5. O-O O-O

## Learner Illegal-Move Losses
- A `0000` illegal move after real plies usually means the model timed out or returned invalid JSON three consecutive times. Treat it as a clock/format failure, not a chess tactic.
- Game 5 as Black: Black makes an illegal move after 47 plies; 1. e4 e5 2. Nf3 Nc6 3. Bb5 a6 4. Ba4 Nf6 5. O-O Be7
- Game 6 as White: White makes an illegal move after 68 plies; 1. e4 e5 2. Nf3 Nc6 3. Bc4 Nf6 4. O-O Be7 5. d3 O-O
- Game 9 as White: White makes an illegal move after 131 plies; 1. e4 e5 2. Nf3 Nc6 3. Bc4 Nf6 4. O-O Bc5 5. d3 O-O

## Learner Mate Losses
- Game 11 as Black: White mates after 67 plies; 1. e4 e5 2. Nf3 Nc6 3. Bc4 Nf6 4. O-O Bc5 5. d3 O-O
- Game 12 as White: Black mates after 88 plies; 1. e4 e5 2. Nf3 Nc6 3. Bc4 Nf6 4. O-O Bc5 5. d3 O-O
