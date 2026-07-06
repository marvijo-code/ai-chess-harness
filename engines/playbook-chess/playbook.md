# Playbook-chess — Human-Readable Engine Instructions

Playbook-chess reads this file before every search. The engine machine-reads only
lines shaped like `- name = value` under the sections below; the prose after each
em dash explains the evidence for a human reader and is ignored by the parser.
Everything else in this file is prose for humans.

Evidence base: 623,110 decisive games from The Week In Chess, issues 1549-1647
(local corpus under `out\twic-manual-wisdom`, theme rates from
`batch-progress.json`). Rules of this file: no exact FEN-to-move instructions,
no opening-book move lists, no external-engine variations (PRD 166).

## Search discipline

- meta.version = 37
- search.min_depth = 6 — always finish at least this depth before honoring the clock; shallow moves lose to one-ply tactics.
- search.base_movetime_ms = 24000
- search.movetime_fraction = 0.90
- search.draw_contempt = 80 — TWIC: 97.4% of decisive games contain a material swing; when we are ahead, a draw is a failed conversion, so repeated positions score negative for the side that is better.
- search.equal_draw_aversion = 30 — the depth-2 gate drew by repetition from an equal position; win-gated ladders require playing on at equality, so even an equal draw scores mildly negative.
- search.aspiration_window = 40
- search.root_variety = 19 — rotates which equal-best root move is tried first; the trainer bumps this every round so a saturated playbook still produces a different game against a deterministic fixed-depth opponent.

## Material

- material.pawn = 100
- material.knight = 320
- material.bishop = 330
- material.rook = 500
- material.queen = 900

## Mobility and piece activity

- mobility.per_square = 6 — winners restrict losers: piece scope is worth paying small material-free costs for.
- pieces.bishop_pair = 30
- pieces.rook_open_file = 30 — TWIC: seventh-rank and file invasion themes appear in 41.3% of decisive games (257,522/623,110).
- pieces.rook_semi_open_file = 10
- pieces.rook_seventh = 24 — same seventh-rank evidence as above.

## Development and castling

- development.undeveloped_minor_penalty = 14 — TWIC: a development edge shows up in 28.0% of decisive games (174,628/623,110).
- development.uncastled_penalty = 21 — winners castle and connect rooks before starting operations.
- development.castle_urgency = 14 — the depth-4 losses were pawn grabs with a central king (castled at move 27, already lost); every move past move 8 without castling costs more. TWIC: development edge decides 28.0% of games.
- development.early_queen_penalty = 13 — queen raids before castling won pawns and lost the king in the depth-4 gates; penalty scales with how far the queen has wandered while the king is unsafe.

## King safety

- king.shield_pawn = 22
- king.open_file_penalty = 39 — an open or half-open file next to the king is the highway every TWIC king attack uses.
- king.ring_attack_penalty = 22 — TWIC: king attacks decide 50.0% of games (311,632/623,110); count every enemy attack touching the king ring.
- king.tropism = 5 — TWIC: king attacks decide 50.0% of games (311,632/623,110); attacking pieces gain value standing near the enemy king, which turns drawn-level shuffling into attack building.
- king.danger_scale = 10 — depth-8 losses included king-collapse mates; danger grows with the SQUARE of accumulated attack units (piece-weighted) once >= 2 enemy pieces hit the king ring, so mating nets are foreseen a ply earlier and defended. TWIC: king attacks decide 50.0% of games.
- king.danger_cap = 250 — caps the super-linear king-danger penalty so a heavy but survivable attack cannot read as a full piece down.

## Pawns

- pawns.passed_base = 18
- pawns.passed_per_rank = 24 — TWIC: passed-pawn conversion appears in 52.0% of decisive wins (324,194/623,110); each rank of advance matters more than the last.
- pawns.doubled_penalty = 12
- pawns.isolated_penalty = 12

## Conversion (winning technique)

