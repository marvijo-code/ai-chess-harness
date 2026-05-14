# Live Match Lessons

Generated: 2026-05-13 12:27:31
Completed games: 2 / 10
Learner score: 0.0 (0.0%)

## Result Shape
- illegal move: 1
- white's connection stalls: 1

## Durable Move Rules
- Copy `uci` exactly from `legal_moves`; never output `0000` while legal moves exist.
- The main failure mode is repeated positions: avoid moves listed as repetition risks unless a draw is the only safe result.
- A 15-ply threefold loop is not learning; choose a legal capture, check, pawn break, or development move that changes the position when available.
- Manage the clock while still choosing a move intentionally; there is no fallback or client-picked move.
- In winning endings, convert material with forcing moves and pawn promotion plans; do not shuffle the king until the clock collapses.

## Repeated Lines To Avoid
- 1 games, score 0.0: 1. e4 c5 2. Nf3 d6 3. d4 cxd4 4. Nxd4 Nf6 5. Nc3 Nc6
- 1 games, score 0.0: 1. e4 e5 2. Nf3 Nc6 3. Bc4 Nf6 4. O-O Bc5 5. c3 O-O

## Learner Illegal-Move Losses
- A `0000` illegal move after real plies usually means the model timed out or returned invalid JSON three consecutive times. Treat it as a clock/format failure, not a chess tactic.
- Game 1 as Black: Black makes an illegal move after 17 plies; 1. e4 e5 2. Nf3 Nc6 3. Bc4 Nf6 4. O-O Bc5 5. c3 O-O
