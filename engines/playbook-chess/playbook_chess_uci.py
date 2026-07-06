"""Playbook-chess UCI engine.

Deterministic alpha-beta engine whose evaluation weights, search settings, and
draw-contempt discipline are parsed at runtime from the human-readable
``playbook.md`` instruction file next to this script (PRD 165-167).

Hard rules learned from the wisdom-chess failure (PRD 167):
- Never hard-filter legal moves out of interior search or quiescence by style.
  Preferences act through evaluation weights and move ordering only.
- No LLM, Stockfish, opening book, tablebase, or fallback-move logic in the
  move path. The engine always plays its own searched move.
"""

from __future__ import annotations

import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import chess
import chess.polyglot

ENGINE_NAME = "Playbook-chess"
ENGINE_AUTHOR = "Playbook harness"
ROOT = Path(__file__).resolve().parents[2]
PLAYBOOK_PATH = Path(os.environ.get("PLAYBOOK_CHESS_FILE", "") or (Path(__file__).resolve().parent / "playbook.md"))

MATE_SCORE = 100_000
MATE_BOUND = MATE_SCORE - 512
INF = MATE_SCORE * 2
MAX_PLY = 96
QS_CAP = 24

TT_EXACT, TT_LOWER, TT_UPPER = 0, 1, 2

WEIGHT_LINE_RE = re.compile(r"^\s*-\s*([a-z][a-z0-9_.]*)\s*=\s*(-?\d+(?:\.\d+)?)")

DEFAULT_PLAYBOOK: dict[str, float] = {
    "meta.version": 1,
    "search.min_depth": 4,
    "search.base_movetime_ms": 8000,
    "search.movetime_fraction": 0.90,
    "search.draw_contempt": 30,
    "search.aspiration_window": 40,
    "material.pawn": 100,
    "material.knight": 320,
    "material.bishop": 330,
    "material.rook": 500,
    "material.queen": 900,
    "mobility.per_square": 3,
    "pieces.bishop_pair": 30,
    "pieces.rook_open_file": 22,
    "pieces.rook_semi_open_file": 10,
    "pieces.rook_seventh": 24,
    "development.undeveloped_minor_penalty": 12,
    "development.uncastled_penalty": 18,
    "king.shield_pawn": 12,
    "king.open_file_penalty": 24,
    "king.ring_attack_penalty": 12,
    "pawns.passed_base": 18,
    "pawns.passed_per_rank": 14,
    "pawns.doubled_penalty": 12,
    "pawns.isolated_penalty": 12,
    "conversion.edge_threshold": 250,
    "conversion.simplify_bonus": 4,
    "conversion.king_activity": 12,
    "tempo.bonus": 12,
}