- conversion.edge_threshold = 250
- conversion.simplify_bonus = 9 — TWIC: queen trades while ahead appear in 18.5% of wins (115,121); trade pieces, not pawns, when up material.
- conversion.king_activity = 22 — TWIC: king activation decides 18.0% of games (111,993); in simplified positions the king is a fighting piece.
- conversion.keep_pawns = 16 — the depth-4 gate ended as a bare rook-vs-bishop book draw after trading every pawn while ahead; TWIC: passed pawns convert 52.0% of wins, and passers require keeping pawns on the board.
- conversion.greed_damping = 45 — every depth-4 loss harvested material into a mating attack; centipawns beyond the winning edge are discounted 25% so safety outbids one more pawn grab.
- conversion.progress_pressure = 2 — the depth-6 losses included 100+ move king shuffles while materially ahead; the rising 50-move clock costs the winning side (capped 50cp) so a clock-resetting pawn move or capture beats an aimless shuffle, never enough to sacrifice material.
- tempo.bonus = 12

## Principles (prose for humans and the trainer)

1. Never remove a legal move from consideration because of style. The previous
   engine (wisdom-chess) hard-filtered "unreasonable" moves and thereby became
   blind to queen captures of undefended pieces — it lost to Stockfish depth 1
   with 72 seconds per move. Preferences act only through the weights above.
2. Material first, then king safety, then activity. A clean pawn is worth more
   than a speculative attack.
3. When ahead, trade pieces, push passers, activate the king, and never repeat
   a position (draw contempt handles the search side of this).
4. When behind, keep pieces on, keep the position closed, and maximize the
   opponent's chances to go wrong.

## Training log

### 2026-07-06 — Seeded from the TWIC corpus

Initial weights authored from the 99-issue TWIC batch (1549-1647, 623,110
decisive games): king-attack rate 50.0%, passed-pawn rate 52.0%, seventh-rank
rate 41.3%, development-edge rate 28.0%, queen-trade-ahead rate 18.5%,
king-activation rate 18.0%. Search discipline seeded from the wisdom-chess
depth-1 failure analysis (no legal-move filtering, draw contempt when ahead).

### 2026-07-06 06:43 — Gate training round

Games: `playbook-vs-stockfish-depth-2-20260706-063920.pgn` (1/2-1/2, draw).

Diagnosis: repetition_draw x1. draw by threefold repetition.

Adjustments: search.draw_contempt 30 -> 40.

Evidence: TWIC issues 1549-1647, 623,110 decisive games; material_swing 606,710 games (97.4%); fresh sample (twic1647g.zip, 150 decisive games): material_swing proxy 84.7%.

### 2026-07-06 06:53 — Gate training round

Games: `playbook-vs-stockfish-depth-3-20260706-064616.pgn` (1/2-1/2, draw).

Diagnosis: failed_conversion x1, repetition_draw x1. held >= +300cp for 6+ own moves without winning; draw by threefold repetition.

Adjustments: conversion.king_activity 12 -> 14; conversion.simplify_bonus 4 -> 5; pawns.passed_per_rank 14 -> 16; search.draw_contempt 40 -> 50.

Evidence: TWIC issues 1549-1647, 623,110 decisive games; passed_pawn 324,194 games (52.0%); queen_trade_ahead 115,121 games (18.5%); king_activation 111,993 games (18.0%); material_swing 606,710 games (97.4%); fresh sample (twic1647g.zip, 150 decisive games): passed_pawn proxy 59.3%.

### 2026-07-06 07:02 — Gate training round

Games: `playbook-vs-stockfish-depth-4-20260706-065800.pgn` (0-1, loss).

Diagnosis: blunder_swing x1. eval fell -123 -> -760 around move 37.

Adjustments: search.base_movetime_ms 8000 -> 10000; search.min_depth 4 -> 5.

Evidence: TWIC issues 1549-1647, 623,110 decisive games; material_swing 606,710 games (97.4%); fresh sample (twic1647g.zip, 150 decisive games): material_swing proxy 84.7%.

### 2026-07-06 07:09 — Gate training round

Games: `playbook-vs-stockfish-depth-4-20260706-070218.pgn` (1/2-1/2, draw).

