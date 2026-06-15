# Composer-chess wisdom

First-principles lessons for the Composer UCI engine. General concepts only — no FEN-to-move recipes, no copied learner/zero wisdom, no engine PVs.

## Principles

- Prefer tactically **sound** captures; unsound sacrifices (e.g. Nxf7 without follow-up) lose to even shallow search.
- Develop toward the center; rim knight moves in the opening are rarely justified.
- King safety is not optional: open lines near your king require concrete calculation, not hope.
- Against fixed-depth opponents, **quiet preparatory moves** that improve worst-piece placement can outperform immediate captures.
- When ahead in material, trade pieces not pawns; when behind, seek complications only with calculated forcing lines.
- **Thinking is stored in the PGN** as brace comments after each Composer move; the live board reads those comments directly — no log API.
- Do not push flank pawns (`a`/`b`/`g`/`h`) in the first ~12 moves unless capturing; contest the center first.
- Do not march the king forward on open wings when not in check — open lines are for the stronger side.
- Reject queen-for-rook swaps when the queen is immediately recaptured.
- Claim or contest central squares with pawns before quiet piece shuffles.
- Early queen sorties need a tactical receipt; otherwise development and king safety come first.
- Develop at least two minor pieces before repeated moves with the same piece.
- Reject captures that lose material on recapture without forcing follow-up.
- Do not march the king into the center or open wings when not forced; shelter first.
- Do not push a/h pawns in the opening unless capturing; contest the center first.

## Log

### 2026-06-02 — Baseline vs Stockfish Elo 800–1000

**Hypothesis:** Alpha-beta with PST and capture filtering is enough for weak Elo limits.

**Change:** Initial `composer_chess_uci.py` with quiescence, MVV-LVA, tactical capture filter.

**Result:** Losses (`0-1`) vs Elo 800 and 1000 in early runs.

**Lesson (general):** Elo-limited Stockfish and depth-limited Stockfish are different opponents; depth 8 gate needs horizon-aware tactics and conversion logic, not just opening heuristics.

**Next:** Climb depth gates 1→8 with `run_composer_depth_gate.py`; improve king safety and unsound-sacrifice rejection before chasing depth 8 directly.

### 2026-06-02 — STM eval + search depth (depth gate prep)

**Hypothesis:** The engine was evaluating from White's perspective at every leaf, so Black's search maximized White's advantage. Fixing side-to-move eval plus null-move/check extensions should beat shallow fixed-depth Stockfish when given more think time.

**Change:** `white_eval`/`evaluate` split, pawn structure + bishop pair + open files, stricter capture net ≥ 0, null-move pruning, check extensions, repetition sidestep at root.

**Result:** Pending depth gate runs.

**Lesson (general):** Search only works if leaf scores belong to the side to move; otherwise both colors "prefer" the same eval sign and blunder.

**Next:** Run `watch-composer-depth-gate.ps1` from depth 1 upward; promote passing ideas to Principles.

### 2026-06-03 — Depth 1 gate (loss)

**Hypothesis:** Out-search fixed depth-1 Stockfish; persist reasoning as PGN comments after each Composer move.

**Change:** Live PGN rewritten with {comment} on every ply — visible on the board without log APIs.

**Result:** 0-1 — `composer-vs-stockfish-depth-1-20260603-000245.pgn`

**Lesson (general):** If the operator cannot see thinking, the fix is the PGN file, not another API.

**Next:** Tighten repetition/convert logic, rerun gate

### 2026-06-03 — Depth 8 gate (loss)

**Hypothesis:** Out-search fixed depth-8 Stockfish; persist reasoning as PGN comments after each Composer move.

**Change:** Live PGN rewritten with {comment} on every ply — visible on the board without log APIs.

**Result:** 0-1 — `composer-vs-stockfish-depth-8-20260603-001435.pgn`

**Lesson (general):** If the operator cannot see thinking, the fix is the PGN file, not another API.

**Next:** Tighten repetition/convert logic, rerun gate

### 2026-06-03 — Depth 8 gate (loss, king march)

**Hypothesis:** Flank pawn pushes (`b4`/`h4`), unsound `Bxc6`/`Qxb8` trades, and king marches while losing are separate failure classes fixable with move filters, not deeper search alone.

**Change:** `is_reasonable_move` (flank pawn ban early, king-march ban, queen-for-rook recapture check); tactical capture filter at all depths; stronger opening penalties on a/h files.

**Result:** 0-1 — `composer-vs-stockfish-depth-8-20260603-001441.pgn`

**Lesson (general):** Depth-1 nodes still played unsound sacrifices because capture pruning only ran at depth ≥ 2; king safety needs hard move bans, not eval nudges.

**Next:** Rerun depth 8 with filters; promote flank/king rules to Principles if stable.

### 2026-06-03 — Depth 8 retry (filters)

**Hypothesis:** Banning flank pawn pushes, unsound exchanges, and forward king walks in the opening/middlegame stops the recurring depth-8 loss pattern without book lines.

**Change:** See `is_reasonable_move`, opening_bonus flank penalties, capture filter at every search ply.

**Result:** 0-1 — `composer-vs-stockfish-depth-8-20260603-015122.pgn` (27 moves, `Qg4??` on move 3).

**Lesson (general):** Flank/king filters helped, but an early queen sortie to an attacked square (`Qg4`) still ends the game before depth matters; ban undeveloped queen hops to attacked squares.

**Next:** Queen safety filter in opening; rerun depth 8.

### 2026-06-03 — Depth 8 gate (loss)

**Hypothesis:** Out-search fixed depth-8 Stockfish; persist reasoning as PGN comments after each Composer move.

**Change:** Live PGN rewritten with {comment} on every ply — visible on the board without log APIs.

**Result:** 0-1 — `composer-vs-stockfish-depth-8-20260603-015122.pgn`

**Lesson (general):** If the operator cannot see thinking, the fix is the PGN file, not another API.

**Next:** Tighten repetition/convert logic, rerun gate

## Training log

### 2026-06-08 — Training from `composer-vs-stockfish-depth-8-20260603-015122.pgn` (passive_opening)

**Layer B:** Restrict scope before tactics (Beam).

**Lesson:** Claim or contest central squares with pawns before quiet piece shuffles.

**Signal:** move 1 `e3` in a non-win.

**Rule:** local ban `passive_opening_pawn` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-8-20260603-015122.pgn` (early_queen)

**Layer C:** Deny counterplay before collecting (Bedrock).

**Lesson:** Early queen sorties need a tactical receipt; otherwise development and king safety come first.

**Signal:** move 3 `Qg4` in a non-win.

**Rule:** local ban `early_queen_a_h_file` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-8-20260603-015122.pgn` (slow_development)

**Layer B:** Restrict scope before tactics (Beam).

**Lesson:** Develop at least two minor pieces before repeated moves with the same piece.

**Signal:** move 6 `d3` in a non-win.