PST_PAWN = [
    0, 0, 0, 0, 0, 0, 0, 0,
    50, 50, 50, 50, 50, 50, 50, 50,
    10, 10, 20, 30, 30, 20, 10, 10,
    5, 5, 10, 25, 25, 10, 5, 5,
    0, 0, 0, 20, 20, 0, 0, 0,
    5, -5, -10, 0, 0, -10, -5, 5,
    5, 10, 10, -20, -20, 10, 10, 5,
    0, 0, 0, 0, 0, 0, 0, 0,
]
PST_KNIGHT = [
    -50, -40, -30, -30, -30, -30, -40, -50,
    -40, -20, 0, 0, 0, 0, -20, -40,
    -30, 0, 10, 15, 15, 10, 0, -30,
    -30, 5, 15, 20, 20, 15, 5, -30,
    -30, 0, 15, 20, 20, 15, 0, -30,
    -30, 5, 10, 15, 15, 10, 5, -30,
    -40, -20, 0, 5, 5, 0, -20, -40,
    -50, -40, -30, -30, -30, -30, -40, -50,
]
PST_BISHOP = [
    -20, -10, -10, -10, -10, -10, -10, -20,
    -10, 0, 0, 0, 0, 0, 0, -10,
    -10, 0, 5, 10, 10, 5, 0, -10,
    -10, 5, 5, 10, 10, 5, 5, -10,
    -10, 0, 10, 10, 10, 10, 0, -10,
    -10, 10, 10, 10, 10, 10, 10, -10,
    -10, 5, 0, 0, 0, 0, 5, -10,
    -20, -10, -10, -10, -10, -10, -10, -20,
]
PST_ROOK = [
    0, 0, 0, 0, 0, 0, 0, 0,
    5, 10, 10, 10, 10, 10, 10, 5,
    -5, 0, 0, 0, 0, 0, 0, -5,
    -5, 0, 0, 0, 0, 0, 0, -5,
    -5, 0, 0, 0, 0, 0, 0, -5,
    -5, 0, 0, 0, 0, 0, 0, -5,
    -5, 0, 0, 0, 0, 0, 0, -5,
    0, 0, 0, 5, 5, 0, 0, 0,
]
PST_QUEEN = [
    -20, -10, -10, -5, -5, -10, -10, -20,
    -10, 0, 0, 0, 0, 0, 0, -10,
    -10, 0, 5, 5, 5, 5, 0, -10,
    -5, 0, 5, 5, 5, 5, 0, -5,
    0, 0, 5, 5, 5, 5, 0, -5,
    -10, 5, 5, 5, 5, 5, 0, -10,
    -10, 0, 5, 0, 0, 0, 0, -10,
    -20, -10, -10, -5, -5, -10, -10, -20,
]
PST_KING_MG = [
    -30, -40, -40, -50, -50, -40, -40, -30,
    -30, -40, -40, -50, -50, -40, -40, -30,
    -30, -40, -40, -50, -50, -40, -40, -30,
    -30, -40, -40, -50, -50, -40, -40, -30,
    -20, -30, -30, -40, -40, -30, -30, -20,
    -10, -20, -20, -20, -20, -20, -20, -10,
    20, 20, 0, 0, 0, 0, 20, 20,
    20, 30, 10, 0, 0, 10, 30, 20,
]
PST_KING_EG = [
    -50, -40, -30, -20, -20, -30, -40, -50,
    -30, -20, -10, 0, 0, -10, -20, -30,
    -30, -10, 20, 30, 30, 20, -10, -30,
    -30, -10, 30, 40, 40, 30, -10, -30,
    -30, -10, 30, 40, 40, 30, -10, -30,
    -30, -10, 20, 30, 30, 20, -10, -30,
    -30, -30, 0, 0, 0, 0, -30, -30,
    -50, -30, -30, -30, -30, -30, -30, -50,
]
PST = {
    chess.PAWN: PST_PAWN,
    chess.KNIGHT: PST_KNIGHT,
    chess.BISHOP: PST_BISHOP,
    chess.ROOK: PST_ROOK,
    chess.QUEEN: PST_QUEEN,
}

# Front-span + adjacent-files masks for passed-pawn detection, per color.
PASSED_MASK: dict[bool, list[int]] = {chess.WHITE: [0] * 64, chess.BLACK: [0] * 64}
for _sq in range(64):
    _f, _r = chess.square_file(_sq), chess.square_rank(_sq)
    _wmask = _bmask = 0
    for _df in (-1, 0, 1):
        _nf = _f + _df
        if not 0 <= _nf <= 7:
            continue
        for _nr in range(_r + 1, 8):
            _wmask |= chess.BB_SQUARES[chess.square(_nf, _nr)]
        for _nr in range(0, _r):
            _bmask |= chess.BB_SQUARES[chess.square(_nf, _nr)]
    PASSED_MASK[chess.WHITE][_sq] = _wmask
    PASSED_MASK[chess.BLACK][_sq] = _bmask


def parse_playbook_text(text: str) -> dict[str, float]:
    """Parse `- key = value` weight lines; prose and malformed lines are ignored."""
    weights = dict(DEFAULT_PLAYBOOK)
    for line in text.splitlines():
        match = WEIGHT_LINE_RE.match(line)
        if match:
            try:
                weights[match.group(1)] = float(match.group(2))
            except ValueError:
                continue
    return weights


class Playbook:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.mtime = 0.0
        self.weights = dict(DEFAULT_PLAYBOOK)
        self.refresh(force=True)

    def refresh(self, force: bool = False) -> None:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            self.weights = dict(DEFAULT_PLAYBOOK)
            return
        if force or mtime != self.mtime:
            self.mtime = mtime
            try:
                self.weights = parse_playbook_text(self.path.read_text(encoding="utf-8"))
            except OSError:
                self.weights = dict(DEFAULT_PLAYBOOK)

    def __getitem__(self, key: str) -> float:
        return self.weights.get(key, DEFAULT_PLAYBOOK.get(key, 0.0))