Diagnosis: repetition_draw x1. draw by threefold repetition.

Adjustments: search.draw_contempt 50 -> 60.

Evidence: TWIC issues 1549-1647, 623,110 decisive games; material_swing 606,710 games (97.4%); fresh sample (twic1647g.zip, 150 decisive games): material_swing proxy 84.7%.

### 2026-07-06 07:26 — Gate training round

Games: `playbook-vs-stockfish-depth-4-20260706-071250.pgn` (1/2-1/2, draw).

Diagnosis: blunder_swing x1, failed_conversion x1, repetition_draw x1. eval fell -2 -> -319 around move 22; held >= +300cp for 6+ own moves without winning.

Adjustments: conversion.keep_pawns 6 -> 9; conversion.king_activity 14 -> 16; conversion.simplify_bonus 5 -> 6; pawns.passed_per_rank 16 -> 18; search.base_movetime_ms 10000 -> 12000; search.draw_contempt 60 -> 70; search.equal_draw_aversion 12 -> 18; search.min_depth 5 -> 6.

Evidence: TWIC issues 1549-1647, 623,110 decisive games; material_swing 606,710 games (97.4%); passed_pawn 324,194 games (52.0%); queen_trade_ahead 115,121 games (18.5%); king_activation 111,993 games (18.0%); fresh sample (twic1647g.zip, 150 decisive games): material_swing proxy 84.7%.

### 2026-07-06 07:33 — Gate training round

Games: `playbook-vs-stockfish-depth-4-20260706-072654.pgn` (1-0, loss).

Diagnosis: blunder_swing x1, king_collapse x1. eval fell +18 -> -294 around move 41; mated shortly after a roughly level self-eval.

Adjustments: king.open_file_penalty 24 -> 27; king.ring_attack_penalty 12 -> 14; king.shield_pawn 12 -> 14; search.base_movetime_ms 12000 -> 14000.

Evidence: TWIC issues 1549-1647, 623,110 decisive games; material_swing 606,710 games (97.4%); king_attack 311,632 games (50.0%); fresh sample (twic1647g.zip, 150 decisive games): material_swing proxy 84.7%.

### 2026-07-06 07:38 — Gate training round

Games: `playbook-vs-stockfish-depth-4-20260706-073356.pgn` (1/2-1/2, draw).

Diagnosis: repetition_draw x1. draw by threefold repetition.

Adjustments: search.draw_contempt 70 -> 80; search.equal_draw_aversion 18 -> 24.

Evidence: TWIC issues 1549-1647, 623,110 decisive games; material_swing 606,710 games (97.4%); fresh sample (twic1647g.zip, 150 decisive games): material_swing proxy 84.7%.

### 2026-07-06 07:43 — Gate training round

Games: `playbook-vs-stockfish-depth-4-20260706-073805.pgn` (1-0, loss).

Diagnosis: blunder_swing x1. eval fell +86 -> -408 around move 27.

Adjustments: search.base_movetime_ms 14000 -> 16000.

Evidence: TWIC issues 1549-1647, 623,110 decisive games; material_swing 606,710 games (97.4%); fresh sample (twic1647g.zip, 150 decisive games): material_swing proxy 84.7%.

### 2026-07-06 07:59 — Gate training round

Games: `playbook-vs-stockfish-depth-4-20260706-074544.pgn` (0-1, loss).

Diagnosis: blunder_swing x1, failed_conversion x1. eval fell +668 -> -80 around move 32; held >= +300cp for 6+ own moves without winning.

Adjustments: conversion.keep_pawns 9 -> 12; conversion.king_activity 16 -> 18; conversion.simplify_bonus 6 -> 7; pawns.passed_per_rank 18 -> 20; search.base_movetime_ms 16000 -> 18000.

Evidence: TWIC issues 1549-1647, 623,110 decisive games; passed_pawn 324,194 games (52.0%); queen_trade_ahead 115,121 games (18.5%); king_activation 111,993 games (18.0%); material_swing 606,710 games (97.4%); fresh sample (twic1647g.zip, 150 decisive games): passed_pawn proxy 59.3%.