**Rule:** local ban `slow_minor_development` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-8-20260603-015122.pgn` (unsound_capture)

**Layer C:** Deny counterplay before collecting (Bedrock).

**Lesson:** Reject captures that lose material on recapture without forcing follow-up.

**Signal:** move 10 `Bxd2` in a non-win.

**Rule:** local ban `unsound_knight_capture` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-8-20260603-015122.pgn` (king_march)

**Layer C:** Deny counterplay before collecting (Bedrock).

**Lesson:** Do not march the king into the center or open wings when not forced; shelter first.

**Signal:** move 23 `Kg3` in a non-win.

**Rule:** local ban `king_march_opening` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-8-20260603-001441.pgn` (flank_pawn)

**Layer B:** Restrict scope before tactics (Beam).

**Lesson:** Do not push a/h pawns in the opening unless capturing; contest the center first.

**Signal:** move 2 `b4` in a non-win.

**Rule:** local ban `flank_pawn_opening` (no API).

### 2026-06-08 — Depth 1 gate (loss)

**Hypothesis:** Out-search fixed depth-1 Stockfish; persist reasoning as PGN comments after each Composer move.

**Change:** Live PGN rewritten with {comment} on every ply — visible on the board without log APIs.

**Result:** 0-1 — `composer-vs-stockfish-depth-1-20260608-113834.pgn`

**Lesson (general):** If the operator cannot see thinking, the fix is the PGN file, not another API.

**Next:** Tighten repetition/convert logic, rerun gate

### 2026-06-08 — Depth 1 gate (loss)

**Hypothesis:** Out-search fixed depth-1 Stockfish; persist reasoning as PGN comments after each Composer move.

**Change:** Live PGN rewritten with {comment} on every ply — visible on the board without log APIs.

**Result:** 1-0 — `composer-vs-stockfish-depth-1-20260608-114904.pgn`

**Lesson (general):** If the operator cannot see thinking, the fix is the PGN file, not another API.

**Next:** Tighten repetition/convert logic, rerun gate

### 2026-06-08 — Depth 1 gate (loss)

**Hypothesis:** Out-search fixed depth-1 Stockfish; persist reasoning as PGN comments after each Composer move.

**Change:** Live PGN rewritten with {comment} on every ply — visible on the board without log APIs.

**Result:** 0-1 — `composer-vs-stockfish-depth-1-20260608-115016.pgn`

**Lesson (general):** If the operator cannot see thinking, the fix is the PGN file, not another API.

**Next:** Tighten repetition/convert logic, rerun gate

### 2026-06-08 — Training from `composer-vs-stockfish-depth-1-20260608-115016.pgn` (slow_development)

**Layer B:** Restrict scope before tactics (Beam).

**Lesson:** Develop at least two minor pieces before repeated moves with the same piece.

**Signal:** move 5 `c4` in a non-win.

**Rule:** local ban `slow_minor_development` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-1-20260608-115016.pgn` (king_march)

**Layer C:** Deny counterplay before collecting (Bedrock).

**Lesson:** Do not march the king into the center or open wings when not forced; shelter first.

**Signal:** move 8 `Kd2` in a non-win.

**Rule:** local ban `king_march_opening` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-8-20260603-015122.pgn` (passive_opening)

**Layer B:** Restrict scope before tactics (Beam).

**Lesson:** Claim or contest central squares with pawns before quiet piece shuffles.

**Signal:** move 1 `e3` in a non-win.

**Rule:** local ban `passive_opening_pawn` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-8-20260603-015122.pgn` (early_queen)

**Layer C:** Deny counterplay before collecting (Bedrock).

**Lesson:** Early queen sorties need a tactical receipt; otherwise development and king safety come first.

**Signal:** move 3 `Qg4` in a non-win.

**Rule:** local ban `early_queen_a_h_file` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-8-20260603-015122.pgn` (unsound_capture)

**Layer C:** Deny counterplay before collecting (Bedrock).

**Lesson:** Reject captures that lose material on recapture without forcing follow-up.

**Signal:** move 10 `Bxd2` in a non-win.

**Rule:** local ban `unsound_knight_capture` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-8-20260603-001441.pgn` (flank_pawn)

**Layer B:** Restrict scope before tactics (Beam).

**Lesson:** Do not push a/h pawns in the opening unless capturing; contest the center first.

**Signal:** move 2 `b4` in a non-win.

**Rule:** local ban `flank_pawn_opening` (no API).

### 2026-06-08 — Depth 1 gate (loss)

**Hypothesis:** Out-search fixed depth-1 Stockfish; persist reasoning as PGN comments after each Composer move.

**Change:** Live PGN rewritten with {comment} on every ply — visible on the board without log APIs.

**Result:** 0-1 — `composer-vs-stockfish-depth-1-20260608-120048.pgn`

**Lesson (general):** If the operator cannot see thinking, the fix is the PGN file, not another API.

**Next:** Tighten repetition/convert logic, rerun gate

### 2026-06-08 — Depth 1 gate (loss)

**Hypothesis:** Out-search fixed depth-1 Stockfish; persist reasoning as PGN comments after each Composer move.

**Change:** Live PGN rewritten with {comment} on every ply — visible on the board without log APIs.

**Result:** 1-0 — `composer-vs-stockfish-depth-1-20260608-121131.pgn`

**Lesson (general):** If the operator cannot see thinking, the fix is the PGN file, not another API.

**Next:** Tighten repetition/convert logic, rerun gate

### 2026-06-08 — Depth 1 gate (loss)

**Hypothesis:** Out-search fixed depth-1 Stockfish; persist reasoning as PGN comments after each Composer move.

**Change:** Live PGN rewritten with {comment} on every ply — visible on the board without log APIs.

**Result:** 0-1 — `composer-vs-stockfish-depth-1-20260608-122313.pgn`

**Lesson (general):** If the operator cannot see thinking, the fix is the PGN file, not another API.

**Next:** Tighten repetition/convert logic, rerun gate

### 2026-06-08 — Training from `composer-vs-stockfish-depth-1-20260608-122313.pgn` (slow_development)

**Layer B:** Restrict scope before tactics (Beam).

**Lesson:** Develop at least two minor pieces before repeated moves with the same piece.

**Signal:** move 5 `c4` in a non-win.

**Rule:** local ban `slow_minor_development` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-1-20260608-122313.pgn` (king_march)

**Layer C:** Deny counterplay before collecting (Bedrock).

**Lesson:** Do not march the king into the center or open wings when not forced; shelter first.

**Signal:** move 8 `Kd2` in a non-win.

**Rule:** local ban `king_march_opening` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-8-20260603-015122.pgn` (passive_opening)

**Layer B:** Restrict scope before tactics (Beam).

**Lesson:** Claim or contest central squares with pawns before quiet piece shuffles.

**Signal:** move 1 `e3` in a non-win.