PB = Playbook(PLAYBOOK_PATH)


class SearchTimeout(Exception):
    pass


@dataclass
class SearchState:
    board: chess.Board = field(default_factory=chess.Board)
    nodes: int = 0
    stop_time: float = 0.0
    allow_stop: bool = False
    best_root: chess.Move | None = None
    root_score: int = 0
    depth_reached: int = 0
    killers: list[list[chess.Move | None]] = field(
        default_factory=lambda: [[None, None] for _ in range(MAX_PLY + QS_CAP)]
    )
    history: list[list[int]] = field(default_factory=lambda: [[0] * 64 for _ in range(12)])


STATE = SearchState()
TT: dict[int, tuple[int, int, int, chess.Move | None]] = {}
EVAL_CACHE: dict[int, int] = {}

_HAS_PRIVATE_ATTACKERS = hasattr(chess.Board(), "_attackers_mask")


def piece_values() -> dict[int, int]:
    return {
        chess.PAWN: int(PB["material.pawn"]),
        chess.KNIGHT: int(PB["material.knight"]),
        chess.BISHOP: int(PB["material.bishop"]),
        chess.ROOK: int(PB["material.rook"]),
        chess.QUEEN: int(PB["material.queen"]),
        chess.KING: 20_000,
    }


PV: dict[int, int] = piece_values()


def game_phase(board: chess.Board) -> float:
    """0 = full material, 1 = bare endgame."""
    pieces = chess.popcount(board.occupied)
    return max(0.0, min(1.0, 1.0 - (pieces - 2) / 28.0))


def material_diff_white(board: chess.Board) -> int:
    score = 0
    for pt in (chess.PAWN, chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN):
        score += PV[pt] * (
            chess.popcount(board.pieces_mask(pt, chess.WHITE))
            - chess.popcount(board.pieces_mask(pt, chess.BLACK))
        )
    return score


def king_centralization(board: chess.Board, color: chess.Color) -> int:
    king_sq = board.king(color)
    if king_sq is None:
        return 0
    f, r = chess.square_file(king_sq), chess.square_rank(king_sq)
    return min(f, 7 - f) + min(r, 7 - r)