### 2026-07-06 08:09 — Gate training round

Games: `playbook-vs-stockfish-depth-4-20260706-080613.pgn` (1/2-1/2, draw).

Diagnosis: repetition_draw x1. draw by threefold repetition.

Adjustments: search.equal_draw_aversion 24 -> 30.

Evidence: TWIC issues 1549-1647, 623,110 decisive games; material_swing 606,710 games (97.4%); fresh sample (twic1647g.zip, 150 decisive games): material_swing proxy 84.7%.

### 2026-07-06 08:41 — Gate training round

Games: `playbook-vs-stockfish-depth-5-20260706-082347.pgn` (1/2-1/2, draw).

Diagnosis: blunder_swing x1, repetition_draw x1. eval fell -78 -> -351 around move 36; draw by threefold repetition.

Adjustments: conversion.greed_damping 25 -> 35; search.base_movetime_ms 18000 -> 20000.

Evidence: TWIC issues 1549-1647, 623,110 decisive games; material_swing 606,710 games (97.4%); fresh sample (twic1647g.zip, 150 decisive games): material_swing proxy 84.7%.

### 2026-07-06 08:56 — Gate training round

Games: `playbook-vs-stockfish-depth-5-20260706-084101.pgn` (1/2-1/2, draw).

Diagnosis: blunder_swing x1, repetition_draw x1. eval fell +30 -> -393 around move 52; draw by threefold repetition.

Adjustments: conversion.greed_damping 35 -> 45; search.base_movetime_ms 20000 -> 22000.

Evidence: TWIC issues 1549-1647, 623,110 decisive games; material_swing 606,710 games (97.4%); fresh sample (twic1647g.zip, 150 decisive games): material_swing proxy 84.7%.

### 2026-07-06 09:19 — Gate training round

Games: `playbook-vs-stockfish-depth-5-20260706-085639.pgn` (0-1, loss).

Diagnosis: blunder_swing x1. eval fell -203 -> -612 around move 67.

Adjustments: conversion.greed_damping 45 -> 55; search.base_movetime_ms 22000 -> 24000.

Evidence: TWIC issues 1549-1647, 623,110 decisive games; material_swing 606,710 games (97.4%); fresh sample (twic1647g.zip, 150 decisive games): material_swing proxy 84.7%.

### 2026-07-06 09:20 — Manual correction: greed damping rollback

Rolled conversion.greed_damping 55 -> 35. The 45/55 bumps were misattributed:
the depth-5 blunder swings started from LEVEL positions (tactical misses),
not from winning ones, so damping the winning margin does not address them
and 55% weakens conversion. The trainer now separates greed_blunder
(drop from >= +300, steps damping) from blunder_swing (steps depth/time only).

### 2026-07-06 09:47 — Gate training round

Games: `playbook-vs-stockfish-depth-5-20260706-091910.pgn` (1/2-1/2, draw).

Diagnosis: blunder_swing x1, repetition_draw x1. eval fell +80 -> -755 around move 62; draw by threefold repetition.

Adjustments: conversion.greed_damping 35 -> 45; search.base_movetime_ms 24000 -> 26000.

Evidence: TWIC issues 1549-1647, 623,110 decisive games; material_swing 606,710 games (97.4%); fresh sample (twic1647g.zip, 150 decisive games): material_swing proxy 84.7%.

### 2026-07-06 10:23 — Gate training round

Games: `playbook-vs-stockfish-depth-5-20260706-094700.pgn` (0-1, loss).

Diagnosis: blunder_swing x1, king_collapse x1. eval fell +80 -> -196 around move 43; mated shortly after a roughly level self-eval.

Adjustments: conversion.greed_damping 45 -> 55; development.castle_urgency 6 -> 8; king.open_file_penalty 27 -> 30; king.ring_attack_penalty 14 -> 16; king.shield_pawn 14 -> 16; search.base_movetime_ms 26000 -> 28000.