**Rule:** local ban `passive_opening_pawn` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-8-20260603-015122.pgn` (early_queen)

**Layer C:** Deny counterplay before collecting (Bedrock).

**Lesson:** Early queen sorties need a tactical receipt; otherwise development and king safety come first.

**Signal:** move 3 `Qg4` in a non-win.

**Rule:** local ban `early_queen_a_h_file` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-8-20260603-015122.pgn` (unsound_capture)

**Layer C:** Deny counterplay before collecting (Bedrock).

**Lesson:** Reject captures that lose material on recapture without forcing follow-up.

**Signal:** move 10 `Bxd2` in a non-win.

**Rule:** local ban `unsound_knight_capture` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-8-20260603-001441.pgn` (flank_pawn)

**Layer B:** Restrict scope before tactics (Beam).

**Lesson:** Do not push a/h pawns in the opening unless capturing; contest the center first.

**Signal:** move 2 `b4` in a non-win.

**Rule:** local ban `flank_pawn_opening` (no API).

### 2026-06-08 — Depth 1 gate (loss)

**Hypothesis:** Out-search fixed depth-1 Stockfish; persist reasoning as PGN comments after each Composer move.

**Change:** Live PGN rewritten with {comment} on every ply — visible on the board without log APIs.

**Result:** 0-1 — `composer-vs-stockfish-depth-1-20260608-123415.pgn`

**Lesson (general):** If the operator cannot see thinking, the fix is the PGN file, not another API.

**Next:** Tighten repetition/convert logic, rerun gate

### 2026-06-08 — Depth 1 gate (loss)

**Hypothesis:** Out-search fixed depth-1 Stockfish; persist reasoning as PGN comments after each Composer move.

**Change:** Live PGN rewritten with {comment} on every ply — visible on the board without log APIs.

**Result:** 1-0 — `composer-vs-stockfish-depth-1-20260608-124509.pgn`

**Lesson (general):** If the operator cannot see thinking, the fix is the PGN file, not another API.

**Next:** Tighten repetition/convert logic, rerun gate

### 2026-06-08 — Depth 1 gate (loss)

**Hypothesis:** Out-search fixed depth-1 Stockfish; persist reasoning as PGN comments after each Composer move.

**Change:** Live PGN rewritten with {comment} on every ply — visible on the board without log APIs.

**Result:** 0-1 — `composer-vs-stockfish-depth-1-20260608-125344.pgn`

**Lesson (general):** If the operator cannot see thinking, the fix is the PGN file, not another API.

**Next:** Tighten repetition/convert logic, rerun gate

### 2026-06-08 — Training from `composer-vs-stockfish-depth-1-20260608-125344.pgn` (slow_development)

**Layer B:** Restrict scope before tactics (Beam).

**Lesson:** Develop at least two minor pieces before repeated moves with the same piece.

**Signal:** move 5 `c4` in a non-win.

**Rule:** local ban `slow_minor_development` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-1-20260608-125344.pgn` (king_march)

**Layer C:** Deny counterplay before collecting (Bedrock).

**Lesson:** Do not march the king into the center or open wings when not forced; shelter first.

**Signal:** move 8 `Kd2` in a non-win.

**Rule:** local ban `king_march_opening` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-8-20260603-015122.pgn` (passive_opening)

**Layer B:** Restrict scope before tactics (Beam).

**Lesson:** Claim or contest central squares with pawns before quiet piece shuffles.

**Signal:** move 1 `e3` in a non-win.

**Rule:** local ban `passive_opening_pawn` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-8-20260603-015122.pgn` (early_queen)

**Layer C:** Deny counterplay before collecting (Bedrock).

**Lesson:** Early queen sorties need a tactical receipt; otherwise development and king safety come first.

**Signal:** move 3 `Qg4` in a non-win.

**Rule:** local ban `early_queen_a_h_file` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-8-20260603-015122.pgn` (unsound_capture)

**Layer C:** Deny counterplay before collecting (Bedrock).

**Lesson:** Reject captures that lose material on recapture without forcing follow-up.

**Signal:** move 10 `Bxd2` in a non-win.

**Rule:** local ban `unsound_knight_capture` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-8-20260603-001441.pgn` (flank_pawn)

**Layer B:** Restrict scope before tactics (Beam).

**Lesson:** Do not push a/h pawns in the opening unless capturing; contest the center first.

**Signal:** move 2 `b4` in a non-win.

**Rule:** local ban `flank_pawn_opening` (no API).

### 2026-06-08 — Depth 1 gate (loss)

**Hypothesis:** Out-search fixed depth-1 Stockfish; persist reasoning as PGN comments after each Composer move.

**Change:** Live PGN rewritten with {comment} on every ply — visible on the board without log APIs.

**Result:** 0-1 — `composer-vs-stockfish-depth-1-20260608-130435.pgn`

**Lesson (general):** If the operator cannot see thinking, the fix is the PGN file, not another API.

**Next:** Tighten repetition/convert logic, rerun gate

### 2026-06-08 — Depth 1 gate (loss)

**Hypothesis:** Out-search fixed depth-1 Stockfish; persist reasoning as PGN comments after each Composer move.

**Change:** Live PGN rewritten with {comment} on every ply — visible on the board without log APIs.

**Result:** 1-0 — `composer-vs-stockfish-depth-1-20260608-131539.pgn`

**Lesson (general):** If the operator cannot see thinking, the fix is the PGN file, not another API.

**Next:** Tighten repetition/convert logic, rerun gate

### 2026-06-08 — Depth 1 gate (loss)

**Hypothesis:** Out-search fixed depth-1 Stockfish; persist reasoning as PGN comments after each Composer move.

**Change:** Live PGN rewritten with {comment} on every ply — visible on the board without log APIs.

**Result:** 0-1 — `composer-vs-stockfish-depth-1-20260608-132434.pgn`

**Lesson (general):** If the operator cannot see thinking, the fix is the PGN file, not another API.

**Next:** Tighten repetition/convert logic, rerun gate

### 2026-06-08 — Training from `composer-vs-stockfish-depth-1-20260608-132434.pgn` (slow_development)

**Layer B:** Restrict scope before tactics (Beam).

**Lesson:** Develop at least two minor pieces before repeated moves with the same piece.

**Signal:** move 5 `c4` in a non-win.

**Rule:** local ban `slow_minor_development` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-1-20260608-132434.pgn` (king_march)

**Layer C:** Deny counterplay before collecting (Bedrock).

**Lesson:** Do not march the king into the center or open wings when not forced; shelter first.

**Signal:** move 8 `Kd2` in a non-win.

**Rule:** local ban `king_march_opening` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-8-20260603-015122.pgn` (passive_opening)

**Layer B:** Restrict scope before tactics (Beam).

**Lesson:** Claim or contest central squares with pawns before quiet piece shuffles.

**Signal:** move 1 `e3` in a non-win.