def white_eval(board: chess.Board) -> int:
    """Static evaluation from White's perspective, all weights from the playbook."""
    phase = game_phase(board)
    mg = 1.0 - phase
    score = 0.0

    mobility_w = PB["mobility.per_square"]
    pawns_w = board.pieces_mask(chess.PAWN, chess.WHITE)
    pawns_b = board.pieces_mask(chess.PAWN, chess.BLACK)
    all_pawns = pawns_w | pawns_b

    file_counts = {chess.WHITE: [0] * 8, chess.BLACK: [0] * 8}
    for sq in chess.scan_forward(pawns_w):
        file_counts[chess.WHITE][chess.square_file(sq)] += 1
    for sq in chess.scan_forward(pawns_b):
        file_counts[chess.BLACK][chess.square_file(sq)] += 1

    for sq, piece in board.piece_map().items():
        color = piece.color
        sign = 1 if color == chess.WHITE else -1
        pt = piece.piece_type
        if pt == chess.KING:
            idx = sq if color == chess.WHITE else chess.square_mirror(sq)
            score += sign * (PST_KING_MG[idx] * mg + PST_KING_EG[idx] * phase)
            continue
        idx = sq if color == chess.WHITE else chess.square_mirror(sq)
        score += sign * (PV[pt] + PST[pt][idx])
        if pt != chess.PAWN:
            score += sign * mobility_w * chess.popcount(board.attacks_mask(sq))
        if pt == chess.ROOK:
            f = chess.square_file(sq)
            if not (all_pawns & chess.BB_FILES[f]):
                score += sign * PB["pieces.rook_open_file"]
            elif not ((pawns_w if color == chess.WHITE else pawns_b) & chess.BB_FILES[f]):
                score += sign * PB["pieces.rook_semi_open_file"]
            rank = chess.square_rank(sq)
            if (color == chess.WHITE and rank == 6) or (color == chess.BLACK and rank == 1):
                score += sign * PB["pieces.rook_seventh"]
        elif pt == chess.PAWN:
            enemy_pawns = pawns_b if color == chess.WHITE else pawns_w
            if not (enemy_pawns & PASSED_MASK[color][sq]):
                advance = chess.square_rank(sq) if color == chess.WHITE else 7 - chess.square_rank(sq)
                score += sign * (PB["pawns.passed_base"] + advance * PB["pawns.passed_per_rank"])
            f = chess.square_file(sq)
            counts = file_counts[color]
            if counts[f] > 1:
                score -= sign * PB["pawns.doubled_penalty"]
            left = counts[f - 1] if f > 0 else 0
            right = counts[f + 1] if f < 7 else 0
            if left == 0 and right == 0:
                score -= sign * PB["pawns.isolated_penalty"]

    # Bishop pair.
    if chess.popcount(board.pieces_mask(chess.BISHOP, chess.WHITE)) >= 2:
        score += PB["pieces.bishop_pair"]
    if chess.popcount(board.pieces_mask(chess.BISHOP, chess.BLACK)) >= 2:
        score -= PB["pieces.bishop_pair"]

    # Development discipline (opening only).
    if board.fullmove_number <= 14 and mg > 0.5:
        for color, back_rank_bb, sign in (
            (chess.WHITE, chess.BB_RANK_1, 1),
            (chess.BLACK, chess.BB_RANK_8, -1),
        ):
            minors_home = chess.popcount(
                (board.pieces_mask(chess.KNIGHT, color) | board.pieces_mask(chess.BISHOP, color))
                & back_rank_bb
            )
            score -= sign * minors_home * PB["development.undeveloped_minor_penalty"]
            if board.has_castling_rights(color):
                # Still able to castle but has not: mild nudge to finish development.
                score -= sign * PB["development.uncastled_penalty"] * 0.5
            else:
                king_sq = board.king(color)
                if king_sq is not None and chess.square_file(king_sq) in (3, 4):
                    score -= sign * PB["development.uncastled_penalty"]

    # King safety (midgame-scaled).
    if mg > 0.15:
        for color in (chess.WHITE, chess.BLACK):
            king_sq = board.king(color)
            if king_sq is None:
                continue
            sign = 1 if color == chess.WHITE else -1
            enemy = not color
            kf = chess.square_file(king_sq)
            kr = chess.square_rank(king_sq)
            own_pawns = pawns_w if color == chess.WHITE else pawns_b
            shield = 0
            ranks = (kr + 1, kr + 2) if color == chess.WHITE else (kr - 1, kr - 2)
            for df in (-1, 0, 1):
                nf = kf + df
                if not 0 <= nf <= 7:
                    continue
                if not (all_pawns & chess.BB_FILES[nf]):
                    score -= sign * PB["king.open_file_penalty"] * mg
                for nr in ranks:
                    if 0 <= nr <= 7 and own_pawns & chess.BB_SQUARES[chess.square(nf, nr)]:
                        shield += 1
                        break
            score += sign * shield * PB["king.shield_pawn"] * mg
            ring = chess.BB_KING_ATTACKS[king_sq]
            attacks = 0
            for rsq in chess.scan_forward(ring):
                attacks += chess.popcount(board.attackers_mask(enemy, rsq))
            score -= sign * attacks * PB["king.ring_attack_penalty"] * mg

    # Conversion discipline: when clearly ahead, reward trading and king activity.
    matdiff = material_diff_white(board)
    if abs(matdiff) >= PB["conversion.edge_threshold"]:
        ahead = chess.WHITE if matdiff > 0 else chess.BLACK
        sign = 1 if ahead == chess.WHITE else -1
        enemy_men = chess.popcount(board.occupied_co[not ahead])
        score += sign * PB["conversion.simplify_bonus"] * (16 - enemy_men)
        score += sign * PB["conversion.king_activity"] * king_centralization(board, ahead) * phase

    score += PB["tempo.bonus"] if board.turn == chess.WHITE else -PB["tempo.bonus"]
    return int(score)


def evaluate(board: chess.Board) -> int:
    """Static eval from the side-to-move perspective (negamax convention)."""
    key = chess.polyglot.zobrist_hash(board)
    cached = EVAL_CACHE.get(key)
    if cached is None:
        if len(EVAL_CACHE) > 2_000_000:
            EVAL_CACHE.clear()
        cached = white_eval(board)
        EVAL_CACHE[key] = cached
    return cached if board.turn == chess.WHITE else -cached


