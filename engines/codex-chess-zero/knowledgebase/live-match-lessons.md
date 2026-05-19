# Live Match Lessons

Generated: 2026-05-19 18:28:14
Completed games: 36 / 100
Learner score: 26.0 (74.29%)

## Result Shape
- mate: 20
- threefold repetition: 14
- illegal move: 1
- normal: 1

## Durable Move Rules
- Copy `uci` exactly from `legal_moves`; never output `0000` while legal moves exist.
- The main failure mode is repeated positions: avoid moves listed as repetition risks unless a draw is the only safe result.
- A 15-ply threefold loop is not learning; choose a legal capture, check, pawn break, or development move that changes the position when available.
- Manage the clock while still choosing a move intentionally; there is no fallback or client-picked move.
- In winning endings, convert material with forcing moves and pawn promotion plans; do not shuffle the king until the clock collapses.

## Repeated Lines To Avoid
- 6 games, score 6.0: 1. e4 e5 2. Nf3 Bb4 3. c3 Ba5 4. d4 exd4 5. Qxd4 Nc6
- 5 games, score 3.5: 1. e4 e5 2. Nf3 Bb4 3. c3 Ba5 4. Bc4 Nc6 5. O-O Nh6
- 4 games, score 3.0: 1. Nc3 e5 2. e4 Nf6 3. Bb5 c6 4. Ba4 d5 5. exd5 Nbd7
- 3 games, score 2.0: 1. Nc3 d5 2. d4 Nf6 3. Bg5 e6 4. Bxf6 gxf6 5. Nf3 Bg7
- 3 games, score 2.0: 1. e4 e5 2. Nf3 Bb4 3. c3 Ba5 4. d4 exd4 5. Nxd4 Ne7
- 1 games, score 0.5: 1. Nc3 Nf6 2. d4 d5 3. Bg5 e6 4. Bxf6 gxf6 5. Nf3 Bg7
- 1 games, score 1.0: 1. Nc3 Nf6 2. d4 d5 3. Bg5 e6 4. Bxf6 gxf6 5. Nf3 Nc6
- 1 games, score 0.5: 1. Nc3 d5 2. Nf3 Nf6 3. Ne5 c5 4. Nb5 a6 5. Na3 e6

## Learner Mate Losses
- Game 17 as Black: White mates after 23 plies; 1. e4 e5 2. Nf3 Bb4 3. c3 Ba5 4. d4 Nc6 5. Bc4 exd4