**Rule:** local ban `passive_opening_pawn` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-8-20260603-015122.pgn` (early_queen)

**Layer C:** Deny counterplay before collecting (Bedrock).

**Lesson:** Early queen sorties need a tactical receipt; otherwise development and king safety come first.

**Signal:** move 3 `Qg4` in a non-win.

**Rule:** local ban `early_queen_a_h_file` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-8-20260603-015122.pgn` (unsound_capture)

**Layer C:** Deny counterplay before collecting (Bedrock).

**Lesson:** Reject captures that lose material on recapture without forcing follow-up.

**Signal:** move 10 `Bxd2` in a non-win.

**Rule:** local ban `unsound_knight_capture` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-8-20260603-001441.pgn` (flank_pawn)

**Layer B:** Restrict scope before tactics (Beam).

**Lesson:** Do not push a/h pawns in the opening unless capturing; contest the center first.

**Signal:** move 2 `b4` in a non-win.

**Rule:** local ban `flank_pawn_opening` (no API).

### 2026-06-08 — Depth 1 gate (draw)

**Hypothesis:** Out-search fixed depth-1 Stockfish; persist reasoning as PGN comments after each Composer move.

**Change:** Live PGN rewritten with {comment} on every ply — visible on the board without log APIs.

**Result:** 1/2-1/2 — `composer-vs-stockfish-depth-1-20260608-133640.pgn`

**Lesson (general):** If the operator cannot see thinking, the fix is the PGN file, not another API.

**Next:** Tighten repetition/convert logic, rerun gate

### 2026-06-08 — Depth 1 gate (loss)

**Hypothesis:** Out-search fixed depth-1 Stockfish; persist reasoning as PGN comments after each Composer move.

**Change:** Live PGN rewritten with {comment} on every ply — visible on the board without log APIs.

**Result:** 1-0 — `composer-vs-stockfish-depth-1-20260608-135412.pgn`

**Lesson (general):** If the operator cannot see thinking, the fix is the PGN file, not another API.

**Next:** Tighten repetition/convert logic, rerun gate

### 2026-06-08 — Depth 1 gate (loss)

**Hypothesis:** Out-search fixed depth-1 Stockfish; persist reasoning as PGN comments after each Composer move.

**Change:** Live PGN rewritten with {comment} on every ply — visible on the board without log APIs.

**Result:** 0-1 — `composer-vs-stockfish-depth-1-20260608-140330.pgn`

**Lesson (general):** If the operator cannot see thinking, the fix is the PGN file, not another API.

**Next:** Tighten repetition/convert logic, rerun gate

### 2026-06-08 — Training from `composer-vs-stockfish-depth-1-20260608-140330.pgn` (slow_development)

**Layer B:** Restrict scope before tactics (Beam).

**Lesson:** Develop at least two minor pieces before repeated moves with the same piece.

**Signal:** move 5 `c4` in a non-win.

**Rule:** local ban `slow_minor_development` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-1-20260608-140330.pgn` (king_march)

**Layer C:** Deny counterplay before collecting (Bedrock).

**Lesson:** Do not march the king into the center or open wings when not forced; shelter first.

**Signal:** move 8 `Kd2` in a non-win.

**Rule:** local ban `king_march_opening` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-1-20260608-133640.pgn` (unsound_capture)

**Layer C:** Deny counterplay before collecting (Bedrock).

**Lesson:** Reject captures that lose material on recapture without forcing follow-up.

**Signal:** move 17 `Nxf5` in a non-win.

**Rule:** local ban `unsound_knight_capture` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-8-20260603-015122.pgn` (passive_opening)

**Layer B:** Restrict scope before tactics (Beam).

**Lesson:** Claim or contest central squares with pawns before quiet piece shuffles.

**Signal:** move 1 `e3` in a non-win.

**Rule:** local ban `passive_opening_pawn` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-8-20260603-015122.pgn` (early_queen)

**Layer C:** Deny counterplay before collecting (Bedrock).

**Lesson:** Early queen sorties need a tactical receipt; otherwise development and king safety come first.

**Signal:** move 3 `Qg4` in a non-win.

**Rule:** local ban `early_queen_a_h_file` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-8-20260603-001441.pgn` (flank_pawn)

**Layer B:** Restrict scope before tactics (Beam).

**Lesson:** Do not push a/h pawns in the opening unless capturing; contest the center first.

**Signal:** move 2 `b4` in a non-win.

**Rule:** local ban `flank_pawn_opening` (no API).

### 2026-06-08 — Depth 1 gate (draw)

**Hypothesis:** Out-search fixed depth-1 Stockfish; persist reasoning as PGN comments after each Composer move.

**Change:** Live PGN rewritten with {comment} on every ply — visible on the board without log APIs.

**Result:** 1/2-1/2 — `composer-vs-stockfish-depth-1-20260608-141544.pgn`

**Lesson (general):** If the operator cannot see thinking, the fix is the PGN file, not another API.

**Next:** Tighten repetition/convert logic, rerun gate

### 2026-06-08 — Depth 1 gate (loss)

**Hypothesis:** Out-search fixed depth-1 Stockfish; persist reasoning as PGN comments after each Composer move.

**Change:** Live PGN rewritten with {comment} on every ply — visible on the board without log APIs.

**Result:** 1-0 — `composer-vs-stockfish-depth-1-20260608-143321.pgn`

**Lesson (general):** If the operator cannot see thinking, the fix is the PGN file, not another API.

**Next:** Tighten repetition/convert logic, rerun gate

### 2026-06-08 — Depth 1 gate (loss)

**Hypothesis:** Out-search fixed depth-1 Stockfish; persist reasoning as PGN comments after each Composer move.

**Change:** Live PGN rewritten with {comment} on every ply — visible on the board without log APIs.

**Result:** 0-1 — `composer-vs-stockfish-depth-1-20260608-144019.pgn`

**Lesson (general):** If the operator cannot see thinking, the fix is the PGN file, not another API.

**Next:** Tighten repetition/convert logic, rerun gate

### 2026-06-08 — Training from `composer-vs-stockfish-depth-1-20260608-144019.pgn` (slow_development)

**Layer B:** Restrict scope before tactics (Beam).

**Lesson:** Develop at least two minor pieces before repeated moves with the same piece.

**Signal:** move 5 `c4` in a non-win.

**Rule:** local ban `slow_minor_development` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-1-20260608-144019.pgn` (king_march)

**Layer C:** Deny counterplay before collecting (Bedrock).

**Lesson:** Do not march the king into the center or open wings when not forced; shelter first.

**Signal:** move 8 `Kd2` in a non-win.

**Rule:** local ban `king_march_opening` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-1-20260608-141544.pgn` (unsound_capture)

**Layer C:** Deny counterplay before collecting (Bedrock).

**Lesson:** Reject captures that lose material on recapture without forcing follow-up.

**Signal:** move 17 `Nxf5` in a non-win.