def draw_score(board: chess.Board) -> int:
    """Draw value from the side-to-move perspective with dynamic contempt."""
    contempt = int(PB["search.draw_contempt"])
    matdiff = material_diff_white(board)
    stm_diff = matdiff if board.turn == chess.WHITE else -matdiff
    if stm_diff >= 150:
        return -contempt
    if stm_diff <= -150:
        return contempt // 2
    return 0


def static_exchange(board: chess.Board, move: chess.Move) -> int:
    """Static exchange evaluation of `move` (standard swap algorithm)."""
    to_sq = move.to_square
    if board.is_en_passant(move):
        first_gain = PV[chess.PAWN]
    else:
        victim = board.piece_type_at(to_sq)
        first_gain = PV[victim] if victim else 0
    attacker_pt = board.piece_type_at(move.from_square)
    if attacker_pt is None:
        return 0
    if not _HAS_PRIVATE_ATTACKERS:
        # Conservative fallback: assume full loss of the attacker if defended.
        if board.attackers_mask(not board.turn, to_sq):
            return first_gain - PV[attacker_pt]
        return first_gain

    occ = board.occupied & ~chess.BB_SQUARES[move.from_square]
    gains = [first_gain]
    side = not board.turn
    victim_val = PV[attacker_pt]
    while True:
        attackers = board._attackers_mask(side, to_sq, occ) & occ
        if not attackers:
            break
        lva_sq = None
        lva_val = None
        for pt in (chess.PAWN, chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN, chess.KING):
            subset = attackers & board.pieces_mask(pt, side)
            if subset:
                lva_sq = chess.lsb(subset)
                lva_val = PV[pt]
                break
        if lva_sq is None:
            break
        gains.append(victim_val - gains[-1])
        if max(-gains[-2], gains[-1]) < 0:
            break
        victim_val = lva_val
        occ &= ~chess.BB_SQUARES[lva_sq]
        side = not side
    for i in range(len(gains) - 1, 0, -1):
        gains[i - 1] = -max(-gains[i - 1], gains[i])
    return gains[0]


def mvv_lva(board: chess.Board, move: chess.Move) -> int:
    if board.is_en_passant(move):
        victim_val = PV[chess.PAWN]
    else:
        victim = board.piece_type_at(move.to_square)
        victim_val = PV[victim] if victim else 0
    attacker = board.piece_type_at(move.from_square)
    return victim_val * 16 - (PV[attacker] if attacker else 0) // 16


def piece_history_idx(piece: chess.Piece) -> int:
    return piece.piece_type - 1 + (0 if piece.color == chess.WHITE else 6)


def order_moves(
    board: chess.Board,
    moves: list[chess.Move],
    ply: int,
    tt_move: chess.Move | None,
) -> list[chess.Move]:
    killers = STATE.killers[ply] if ply < len(STATE.killers) else [None, None]

    def score(move: chess.Move) -> int:
        if move == tt_move:
            return 1_000_000_000
        if board.is_capture(move):
            base = mvv_lva(board, move)
            victim = PV[chess.PAWN] if board.is_en_passant(move) else PV[board.piece_type_at(move.to_square) or chess.PAWN]
            attacker = PV[board.piece_type_at(move.from_square) or chess.PAWN]
            if victim >= attacker or static_exchange(board, move) >= 0:
                return 10_000_000 + base
            return -10_000_000 + base
        if move.promotion == chess.QUEEN:
            return 9_000_000
        if move == killers[0]:
            return 8_000_000
        if move == killers[1]:
            return 7_900_000
        piece = board.piece_at(move.from_square)
        hist = STATE.history[piece_history_idx(piece)][move.to_square] if piece else 0
        return hist

    return sorted(moves, key=score, reverse=True)


def check_time() -> None:
    if STATE.allow_stop and STATE.stop_time and (STATE.nodes & 255) == 0:
        if time.time() >= STATE.stop_time:
            raise SearchTimeout


