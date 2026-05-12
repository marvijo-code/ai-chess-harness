# Live Match Lessons

Generated: 2026-05-12 09:51:32
Completed games: 3 / 6
Learner score: 0.5 (16.67%)

## Result Shape
- illegal move: 1
- mate: 1
- time: 1

## Durable Move Rules
- Copy `uci` exactly from `legal_moves`; never output `0000` while legal moves exist.
- The main failure mode is repeated positions: avoid moves listed as repetition risks unless a draw is the only safe result.
- A 15-ply threefold loop is not learning; choose a legal capture, check, pawn break, or development move that changes the position when available.
- Under 30 seconds, stop explaining and choose a simple legal move immediately.
- In winning endings, convert material with forcing moves and pawn promotion plans; do not shuffle the king until the clock collapses.

## Repeated Lines To Avoid
- 1 games, score 0.0: 1. Nf3 Nf6 2. d4 d5 3. c4 dxc4 4. e3 Nc6 5. Bxc4 e6
- 1 games, score 0.5: 1. Nf3 Nf6 2. d4 d5 3. c4 dxc4 4. e3 c3 5. bxc3 e6
- 1 games, score 0.0: 1. Nf3 Nf6 2. d4 d5 3. c4 dxc4 4. e3 e6 5. Bxc4 c5

## Learner Illegal-Move Losses
- Game 3 as Black: Black makes an illegal move after 61 plies; 1. Nf3 Nf6 2. d4 d5 3. c4 dxc4 4. e3 Nc6 5. Bxc4 e6

## Learner Time Losses
- Time losses are move-selection failures. Below 25 seconds, return strict JSON immediately with any clearly legal non-repetition move; do not search for perfection.
- Prefer a forcing capture, check, passed-pawn push, king move toward passed pawns, or simple recapture that is copied exactly from `legal_moves`.
- Game 2 as White: White loses on time after 40 plies; 1. Nf3 Nf6 2. d4 d5 3. c4 dxc4 4. e3 e6 5. Bxc4 c5