**Rule:** local ban `unsound_knight_capture` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-8-20260603-015122.pgn` (passive_opening)

**Layer B:** Restrict scope before tactics (Beam).

**Lesson:** Claim or contest central squares with pawns before quiet piece shuffles.

**Signal:** move 1 `e3` in a non-win.

**Rule:** local ban `passive_opening_pawn` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-8-20260603-015122.pgn` (early_queen)

**Layer C:** Deny counterplay before collecting (Bedrock).

**Lesson:** Early queen sorties need a tactical receipt; otherwise development and king safety come first.

**Signal:** move 3 `Qg4` in a non-win.

**Rule:** local ban `early_queen_a_h_file` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-8-20260603-001441.pgn` (flank_pawn)

**Layer B:** Restrict scope before tactics (Beam).

**Lesson:** Do not push a/h pawns in the opening unless capturing; contest the center first.

**Signal:** move 2 `b4` in a non-win.

**Rule:** local ban `flank_pawn_opening` (no API).

### 2026-06-08 — Depth 1 gate (loss)

**Hypothesis:** Out-search fixed depth-1 Stockfish; persist reasoning as PGN comments after each Composer move.

**Change:** Live PGN rewritten with {comment} on every ply — visible on the board without log APIs.

**Result:** 0-1 — `composer-vs-stockfish-depth-1-20260608-145054.pgn`

**Lesson (general):** If the operator cannot see thinking, the fix is the PGN file, not another API.

**Next:** Tighten repetition/convert logic, rerun gate

### 2026-06-08 — Depth 1 gate (loss)

**Hypothesis:** Out-search fixed depth-1 Stockfish; persist reasoning as PGN comments after each Composer move.

**Change:** Live PGN rewritten with {comment} on every ply — visible on the board without log APIs.

**Result:** 1-0 — `composer-vs-stockfish-depth-1-20260608-150128.pgn`

**Lesson (general):** If the operator cannot see thinking, the fix is the PGN file, not another API.

**Next:** Tighten repetition/convert logic, rerun gate

### 2026-06-08 — Depth 1 gate (loss)

**Hypothesis:** Out-search fixed depth-1 Stockfish; persist reasoning as PGN comments after each Composer move.

**Change:** Live PGN rewritten with {comment} on every ply — visible on the board without log APIs.

**Result:** 0-1 — `composer-vs-stockfish-depth-1-20260608-151011.pgn`

**Lesson (general):** If the operator cannot see thinking, the fix is the PGN file, not another API.

**Next:** Tighten repetition/convert logic, rerun gate

### 2026-06-08 — Training from `composer-vs-stockfish-depth-1-20260608-151011.pgn` (slow_development)

**Layer B:** Restrict scope before tactics (Beam).

**Lesson:** Develop at least two minor pieces before repeated moves with the same piece.

**Signal:** move 5 `c4` in a non-win.

**Rule:** local ban `slow_minor_development` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-1-20260608-151011.pgn` (king_march)

**Layer C:** Deny counterplay before collecting (Bedrock).

**Lesson:** Do not march the king into the center or open wings when not forced; shelter first.

**Signal:** move 8 `Kd2` in a non-win.

**Rule:** local ban `king_march_opening` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-1-20260608-141544.pgn` (unsound_capture)

**Layer C:** Deny counterplay before collecting (Bedrock).

**Lesson:** Reject captures that lose material on recapture without forcing follow-up.

**Signal:** move 17 `Nxf5` in a non-win.

**Rule:** local ban `unsound_knight_capture` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-8-20260603-015122.pgn` (passive_opening)

**Layer B:** Restrict scope before tactics (Beam).

**Lesson:** Claim or contest central squares with pawns before quiet piece shuffles.

**Signal:** move 1 `e3` in a non-win.

**Rule:** local ban `passive_opening_pawn` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-8-20260603-015122.pgn` (early_queen)

**Layer C:** Deny counterplay before collecting (Bedrock).

**Lesson:** Early queen sorties need a tactical receipt; otherwise development and king safety come first.

**Signal:** move 3 `Qg4` in a non-win.

**Rule:** local ban `early_queen_a_h_file` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-8-20260603-001441.pgn` (flank_pawn)

**Layer B:** Restrict scope before tactics (Beam).

**Lesson:** Do not push a/h pawns in the opening unless capturing; contest the center first.

**Signal:** move 2 `b4` in a non-win.

**Rule:** local ban `flank_pawn_opening` (no API).

### 2026-06-08 — Depth 1 gate (loss)

**Hypothesis:** Out-search fixed depth-1 Stockfish; persist reasoning as PGN comments after each Composer move.

**Change:** Live PGN rewritten with {comment} on every ply — visible on the board without log APIs.

**Result:** 0-1 — `composer-vs-stockfish-depth-1-20260608-152103.pgn`

**Lesson (general):** If the operator cannot see thinking, the fix is the PGN file, not another API.

**Next:** Tighten repetition/convert logic, rerun gate

### 2026-06-08 — Depth 1 gate (draw)

**Hypothesis:** Out-search fixed depth-1 Stockfish; persist reasoning as PGN comments after each Composer move.

**Change:** Live PGN rewritten with {comment} on every ply — visible on the board without log APIs.

**Result:** 1/2-1/2 — `composer-vs-stockfish-depth-1-20260608-153708.pgn`

**Lesson (general):** If the operator cannot see thinking, the fix is the PGN file, not another API.

**Next:** Tighten repetition/convert logic, rerun gate

### 2026-06-08 — Depth 1 gate (loss)

**Hypothesis:** Out-search fixed depth-1 Stockfish; persist reasoning as PGN comments after each Composer move.

**Change:** Live PGN rewritten with {comment} on every ply — visible on the board without log APIs.

**Result:** 0-1 — `composer-vs-stockfish-depth-1-20260608-154821.pgn`

**Lesson (general):** If the operator cannot see thinking, the fix is the PGN file, not another API.

**Next:** Tighten repetition/convert logic, rerun gate

### 2026-06-08 — Training from `composer-vs-stockfish-depth-1-20260608-154821.pgn` (slow_development)

**Layer B:** Restrict scope before tactics (Beam).

**Lesson:** Develop at least two minor pieces before repeated moves with the same piece.

**Signal:** move 5 `c4` in a non-win.

**Rule:** local ban `slow_minor_development` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-1-20260608-154821.pgn` (king_march)

**Layer C:** Deny counterplay before collecting (Bedrock).

**Lesson:** Do not march the king into the center or open wings when not forced; shelter first.

**Signal:** move 8 `Kd2` in a non-win.

**Rule:** local ban `king_march_opening` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-1-20260608-141544.pgn` (unsound_capture)

**Layer C:** Deny counterplay before collecting (Bedrock).

**Lesson:** Reject captures that lose material on recapture without forcing follow-up.

**Signal:** move 17 `Nxf5` in a non-win.

**Rule:** local ban `unsound_knight_capture` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-8-20260603-015122.pgn` (passive_opening)

**Layer B:** Restrict scope before tactics (Beam).

**Lesson:** Claim or contest central squares with pawns before quiet piece shuffles.

**Signal:** move 1 `e3` in a non-win.

**Rule:** local ban `passive_opening_pawn` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-8-20260603-015122.pgn` (early_queen)