def quiescence(board: chess.Board, alpha: int, beta: int, ply: int, qdepth: int = 0) -> int:
    STATE.nodes += 1
    check_time()
    in_check = board.is_check()
    if not in_check:
        stand = evaluate(board)
        if stand >= beta:
            return stand
        if stand > alpha:
            alpha = stand
        if qdepth >= QS_CAP:
            return alpha
        moves = [
            m
            for m in board.legal_moves
            if board.is_capture(m) or m.promotion == chess.QUEEN
        ]
    else:
        if qdepth >= QS_CAP:
            return evaluate(board)
        moves = list(board.legal_moves)
        if not moves:
            return -MATE_SCORE + ply

    best = alpha if not in_check else -INF
    for move in order_moves(board, moves, min(ply, len(STATE.killers) - 1), None):
        if not in_check and board.is_capture(move):
            victim = PV[chess.PAWN] if board.is_en_passant(move) else PV[board.piece_type_at(move.to_square) or chess.PAWN]
            if stand + victim + 200 <= alpha:
                continue
            attacker = PV[board.piece_type_at(move.from_square) or chess.PAWN]
            if victim < attacker and static_exchange(board, move) < 0:
                continue
        board.push(move)
        score = -quiescence(board, -beta, -alpha, ply + 1, qdepth + 1)
        board.pop()
        if score > best:
            best = score
        if score > alpha:
            alpha = score
        if alpha >= beta:
            break
    return best


def non_pawn_material(board: chess.Board, color: chess.Color) -> int:
    total = 0
    for pt in (chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN):
        total += PV[pt] * chess.popcount(board.pieces_mask(pt, color))
    return total


def alpha_beta(
    board: chess.Board,
    depth: int,
    alpha: int,
    beta: int,
    ply: int,
    allow_null: bool = True,
) -> int:
    STATE.nodes += 1
    check_time()

    if ply > 0:
        if board.is_repetition(2) or board.halfmove_clock >= 100 or board.is_insufficient_material():
            return draw_score(board)

    in_check = board.is_check()
    if depth <= 0 and not in_check:
        return quiescence(board, alpha, beta, ply)
    if depth <= 0:
        depth = 1

    key = chess.polyglot.zobrist_hash(board)
    tt_move: chess.Move | None = None
    entry = TT.get(key)
    if entry is not None:
        tt_depth, tt_score, tt_flag, tt_move = entry
        if tt_depth >= depth and ply > 0:
            score = tt_score
            if score > MATE_BOUND:
                score -= ply
            elif score < -MATE_BOUND:
                score += ply
            if tt_flag == TT_EXACT:
                return score
            if tt_flag == TT_LOWER and score >= beta:
                return score
            if tt_flag == TT_UPPER and score <= alpha:
                return score

    # Null-move pruning.
    if (
        allow_null
        and not in_check
        and depth >= 3
        and ply > 0
        and beta < MATE_BOUND
        and non_pawn_material(board, board.turn) > PV[chess.ROOK]
    ):
        board.push(chess.Move.null())
        try:
            r = 2 + depth // 4
            null_score = -alpha_beta(board, depth - 1 - r, -beta, -beta + 1, ply + 1, allow_null=False)
        finally:
            board.pop()
        if null_score >= beta:
            return beta

    moves = list(board.legal_moves)
    if not moves:
        if in_check:
            return -MATE_SCORE + ply
        return draw_score(board)

    static_val = None
    futile = False
    if depth <= 2 and not in_check and abs(alpha) < MATE_BOUND:
        static_val = evaluate(board)
        futile = static_val + 140 * depth <= alpha

    moves = order_moves(board, moves, min(ply, len(STATE.killers) - 1), tt_move)
    best_score = -INF
    best_move: chess.Move | None = None
    orig_alpha = alpha

    for i, move in enumerate(moves):
        is_capture = board.is_capture(move)
        is_promo = move.promotion is not None
        if futile and best_move is not None and not is_capture and not is_promo:
            continue
        board.push(move)
        gives_check = board.is_check()
        ext = 1 if gives_check else 0
        try:
            if i == 0:
                score = -alpha_beta(board, depth - 1 + ext, -beta, -alpha, ply + 1)
            else:
                reduction = 0
                if (
                    depth >= 3
                    and i >= 4
                    and not is_capture
                    and not is_promo
                    and not in_check
                    and not gives_check
                ):
                    reduction = 1 + (1 if i >= 12 else 0)
                score = -alpha_beta(board, depth - 1 - reduction + ext, -alpha - 1, -alpha, ply + 1)
                if score > alpha and reduction:
                    score = -alpha_beta(board, depth - 1 + ext, -alpha - 1, -alpha, ply + 1)
                if score > alpha and score < beta:
                    score = -alpha_beta(board, depth - 1 + ext, -beta, -alpha, ply + 1)
        finally:
            board.pop()

        if score > best_score:
            best_score = score
            best_move = move
        if score > alpha:
            alpha = score
        if alpha >= beta:
            if not is_capture and ply < len(STATE.killers):
                killers = STATE.killers[ply]
                if killers[0] != move:
                    killers[1] = killers[0]
                    killers[0] = move
                piece = board.piece_at(move.from_square)
                if piece:
                    STATE.history[piece_history_idx(piece)][move.to_square] += depth * depth
            break

    store_score = best_score
    if store_score > MATE_BOUND:
        store_score += ply
    elif store_score < -MATE_BOUND:
        store_score -= ply
    if best_score >= beta:
        flag = TT_LOWER
    elif best_score <= orig_alpha:
        flag = TT_UPPER
    else:
        flag = TT_EXACT
    if len(TT) > 4_000_000:
        TT.clear()
    TT[key] = (depth, store_score, flag, best_move)
    return best_score