Evidence: TWIC issues 1549-1647, 623,110 decisive games; material_swing 606,710 games (97.4%); king_attack 311,632 games (50.0%); fresh sample (twic1647g.zip, 150 decisive games): material_swing proxy 84.7%.

### 2026-07-06 10:24 — Manual correction: damping re-rollback and movetime reset

conversion.greed_damping 55 -> 35 again (the pre-fix trainer re-stepped it from
level-position blunders before the greed_blunder split was loaded), and
search.base_movetime_ms 28000 -> 20000: 20s is where the depth-4 win and the
clean depth-5 draws happened; 80-minute games starve the learn-quick loop.
Trainer movetime cap lowered to 24000 so this lever saturates sanely.

### 2026-07-06 10:44 — Gate training round

Games: `playbook-vs-stockfish-depth-5-20260706-102454.pgn` (1/2-1/2, draw).

Diagnosis: blunder_swing x1, repetition_draw x1. eval fell -133 -> -552 around move 24; draw by threefold repetition.

Adjustments: search.base_movetime_ms 20000 -> 22000.

Evidence: TWIC issues 1549-1647, 623,110 decisive games; material_swing 606,710 games (97.4%); fresh sample (twic1647g.zip, 150 decisive games): material_swing proxy 84.7%.

### 2026-07-06 11:12 — Gate training round

Games: `playbook-vs-stockfish-depth-5-20260706-104457.pgn` (1/2-1/2, draw).

Diagnosis: blunder_swing x1, repetition_draw x1. eval fell +222 -> -54 around move 20; draw by threefold repetition.

Adjustments: search.base_movetime_ms 22000 -> 24000.

Evidence: TWIC issues 1549-1647, 623,110 decisive games; material_swing 606,710 games (97.4%); fresh sample (twic1647g.zip, 150 decisive games): material_swing proxy 84.7%.

### 2026-07-06 11:31 — Gate training round

Games: `playbook-vs-stockfish-depth-5-20260706-111231.pgn` (1-0, loss).

Diagnosis: blunder_swing x1. eval fell -72 -> -346 around move 49.

Adjustments: no weight headroom left inside safety bounds.

Evidence: TWIC issues 1549-1647, 623,110 decisive games; material_swing 606,710 games (97.4%); fresh sample (twic1647g.zip, 150 decisive games): material_swing proxy 84.7%.

### 2026-07-06 12:15 — Gate training round

Games: `playbook-vs-stockfish-depth-6-20260706-120145.pgn` (0-1, loss).

Diagnosis: blunder_swing x1. eval fell +0 -> -350 around move 30.

Adjustments: search.root_variety 0 -> 1.

Evidence: TWIC issues 1549-1647, 623,110 decisive games; material_swing 606,710 games (97.4%); fresh sample (twic1647g.zip, 150 decisive games): material_swing proxy 84.7%.

### 2026-07-06 12:30 — Gate training round

Games: `playbook-vs-stockfish-depth-6-20260706-121554.pgn` (1/2-1/2, draw).

Diagnosis: failed_conversion x1, repetition_draw x1. held >= +300cp for 6+ own moves without winning; draw by threefold repetition.

Adjustments: conversion.keep_pawns 12 -> 15; conversion.king_activity 18 -> 20; conversion.simplify_bonus 7 -> 8; pawns.passed_per_rank 20 -> 22; search.root_variety 1 -> 2.

Evidence: TWIC issues 1549-1647, 623,110 decisive games; passed_pawn 324,194 games (52.0%); queen_trade_ahead 115,121 games (18.5%); king_activation 111,993 games (18.0%); material_swing 606,710 games (97.4%); fresh sample (twic1647g.zip, 150 decisive games): passed_pawn proxy 59.3%.

### 2026-07-06 12:39 — Gate training round

Games: `playbook-vs-stockfish-depth-6-20260706-123045.pgn` (0-1, loss).

Diagnosis: blunder_swing x1. eval fell -133 -> -943 around move 26.