**Layer C:** Deny counterplay before collecting (Bedrock).

**Lesson:** Early queen sorties need a tactical receipt; otherwise development and king safety come first.

**Signal:** move 3 `Qg4` in a non-win.

**Rule:** local ban `early_queen_a_h_file` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-8-20260603-001441.pgn` (flank_pawn)

**Layer B:** Restrict scope before tactics (Beam).

**Lesson:** Do not push a/h pawns in the opening unless capturing; contest the center first.

**Signal:** move 2 `b4` in a non-win.

**Rule:** local ban `flank_pawn_opening` (no API).

### 2026-06-08 — Depth 1 gate (loss)

**Hypothesis:** Out-search fixed depth-1 Stockfish; persist reasoning as PGN comments after each Composer move.

**Change:** Live PGN rewritten with {comment} on every ply — visible on the board without log APIs.

**Result:** 0-1 — `composer-vs-stockfish-depth-1-20260608-155855.pgn`

**Lesson (general):** If the operator cannot see thinking, the fix is the PGN file, not another API.

**Next:** Tighten repetition/convert logic, rerun gate

### 2026-06-08 — Depth 1 gate (draw)

**Hypothesis:** Out-search fixed depth-1 Stockfish; persist reasoning as PGN comments after each Composer move.

**Change:** Live PGN rewritten with {comment} on every ply — visible on the board without log APIs.

**Result:** 1/2-1/2 — `composer-vs-stockfish-depth-1-20260608-160931.pgn`

**Lesson (general):** If the operator cannot see thinking, the fix is the PGN file, not another API.

**Next:** Tighten repetition/convert logic, rerun gate

### 2026-06-08 — Depth 1 gate (loss)

**Hypothesis:** Out-search fixed depth-1 Stockfish; persist reasoning as PGN comments after each Composer move.

**Change:** Live PGN rewritten with {comment} on every ply — visible on the board without log APIs.

**Result:** 0-1 — `composer-vs-stockfish-depth-1-20260608-162807.pgn`

**Lesson (general):** If the operator cannot see thinking, the fix is the PGN file, not another API.

**Next:** Tighten repetition/convert logic, rerun gate

### 2026-06-08 — Training from `composer-vs-stockfish-depth-1-20260608-162807.pgn` (slow_development)

**Layer B:** Restrict scope before tactics (Beam).

**Lesson:** Develop at least two minor pieces before repeated moves with the same piece.

**Signal:** move 5 `c4` in a non-win.

**Rule:** local ban `slow_minor_development` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-1-20260608-162807.pgn` (king_march)

**Layer C:** Deny counterplay before collecting (Bedrock).

**Lesson:** Do not march the king into the center or open wings when not forced; shelter first.

**Signal:** move 8 `Kd2` in a non-win.

**Rule:** local ban `king_march_opening` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-1-20260608-141544.pgn` (unsound_capture)

**Layer C:** Deny counterplay before collecting (Bedrock).

**Lesson:** Reject captures that lose material on recapture without forcing follow-up.

**Signal:** move 17 `Nxf5` in a non-win.

**Rule:** local ban `unsound_knight_capture` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-8-20260603-015122.pgn` (passive_opening)

**Layer B:** Restrict scope before tactics (Beam).

**Lesson:** Claim or contest central squares with pawns before quiet piece shuffles.

**Signal:** move 1 `e3` in a non-win.

**Rule:** local ban `passive_opening_pawn` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-8-20260603-015122.pgn` (early_queen)

**Layer C:** Deny counterplay before collecting (Bedrock).

**Lesson:** Early queen sorties need a tactical receipt; otherwise development and king safety come first.

**Signal:** move 3 `Qg4` in a non-win.

**Rule:** local ban `early_queen_a_h_file` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-8-20260603-001441.pgn` (flank_pawn)

**Layer B:** Restrict scope before tactics (Beam).

**Lesson:** Do not push a/h pawns in the opening unless capturing; contest the center first.

**Signal:** move 2 `b4` in a non-win.

**Rule:** local ban `flank_pawn_opening` (no API).

### 2026-06-08 — Depth 1 gate (loss)

**Hypothesis:** Out-search fixed depth-1 Stockfish; persist reasoning as PGN comments after each Composer move.

**Change:** Live PGN rewritten with {comment} on every ply — visible on the board without log APIs.

**Result:** 0-1 — `composer-vs-stockfish-depth-1-20260608-163852.pgn`

**Lesson (general):** If the operator cannot see thinking, the fix is the PGN file, not another API.

**Next:** Tighten repetition/convert logic, rerun gate

### 2026-06-08 — Depth 1 gate (loss)

**Hypothesis:** Out-search fixed depth-1 Stockfish; persist reasoning as PGN comments after each Composer move.

**Change:** Live PGN rewritten with {comment} on every ply — visible on the board without log APIs.

**Result:** 1-0 — `composer-vs-stockfish-depth-1-20260608-164938.pgn`

**Lesson (general):** If the operator cannot see thinking, the fix is the PGN file, not another API.

**Next:** Tighten repetition/convert logic, rerun gate

### 2026-06-08 — Depth 1 gate (loss)

**Hypothesis:** Out-search fixed depth-1 Stockfish; persist reasoning as PGN comments after each Composer move.

**Change:** Live PGN rewritten with {comment} on every ply — visible on the board without log APIs.

**Result:** 0-1 — `composer-vs-stockfish-depth-1-20260608-165053.pgn`

**Lesson (general):** If the operator cannot see thinking, the fix is the PGN file, not another API.

**Next:** Tighten repetition/convert logic, rerun gate

### 2026-06-08 — Training from `composer-vs-stockfish-depth-1-20260608-165053.pgn` (slow_development)

**Layer B:** Restrict scope before tactics (Beam).

**Lesson:** Develop at least two minor pieces before repeated moves with the same piece.

**Signal:** move 5 `c4` in a non-win.

**Rule:** local ban `slow_minor_development` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-1-20260608-165053.pgn` (king_march)

**Layer C:** Deny counterplay before collecting (Bedrock).

**Lesson:** Do not march the king into the center or open wings when not forced; shelter first.

**Signal:** move 8 `Kd2` in a non-win.

**Rule:** local ban `king_march_opening` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-1-20260608-141544.pgn` (unsound_capture)

**Layer C:** Deny counterplay before collecting (Bedrock).

**Lesson:** Reject captures that lose material on recapture without forcing follow-up.

**Signal:** move 17 `Nxf5` in a non-win.

**Rule:** local ban `unsound_knight_capture` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-8-20260603-015122.pgn` (passive_opening)

**Layer B:** Restrict scope before tactics (Beam).

**Lesson:** Claim or contest central squares with pawns before quiet piece shuffles.

**Signal:** move 1 `e3` in a non-win.

**Rule:** local ban `passive_opening_pawn` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-8-20260603-015122.pgn` (early_queen)

