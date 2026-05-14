# Live Match Lessons

Generated: 2026-05-14 17:19:35
Completed games: 9 / 100
Learner score: 3.5 (38.89%)

## Result Shape
- mate: 9

## Durable Move Rules
- Copy `uci` exactly from `legal_moves`; never output `0000` while legal moves exist.
- The main failure mode is repeated positions: avoid moves listed as repetition risks unless a draw is the only safe result.
- A 15-ply threefold loop is not learning; choose a legal capture, check, pawn break, or development move that changes the position when available.
- Manage the clock while still choosing a move intentionally; there is no fallback or client-picked move.
- In winning endings, convert material with forcing moves and pawn promotion plans; do not shuffle the king until the clock collapses.

## Repeated Lines To Avoid
- 2 games, score 1.0: 1. e4 c5 2. Nf3 Nc6 3. Bb5 a6 4. Ba4 b5 5. O-O Bb7
- 2 games, score 0.5: 1. e4 e5 2. Nf3 Nc6 3. Bb5 a6 4. Ba4 Nf6 5. O-O Be7
- 2 games, score 0.5: 1. e4 e5 2. Nf3 Nc6 3. Bc4 Nf6 4. O-O Bc5 5. c3 O-O
- 1 games, score 1.0: 1. e4 c5 2. Nf3 Nc6 3. Bb5 Nf6 4. O-O e5 5. Nc3 Be7
- 1 games, score 0.5: 1. e4 e5 2. Nf3 Nc6 3. Bc4 Nf6 4. O-O Bc5 5. d3 O-O
- 1 games, score 0.0: 1. e4 e5 2. Nf3 Nc6 3. d4 exd4 4. Nxd4 Nf6 5. Nc3 Bb4

## Learner Mate Losses
- Game 1 as Black: White mates after 35 plies; 1. e4 e5 2. Nf3 Nc6 3. Bc4 Nf6 4. O-O Bc5 5. c3 O-O
- Game 6 as White: Black mates after 42 plies; 1. e4 e5 2. Nf3 Nc6 3. Bb5 a6 4. Ba4 Nf6 5. O-O Be7
- Game 8 as White: Black mates after 28 plies; 1. e4 e5 2. Nf3 Nc6 3. d4 exd4 4. Nxd4 Nf6 5. Nc3 Bb4
