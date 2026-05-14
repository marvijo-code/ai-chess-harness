# Live Match Lessons

Generated: 2026-05-14 16:02:39
Completed games: 17 / 100
Learner score: 7.5 (44.12%)

## Result Shape
- mate: 9
- threefold repetition: 4
- time: 3
- normal: 1

## Durable Move Rules
- Copy `uci` exactly from `legal_moves`; never output `0000` while legal moves exist.
- The main failure mode is repeated positions: avoid moves listed as repetition risks unless a draw is the only safe result.
- A 15-ply threefold loop is not learning; choose a legal capture, check, pawn break, or development move that changes the position when available.
- Manage the clock while still choosing a move intentionally; there is no fallback or client-picked move.
- In winning endings, convert material with forcing moves and pawn promotion plans; do not shuffle the king until the clock collapses.

## Repeated Lines To Avoid
- 4 games, score 2.5: 1. e4 c5 2. Nf3 d6 3. d4 cxd4 4. Nxd4 Nf6 5. Nc3 Nc6
- 2 games, score 0.0: 1. e4 e5 2. Nf3 Nc6 3. Bb5 Nf6 4. O-O Be7 5. Re1 O-O
- 2 games, score 1.5: 1. e4 e5 2. Nf3 Nc6 3. Bc4 Nf6 4. O-O Bc5 5. c3 O-O
- 1 games, score 0.5: 1. Nf3 d5 2. d4 Nf6 3. c4 c6 4. Nc3 e6 5. Bg5 Be7
- 1 games, score 0.5: 1. Nf3 d5 2. d4 Nf6 3. c4 dxc4 4. Qa4+ b5 5. Qxb5+ c6
- 1 games, score 0.5: 1. Nf3 d5 2. d4 Nf6 3. c4 e6 4. Nc3 Bb4 5. e3 O-O
- 1 games, score 0.5: 1. Nf3 d5 2. d4 Nf6 3. c4 e6 4. Nc3 Be7 5. Bg5 O-O
- 1 games, score 0.5: 1. Nf3 d5 2. d4 Nf6 3. c4 e6 4. Nc3 c5 5. cxd5 exd5

## Learner Mate Losses
- Game 6 as White: Black mates after 48 plies; 1. e4 e5 2. Nf3 Nc6 3. Bb5 Nf6 4. O-O Be7 5. Re1 O-O

## Learner Time Losses
- Time losses are move-selection failures. Improve time management, but still choose a move intentionally from the position.
- Prefer a forcing capture, check, passed-pawn push, king move toward passed pawns, or simple recapture when that is the best evaluated plan.
- Game 8 as White: White loses on time after 242 plies; 1. d4 d5 2. Nf3 Nf6 3. c4 e6 4. Nc3 Be7 5. Bg5 O-O
- Game 12 as White: White loses on time after 176 plies; 1. e4 e5 2. Nf3 Nc6 3. Bb5 Nf6 4. O-O Be7 5. Re1 O-O
- Game 16 as White: White loses on time after 206 plies; 1. e4 c5 2. Nf3 Nc6 3. Bb5 a6 4. Bxc6 bxc6 5. O-O Nf6