**Layer C:** Deny counterplay before collecting (Bedrock).

**Lesson:** Early queen sorties need a tactical receipt; otherwise development and king safety come first.

**Signal:** move 3 `Qg4` in a non-win.

**Rule:** local ban `early_queen_a_h_file` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-8-20260603-001441.pgn` (flank_pawn)

**Layer B:** Restrict scope before tactics (Beam).

**Lesson:** Do not push a/h pawns in the opening unless capturing; contest the center first.

**Signal:** move 2 `b4` in a non-win.

**Rule:** local ban `flank_pawn_opening` (no API).

### 2026-06-08 — Depth 1 gate (loss)

**Hypothesis:** Out-search fixed depth-1 Stockfish; persist reasoning as PGN comments after each Composer move.

**Change:** Live PGN rewritten with {comment} on every ply — visible on the board without log APIs.

**Result:** 0-1 — `composer-vs-stockfish-depth-1-20260608-165922.pgn`

**Lesson (general):** If the operator cannot see thinking, the fix is the PGN file, not another API.

**Next:** Tighten repetition/convert logic, rerun gate

### 2026-06-08 — Depth 1 gate (draw)

**Hypothesis:** Out-search fixed depth-1 Stockfish; persist reasoning as PGN comments after each Composer move.

**Change:** Live PGN rewritten with {comment} on every ply — visible on the board without log APIs.

**Result:** 1/2-1/2 — `composer-vs-stockfish-depth-1-20260608-171040.pgn`

**Lesson (general):** If the operator cannot see thinking, the fix is the PGN file, not another API.

**Next:** Tighten repetition/convert logic, rerun gate

### 2026-06-08 — Depth 1 gate (loss)

**Hypothesis:** Out-search fixed depth-1 Stockfish; persist reasoning as PGN comments after each Composer move.

**Change:** Live PGN rewritten with {comment} on every ply — visible on the board without log APIs.

**Result:** 0-1 — `composer-vs-stockfish-depth-1-20260608-172831.pgn`

**Lesson (general):** If the operator cannot see thinking, the fix is the PGN file, not another API.

**Next:** Tighten repetition/convert logic, rerun gate

### 2026-06-08 — Training from `composer-vs-stockfish-depth-1-20260608-172831.pgn` (slow_development)

**Layer B:** Restrict scope before tactics (Beam).

**Lesson:** Develop at least two minor pieces before repeated moves with the same piece.

**Signal:** move 5 `c4` in a non-win.

**Rule:** local ban `slow_minor_development` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-1-20260608-172831.pgn` (king_march)

**Layer C:** Deny counterplay before collecting (Bedrock).

**Lesson:** Do not march the king into the center or open wings when not forced; shelter first.

**Signal:** move 8 `Kd2` in a non-win.

**Rule:** local ban `king_march_opening` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-1-20260608-141544.pgn` (unsound_capture)

**Layer C:** Deny counterplay before collecting (Bedrock).

**Lesson:** Reject captures that lose material on recapture without forcing follow-up.

**Signal:** move 17 `Nxf5` in a non-win.

**Rule:** local ban `unsound_knight_capture` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-8-20260603-015122.pgn` (passive_opening)

**Layer B:** Restrict scope before tactics (Beam).

**Lesson:** Claim or contest central squares with pawns before quiet piece shuffles.

**Signal:** move 1 `e3` in a non-win.

**Rule:** local ban `passive_opening_pawn` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-8-20260603-015122.pgn` (early_queen)

**Layer C:** Deny counterplay before collecting (Bedrock).

**Lesson:** Early queen sorties need a tactical receipt; otherwise development and king safety come first.

**Signal:** move 3 `Qg4` in a non-win.

**Rule:** local ban `early_queen_a_h_file` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-8-20260603-001441.pgn` (flank_pawn)

**Layer B:** Restrict scope before tactics (Beam).

**Lesson:** Do not push a/h pawns in the opening unless capturing; contest the center first.

**Signal:** move 2 `b4` in a non-win.

**Rule:** local ban `flank_pawn_opening` (no API).

### 2026-06-08 — Depth 1 gate (loss)

**Hypothesis:** Out-search fixed depth-1 Stockfish; persist reasoning as PGN comments after each Composer move.

**Change:** Live PGN rewritten with {comment} on every ply — visible on the board without log APIs.

**Result:** 0-1 — `composer-vs-stockfish-depth-1-20260608-173909.pgn`

**Lesson (general):** If the operator cannot see thinking, the fix is the PGN file, not another API.

**Next:** Tighten repetition/convert logic, rerun gate

### 2026-06-08 — Depth 1 gate (draw)

**Hypothesis:** Out-search fixed depth-1 Stockfish; persist reasoning as PGN comments after each Composer move.

**Change:** Live PGN rewritten with {comment} on every ply — visible on the board without log APIs.

**Result:** 1/2-1/2 — `composer-vs-stockfish-depth-1-20260608-174947.pgn`

**Lesson (general):** If the operator cannot see thinking, the fix is the PGN file, not another API.

**Next:** Tighten repetition/convert logic, rerun gate

### 2026-06-08 — Depth 1 gate (loss)

**Hypothesis:** Out-search fixed depth-1 Stockfish; persist reasoning as PGN comments after each Composer move.

**Change:** Live PGN rewritten with {comment} on every ply — visible on the board without log APIs.

**Result:** 0-1 — `composer-vs-stockfish-depth-1-20260608-175803.pgn`

**Lesson (general):** If the operator cannot see thinking, the fix is the PGN file, not another API.

**Next:** Tighten repetition/convert logic, rerun gate

### 2026-06-08 — Training from `composer-vs-stockfish-depth-1-20260608-175803.pgn` (slow_development)

**Layer B:** Restrict scope before tactics (Beam).

**Lesson:** Develop at least two minor pieces before repeated moves with the same piece.

**Signal:** move 5 `c4` in a non-win.

**Rule:** local ban `slow_minor_development` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-1-20260608-175803.pgn` (king_march)

**Layer C:** Deny counterplay before collecting (Bedrock).

**Lesson:** Do not march the king into the center or open wings when not forced; shelter first.

**Signal:** move 8 `Kd2` in a non-win.

**Rule:** local ban `king_march_opening` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-1-20260608-174947.pgn` (unsound_capture)

**Layer C:** Deny counterplay before collecting (Bedrock).

**Lesson:** Reject captures that lose material on recapture without forcing follow-up.

**Signal:** move 18 `Bxa3` in a non-win.

**Rule:** local ban `unsound_knight_capture` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-8-20260603-015122.pgn` (passive_opening)

**Layer B:** Restrict scope before tactics (Beam).

**Lesson:** Claim or contest central squares with pawns before quiet piece shuffles.

**Signal:** move 1 `e3` in a non-win.

**Rule:** local ban `passive_opening_pawn` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-8-20260603-015122.pgn` (early_queen)