Adjustments: search.root_variety 2 -> 3.

Evidence: TWIC issues 1549-1647, 623,110 decisive games; material_swing 606,710 games (97.4%); fresh sample (twic1647g.zip, 150 decisive games): material_swing proxy 84.7%.

### 2026-07-06 13:11 — Gate training round

Games: `playbook-vs-stockfish-depth-6-20260706-123945.pgn` (1-0, loss).

Diagnosis: blunder_swing x1. eval fell +80 -> -402 around move 59.

Adjustments: search.root_variety 3 -> 4.

Evidence: TWIC issues 1549-1647, 623,110 decisive games; material_swing 606,710 games (97.4%); fresh sample (twic1647g.zip, 150 decisive games): material_swing proxy 84.7%.

### 2026-07-06 13:39 — Gate training round

Games: `playbook-vs-stockfish-depth-6-20260706-131121.pgn` (1/2-1/2, draw).

Diagnosis: blunder_swing x1, repetition_draw x1. eval fell +156 -> -102 around move 49; draw by threefold repetition.

Adjustments: search.root_variety 4 -> 5.

Evidence: TWIC issues 1549-1647, 623,110 decisive games; material_swing 606,710 games (97.4%); fresh sample (twic1647g.zip, 150 decisive games): material_swing proxy 84.7%.

### 2026-07-06 13:57 — Gate training round

Games: `playbook-vs-stockfish-depth-6-20260706-133939.pgn` (1-0, loss).

Diagnosis: slow_outplay x1. gradual decline with no single 250cp swing.

Adjustments: mobility.per_square 3 -> 4; pieces.rook_open_file 22 -> 24; search.root_variety 5 -> 6.

Evidence: TWIC issues 1549-1647, 623,110 decisive games; development_edge 174,628 games (28.0%); seventh_rank 257,522 games (41.3%); fresh sample (twic1647g.zip, 150 decisive games): development_edge proxy 84.7%.

### 2026-07-06 16:26 — Engine improvement: log-based LMR + king tropism

Search: replaced the flat 1-2 ply late-move reduction with a log(depth)*log(move)
reduction table (Stockfish-style shape), clamped so a reduced search never drops
straight into quiescence and killers reduce one less. Depth-8 startpos search fell
from ~40k to ~12k nodes (deeper reach per second at the same NPS ~24k), and the PVS
re-search keeps tactics: the Qxg6 sac and both mates in the check suite still solve.
Eval: added king.tropism (attackers gain value near the enemy king; TWIC king-attack
rate 50.0%) to convert drawn-level shuffling into attack building at the depth-6 gate.

### 2026-07-06 16:58 — Gate training round

Games: `playbook-vs-stockfish-depth-6-20260706-162651.pgn` (0-1, loss).

Diagnosis: blunder_swing x1. eval fell +80 -> -403 around move 100.

Adjustments: search.root_variety 6 -> 7.

Evidence: TWIC issues 1549-1647, 623,110 decisive games; material_swing 606,710 games (97.4%); fresh sample (twic1647g.zip, 150 decisive games): material_swing proxy 84.7%.

### 2026-07-06 17:30 — Gate training round

Games: `playbook-vs-stockfish-depth-6-20260706-165857.pgn` (1-0, loss).

Diagnosis: slow_outplay x1. gradual decline with no single 250cp swing.

Adjustments: king.tropism 2 -> 3; mobility.per_square 4 -> 5; pieces.rook_open_file 24 -> 26; search.root_variety 7 -> 8.

Evidence: TWIC issues 1549-1647, 623,110 decisive games; development_edge 174,628 games (28.0%); seventh_rank 257,522 games (41.3%); king_attack 311,632 games (50.0%); fresh sample (twic1647g.zip, 150 decisive games): development_edge proxy 84.7%.

### 2026-07-06 18:19 — Gate training round

Games: `playbook-vs-stockfish-depth-7-20260706-180303.pgn` (0-1, loss).