def search_root(board: chess.Board, depth: int, alpha: int, beta: int) -> tuple[int, chess.Move | None]:
    key = chess.polyglot.zobrist_hash(board)
    entry = TT.get(key)
    tt_move = entry[3] if entry else None
    prefer = STATE.best_root or tt_move
    moves = order_moves(board, list(board.legal_moves), 0, prefer)
    best_score = -INF
    best_move: chess.Move | None = None
    for i, move in enumerate(moves):
        board.push(move)
        gives_check = board.is_check()
        ext = 1 if gives_check else 0
        try:
            if i == 0:
                score = -alpha_beta(board, depth - 1 + ext, -beta, -alpha, 1)
            else:
                score = -alpha_beta(board, depth - 1 + ext, -alpha - 1, -alpha, 1)
                if score > alpha and score < beta:
                    score = -alpha_beta(board, depth - 1 + ext, -beta, -alpha, 1)
        finally:
            board.pop()
        if score > best_score:
            best_score = score
            best_move = move
        if score > alpha:
            alpha = score
        if alpha >= beta:
            break
    if best_move is not None:
        TT[key] = (depth, best_score, TT_EXACT if best_score < beta else TT_LOWER, best_move)
    return best_score, best_move


def move_tag(board: chess.Board, move: chess.Move, score: int) -> str:
    if board.is_castling(move):
        return "Castling: king safety first."
    if board.is_capture(move):
        return "Capture: collect or convert."
    matdiff = material_diff_white(board)
    stm_diff = matdiff if board.turn == chess.WHITE else -matdiff
    if stm_diff >= PB["conversion.edge_threshold"]:
        return "Conversion: simplify and activate."
    if score >= 150:
        return "Pressing the advantage."
    if score <= -150:
        return "Defending: safety over activity."
    if board.fullmove_number <= 12:
        return "Development and center."
    return "Improving the position."