**Layer C:** Deny counterplay before collecting (Bedrock).

**Lesson:** Early queen sorties need a tactical receipt; otherwise development and king safety come first.

**Signal:** move 3 `Qg4` in a non-win.

**Rule:** local ban `early_queen_a_h_file` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-8-20260603-001441.pgn` (flank_pawn)

**Layer B:** Restrict scope before tactics (Beam).

**Lesson:** Do not push a/h pawns in the opening unless capturing; contest the center first.

**Signal:** move 2 `b4` in a non-win.

**Rule:** local ban `flank_pawn_opening` (no API).

### 2026-06-08 — Depth 1 gate (loss)

**Hypothesis:** Out-search fixed depth-1 Stockfish; persist reasoning as PGN comments after each Composer move.

**Change:** Live PGN rewritten with {comment} on every ply — visible on the board without log APIs.

**Result:** 0-1 — `composer-vs-stockfish-depth-1-20260608-180856.pgn`

**Lesson (general):** If the operator cannot see thinking, the fix is the PGN file, not another API.

**Next:** Tighten repetition/convert logic, rerun gate

### 2026-06-08 — Depth 1 gate (draw)

**Hypothesis:** Out-search fixed depth-1 Stockfish; persist reasoning as PGN comments after each Composer move.

**Change:** Live PGN rewritten with {comment} on every ply — visible on the board without log APIs.

**Result:** 1/2-1/2 — `composer-vs-stockfish-depth-1-20260608-181939.pgn`

**Lesson (general):** If the operator cannot see thinking, the fix is the PGN file, not another API.

**Next:** Tighten repetition/convert logic, rerun gate

### 2026-06-08 — Depth 1 gate (loss)

**Hypothesis:** Out-search fixed depth-1 Stockfish; persist reasoning as PGN comments after each Composer move.

**Change:** Live PGN rewritten with {comment} on every ply — visible on the board without log APIs.

**Result:** 0-1 — `composer-vs-stockfish-depth-1-20260608-183325.pgn`

**Lesson (general):** If the operator cannot see thinking, the fix is the PGN file, not another API.

**Next:** Tighten repetition/convert logic, rerun gate

### 2026-06-08 — Training from `composer-vs-stockfish-depth-1-20260608-183325.pgn` (slow_development)

**Layer B:** Restrict scope before tactics (Beam).

**Lesson:** Develop at least two minor pieces before repeated moves with the same piece.

**Signal:** move 5 `c4` in a non-win.

**Rule:** local ban `slow_minor_development` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-1-20260608-183325.pgn` (king_march)

**Layer C:** Deny counterplay before collecting (Bedrock).

**Lesson:** Do not march the king into the center or open wings when not forced; shelter first.

**Signal:** move 8 `Kd2` in a non-win.

**Rule:** local ban `king_march_opening` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-1-20260608-174947.pgn` (unsound_capture)

**Layer C:** Deny counterplay before collecting (Bedrock).

**Lesson:** Reject captures that lose material on recapture without forcing follow-up.

**Signal:** move 18 `Bxa3` in a non-win.

**Rule:** local ban `unsound_knight_capture` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-8-20260603-015122.pgn` (passive_opening)

**Layer B:** Restrict scope before tactics (Beam).

**Lesson:** Claim or contest central squares with pawns before quiet piece shuffles.

**Signal:** move 1 `e3` in a non-win.

**Rule:** local ban `passive_opening_pawn` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-8-20260603-015122.pgn` (early_queen)

**Layer C:** Deny counterplay before collecting (Bedrock).

**Lesson:** Early queen sorties need a tactical receipt; otherwise development and king safety come first.

**Signal:** move 3 `Qg4` in a non-win.

**Rule:** local ban `early_queen_a_h_file` (no API).

### 2026-06-08 — Depth 1 gate (loss)

**Hypothesis:** Out-search fixed depth-1 Stockfish; persist reasoning as PGN comments after each Composer move.

**Change:** Live PGN rewritten with {comment} on every ply — visible on the board without log APIs.

**Result:** 0-1 — `composer-vs-stockfish-depth-1-20260608-184504.pgn`

**Lesson (general):** If the operator cannot see thinking, the fix is the PGN file, not another API.

**Next:** Tighten repetition/convert logic, rerun gate

### 2026-06-08 — Depth 1 gate (loss)

**Hypothesis:** Out-search fixed depth-1 Stockfish; persist reasoning as PGN comments after each Composer move.

**Change:** Live PGN rewritten with {comment} on every ply — visible on the board without log APIs.

**Result:** 1-0 — `composer-vs-stockfish-depth-1-20260608-185547.pgn`

**Lesson (general):** If the operator cannot see thinking, the fix is the PGN file, not another API.

**Next:** Tighten repetition/convert logic, rerun gate

### 2026-06-08 — Depth 1 gate (loss)

**Hypothesis:** Out-search fixed depth-1 Stockfish; persist reasoning as PGN comments after each Composer move.

**Change:** Live PGN rewritten with {comment} on every ply — visible on the board without log APIs.

**Result:** 0-1 — `composer-vs-stockfish-depth-1-20260608-185711.pgn`

**Lesson (general):** If the operator cannot see thinking, the fix is the PGN file, not another API.

**Next:** Tighten repetition/convert logic, rerun gate

### 2026-06-08 — Training from `composer-vs-stockfish-depth-1-20260608-185711.pgn` (slow_development)

**Layer B:** Restrict scope before tactics (Beam).

**Lesson:** Develop at least two minor pieces before repeated moves with the same piece.

**Signal:** move 5 `c4` in a non-win.

**Rule:** local ban `slow_minor_development` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-1-20260608-185711.pgn` (king_march)

**Layer C:** Deny counterplay before collecting (Bedrock).

**Lesson:** Do not march the king into the center or open wings when not forced; shelter first.

**Signal:** move 8 `Kd2` in a non-win.

**Rule:** local ban `king_march_opening` (no API).

### 2026-06-08 — Training from `composer-vs-stockfish-depth-1-20260608-174947.pgn` (unsound_capture)

**Layer C:** Deny counterplay before collecting (Bedrock).

**Lesson:** Reject captures that lose material on recapture without forcing follow-up.

**Signal:** move 18 `Bxa3` in a non-win.

**Rule:** local ban `unsound_knight_capture` (no API).

### 2026-06-08 — Depth 1 gate (loss)

**Hypothesis:** Out-search fixed depth-1 Stockfish; persist reasoning as PGN comments after each Composer move.

**Change:** Live PGN rewritten with {comment} on every ply — visible on the board without log APIs.

**Result:** 0-1 — `composer-vs-stockfish-depth-1-20260608-190807.pgn`

**Lesson (general):** If the operator cannot see thinking, the fix is the PGN file, not another API.

**Next:** Tighten repetition/convert logic, rerun gate
