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

- meta.version = 2
- search.min_depth = 4 — always finish at least this depth before honoring the clock; shallow moves lose to one-ply tactics.
- search.base_movetime_ms = 8000
- search.movetime_fraction = 0.90
- search.draw_contempt = 40 — TWIC: 97.4% of decisive games contain a material swing; when we are ahead, a draw is a failed conversion, so repeated positions score negative for the side that is better.
- search.equal_draw_aversion = 12 — the depth-2 gate drew by repetition from an equal position; win-gated ladders require playing on at equality, so even an equal draw scores mildly negative.
- search.aspiration_window = 40

## Material

- material.pawn = 100
- material.knight = 320
- material.bishop = 330
- material.rook = 500
- material.queen = 900

## Mobility and piece activity

- mobility.per_square = 3 — winners restrict losers: piece scope is worth paying small material-free costs for.
- pieces.bishop_pair = 30
- pieces.rook_open_file = 22 — TWIC: seventh-rank and file invasion themes appear in 41.3% of decisive games (257,522/623,110).
- pieces.rook_semi_open_file = 10
- pieces.rook_seventh = 24 — same seventh-rank evidence as above.

## Development and castling

- development.undeveloped_minor_penalty = 12 — TWIC: a development edge shows up in 28.0% of decisive games (174,628/623,110).
- development.uncastled_penalty = 18 — winners castle and connect rooks before starting operations.

## King safety

- king.shield_pawn = 12
- king.open_file_penalty = 24 — an open or half-open file next to the king is the highway every TWIC king attack uses.
- king.ring_attack_penalty = 12 — TWIC: king attacks decide 50.0% of games (311,632/623,110); count every enemy attack touching the king ring.

## Pawns

- pawns.passed_base = 18
- pawns.passed_per_rank = 14 — TWIC: passed-pawn conversion appears in 52.0% of decisive wins (324,194/623,110); each rank of advance matters more than the last.
- pawns.doubled_penalty = 12
- pawns.isolated_penalty = 12

## Conversion (winning technique)

- conversion.edge_threshold = 250
- conversion.simplify_bonus = 4 — TWIC: queen trades while ahead appear in 18.5% of wins (115,121); trade pieces, not pawns, when up material.
- conversion.king_activity = 12 — TWIC: king activation decides 18.0% of games (111,993); in simplified positions the king is a fighting piece.
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