Diagnosis: blunder_swing x1, failed_conversion x1, greed_blunder x1. eval fell +702 -> +100 around move 20; held >= +300cp for 6+ own moves without winning.

Adjustments: conversion.greed_damping 35 -> 45; conversion.keep_pawns 15 -> 16; conversion.king_activity 20 -> 22; conversion.progress_pressure 1 -> 2; conversion.simplify_bonus 8 -> 9; pawns.passed_per_rank 22 -> 24; search.root_variety 8 -> 9.

Evidence: TWIC issues 1549-1647, 623,110 decisive games; material_swing 606,710 games (97.4%); passed_pawn 324,194 games (52.0%); queen_trade_ahead 115,121 games (18.5%); king_activation 111,993 games (18.0%); fresh sample (twic1647g.zip, 150 decisive games): material_swing proxy 84.7%.

### 2026-07-06 18:26 — Gate training round

Games: `playbook-vs-stockfish-depth-7-20260706-181934.pgn` (1-0, loss).

Diagnosis: blunder_swing x1. eval fell -407 -> -667 around move 18.

Adjustments: search.root_variety 9 -> 10.

Evidence: TWIC issues 1549-1647, 623,110 decisive games; material_swing 606,710 games (97.4%); fresh sample (twic1647g.zip, 150 decisive games): material_swing proxy 84.7%.

### 2026-07-06 18:52 — Gate training round

Games: `playbook-vs-stockfish-depth-8-20260706-183556.pgn` (0-1, loss).

Diagnosis: slow_outplay x1. gradual decline with no single 250cp swing.

Adjustments: king.tropism 3 -> 4; mobility.per_square 5 -> 6; pieces.rook_open_file 26 -> 28; search.root_variety 10 -> 11.

Evidence: TWIC issues 1549-1647, 623,110 decisive games; development_edge 174,628 games (28.0%); seventh_rank 257,522 games (41.3%); king_attack 311,632 games (50.0%); fresh sample (twic1647g.zip, 150 decisive games): development_edge proxy 84.7%.

### 2026-07-06 19:06 — Gate training round

Games: `playbook-vs-stockfish-depth-8-20260706-185208.pgn` (1/2-1/2, draw).

Diagnosis: blunder_swing x1, repetition_draw x1. eval fell +80 -> -258 around move 40; draw by threefold repetition.

Adjustments: search.root_variety 11 -> 12.

Evidence: TWIC issues 1549-1647, 623,110 decisive games; material_swing 606,710 games (97.4%); fresh sample (twic1647g.zip, 150 decisive games): material_swing proxy 84.7%.

### 2026-07-06 19:26 — Gate training round

Games: `playbook-vs-stockfish-depth-8-20260706-190646.pgn` (1/2-1/2, draw).

Diagnosis: blunder_swing x1, repetition_draw x1. eval fell +80 -> -175 around move 55; draw by threefold repetition.

Adjustments: search.root_variety 12 -> 13.

Evidence: TWIC issues 1549-1647, 623,110 decisive games; material_swing 606,710 games (97.4%); fresh sample (twic1647g.zip, 150 decisive games): material_swing proxy 84.7%.

### 2026-07-06 19:48 — Gate training round

Games: `playbook-vs-stockfish-depth-8-20260706-192647.pgn` (1-0, loss).

Diagnosis: blunder_swing x1, king_collapse x1. eval fell -184 -> -472 around move 80; mated shortly after a roughly level self-eval.

Adjustments: development.castle_urgency 8 -> 10; king.open_file_penalty 30 -> 33; king.ring_attack_penalty 16 -> 18; king.shield_pawn 16 -> 18; search.root_variety 13 -> 14.

Evidence: TWIC issues 1549-1647, 623,110 decisive games; material_swing 606,710 games (97.4%); king_attack 311,632 games (50.0%); fresh sample (twic1647g.zip, 150 decisive games): material_swing proxy 84.7%.

### 2026-07-06 20:20 — Gate training round

Games: `playbook-vs-stockfish-depth-8-20260706-194824.pgn` (0-1, loss).