def pick_move(board: chess.Board, movetime_ms: int | None, depth_limit: int | None) -> chess.Move:
    PB.refresh()
    global PV
    PV = piece_values()

    legal = list(board.legal_moves)
    if not legal:
        raise RuntimeError("no legal moves")
    if len(legal) == 1:
        print(
            f"info string Depth 0, eval +0cp, nodes 0 (playbook v{int(PB['meta.version'])}). Forced move.",
            flush=True,
        )
        return legal[0]

    STATE.nodes = 0
    STATE.best_root = None
    STATE.root_score = 0
    STATE.depth_reached = 0
    STATE.allow_stop = False
    STATE.killers = [[None, None] for _ in range(MAX_PLY + QS_CAP)]

    budget_s = None
    if movetime_ms:
        budget_s = max(0.2, movetime_ms / 1000.0 * float(PB["search.movetime_fraction"]))
        STATE.stop_time = time.time() + budget_s
    else:
        STATE.stop_time = 0.0

    start = time.time()
    max_depth = depth_limit or 64
    min_depth = int(PB["search.min_depth"])
    window = int(PB["search.aspiration_window"])
    last_score = 0

    depth = 1
    while depth <= max_depth:
        STATE.allow_stop = depth > 1
        alpha = last_score - window if depth > 1 else -INF
        beta = last_score + window if depth > 1 else INF
        try:
            score, move = search_root(board, depth, alpha, beta)
            if move is not None and (score <= alpha or score >= beta):
                score, move = search_root(board, depth, -INF, INF)
        except SearchTimeout:
            break
        if move is not None:
            STATE.best_root = move
            STATE.root_score = score
            STATE.depth_reached = depth
            last_score = score
        elapsed = time.time() - start
        print(
            f"info depth {depth} score cp {STATE.root_score} nodes {STATE.nodes} "
            f"time {int(elapsed * 1000)} pv {STATE.best_root.uci() if STATE.best_root else ''}",
            flush=True,
        )
        if abs(STATE.root_score) > MATE_BOUND:
            break
        if budget_s is not None and depth >= min_depth and elapsed >= budget_s * 0.5:
            break
        depth += 1

    move = STATE.best_root or legal[0]
    tag = move_tag(board, move, STATE.root_score)
    print(
        f"info string Depth {STATE.depth_reached}, eval {STATE.root_score:+d}cp, "
        f"nodes {STATE.nodes} (playbook v{int(PB['meta.version'])}). {tag}",
        flush=True,
    )
    return move


def handle_position(tokens: list[str]) -> None:
    idx = 1
    if idx < len(tokens) and tokens[idx] == "startpos":
        board = chess.Board()
        idx += 1
    elif idx < len(tokens) and tokens[idx] == "fen":
        idx += 1
        fen_parts: list[str] = []
        while idx < len(tokens) and tokens[idx] != "moves":
            fen_parts.append(tokens[idx])
            idx += 1
        board = chess.Board(" ".join(fen_parts))
    else:
        return
    if idx < len(tokens) and tokens[idx] == "moves":
        for uci in tokens[idx + 1 :]:
            board.push(chess.Move.from_uci(uci))
    STATE.board = board


def handle_go(tokens: list[str]) -> None:
    movetime_ms: int | None = None
    depth_limit: int | None = None
    wtime = btime = winc = binc = None
    i = 1
    while i < len(tokens):
        token = tokens[i]
        if token in ("movetime", "depth", "wtime", "btime", "winc", "binc") and i + 1 < len(tokens):
            value = int(tokens[i + 1])
            if token == "movetime":
                movetime_ms = value
            elif token == "depth":
                depth_limit = value
            elif token == "wtime":
                wtime = value
            elif token == "btime":
                btime = value
            elif token == "winc":
                winc = value
            elif token == "binc":
                binc = value
            i += 2
            continue
        if token == "infinite":
            depth_limit = depth_limit or 12
        i += 1

    if movetime_ms is None and (wtime is not None or btime is not None):
        side_time = wtime if STATE.board.turn == chess.WHITE else btime
        side_inc = (winc or 0) if STATE.board.turn == chess.WHITE else (binc or 0)
        if side_time is not None:
            movetime_ms = max(150, min(side_time // 20 + side_inc, 15_000))

    if movetime_ms is None and depth_limit is None:
        movetime_ms = int(PB["search.base_movetime_ms"])

    move = pick_move(STATE.board, movetime_ms, depth_limit)
    print(f"bestmove {move.uci()}", flush=True)


def uci_loop() -> None:
    while True:
        line = sys.stdin.readline()
        if line == "":
            break
        line = line.strip()
        if not line:
            continue
        tokens = line.split()
        cmd = tokens[0].lower()
        if cmd == "uci":
            print(f"id name {ENGINE_NAME}")
            print(f"id author {ENGINE_AUTHOR}")
            print("option name Hash type spin default 64 min 1 max 512")
            print("uciok", flush=True)
        elif cmd == "isready":
            print("readyok", flush=True)
        elif cmd == "ucinewgame":
            TT.clear()
            EVAL_CACHE.clear()
            STATE.board = chess.Board()
            STATE.history = [[0] * 64 for _ in range(12)]
        elif cmd == "position":
            handle_position(tokens)
        elif cmd == "go":
            handle_go(tokens)
        elif cmd == "stop":
            STATE.stop_time = time.time()
        elif cmd == "quit":
            break


if __name__ == "__main__":
    uci_loop()
