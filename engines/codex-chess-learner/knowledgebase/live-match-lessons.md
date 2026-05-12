# Live Match Lessons

Generated: 2026-05-12 08:16:29
Completed games: 52 / 200
Learner score: 26.0 (50.0%)

## Result Shape
- threefold repetition: 31
- illegal move: 20
- mate: 1

## Durable Move Rules
- Copy `uci` exactly from `legal_moves`; never output `0000` while legal moves exist.
- The main failure mode is repeated positions: avoid moves listed as repetition risks unless a draw is the only safe result.
- A 15-ply threefold loop is not learning; choose a legal capture, check, pawn break, or development move that changes the position when available.
- Under 30 seconds, stop explaining and choose a simple legal move immediately.
- In winning endings, convert material with forcing moves and pawn promotion plans; do not shuffle the king until the clock collapses.

## Repeated Lines To Avoid
- 31 games, score 15.5: 1. Nh3 Nh6 2. Ng5 Rg8 3. Nxh7 Rh8 4. Nxf8 Rg8 5. Nh7 Rh8
- 20 games, score 10.0: [empty line]
- 1 games, score 0.5: 1. Nf3 Nf6 2. Nc3 Nc6 3. e4 Nxe4 4. Nxe4 e5 5. Nxe5 Nxe5

## Learner Illegal-Move Losses
- Game 34 as White: White makes an illegal move after 0 plies; 
- Game 36 as White: White makes an illegal move after 0 plies; 
- Game 38 as White: White makes an illegal move after 0 plies; 
- Game 40 as White: White makes an illegal move after 0 plies; 
- Game 42 as White: White makes an illegal move after 0 plies; 
- Game 44 as White: White makes an illegal move after 0 plies; 
- Game 46 as White: White makes an illegal move after 0 plies; 
- Game 48 as White: White makes an illegal move after 0 plies; 
- Game 50 as White: White makes an illegal move after 0 plies; 
- Game 52 as White: White makes an illegal move after 0 plies; 