Diagnosis: blunder_swing x1, king_collapse x1. eval fell +80 -> -211 around move 83; mated shortly after a roughly level self-eval.

Adjustments: development.castle_urgency 10 -> 12; king.open_file_penalty 33 -> 36; king.ring_attack_penalty 18 -> 20; king.shield_pawn 18 -> 20; search.root_variety 14 -> 15.

Evidence: TWIC issues 1549-1647, 623,110 decisive games; material_swing 606,710 games (97.4%); king_attack 311,632 games (50.0%); fresh sample (twic1647g.zip, 150 decisive games): material_swing proxy 84.7%.

### 2026-07-06 20:33 — Gate training round

Games: `playbook-vs-stockfish-depth-8-20260706-202031.pgn` (1-0, loss).

Diagnosis: slow_outplay x1. gradual decline with no single 250cp swing.

Adjustments: king.tropism 4 -> 5; pieces.rook_open_file 28 -> 30; search.root_variety 15 -> 16.

Evidence: TWIC issues 1549-1647, 623,110 decisive games; development_edge 174,628 games (28.0%); seventh_rank 257,522 games (41.3%); king_attack 311,632 games (50.0%); fresh sample (twic1647g.zip, 150 decisive games): development_edge proxy 84.7%.

### 2026-07-06 20:54 — Gate training round

Games: `playbook-vs-stockfish-depth-8-20260706-203325.pgn` (1/2-1/2, draw).

Diagnosis: blunder_swing x1, opening_blunder x1, repetition_draw x1. eval fell +61 -> -277 around move 10; draw by threefold repetition.

Adjustments: development.castle_urgency 12 -> 14; development.early_queen_penalty 10 -> 13; development.uncastled_penalty 18 -> 21; development.undeveloped_minor_penalty 12 -> 14; search.root_variety 16 -> 17.

Evidence: TWIC issues 1549-1647, 623,110 decisive games; development_edge 174,628 games (28.0%); material_swing 606,710 games (97.4%); fresh sample (twic1647g.zip, 150 decisive games): development_edge proxy 84.7%.

### 2026-07-06 21:07 — Gate training round

Games: `playbook-vs-stockfish-depth-8-20260706-205409.pgn` (1-0, loss).

Diagnosis: blunder_swing x1. eval fell +7 -> -922 around move 41.

Adjustments: search.root_variety 17 -> 18.

Evidence: TWIC issues 1549-1647, 623,110 decisive games; material_swing 606,710 games (97.4%); fresh sample (twic1647g.zip, 150 decisive games): material_swing proxy 84.7%.

### 2026-07-06 21:16 — Gate training round

Games: `playbook-vs-stockfish-depth-8-20260706-210712.pgn` (0-1, loss).

Diagnosis: blunder_swing x1, king_collapse x1. eval fell -374 -> -99990 around move 37; mated shortly after a roughly level self-eval.

Adjustments: king.open_file_penalty 36 -> 39; king.ring_attack_penalty 20 -> 22; king.shield_pawn 20 -> 22; search.root_variety 18 -> 19.

Evidence: TWIC issues 1549-1647, 623,110 decisive games; material_swing 606,710 games (97.4%); king_attack 311,632 games (50.0%); fresh sample (twic1647g.zip, 150 decisive games): material_swing proxy 84.7%.

### 2026-07-06 21:24 — Engine improvement: super-linear king-danger term

Depth-8 losses included repeated king-collapse mates (attempts 4 and 9). Added a
super-linear king-danger penalty: piece-weighted attack units are accumulated per
king ring during the existing piece pass, and once >= 2 enemy pieces bear on a king
the penalty grows with units^2 (king.danger_scale, capped king.danger_cap=250,
mg-scaled), so mating nets are foreseen a ply earlier and defended instead of walked
into. Per-node cost is negligible (attack units piggyback on the ring-pressure
popcount already computed); the back-rank mate and Qxg6 sac still solve. Wired into
the trainer king_collapse lever set. 18 focused tests green.
