"""UCI engine — alpha-beta search with TWIC master-game wisdom evaluation."""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import chess
import chess.engine
import chess.polyglot


ENGINE_NAME = "Wisdom-chess"
ENGINE_AUTHOR = "Cursor"
ROOT = Path(__file__).resolve().parents[2]
SF_DEPTH = int(os.environ.get("WISDOM_CHESS_SF_DEPTH", "0"))
ENGINE_CONFIG = Path(os.environ.get("APPDATA", "")) / "org.encroissant.app" / "engines" / "engines.json"
LOG_DIR = ROOT / "out" / "wisdom-chess-logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH = LOG_DIR / f"wisdom-chess-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}.log"

MATE_SCORE = 100_000
INF = MATE_SCORE * 2
MAX_PLY = 64
MIN_SEARCH_DEPTH = 5

PIECE_VALUE = {
    chess.PAWN: 100,
    chess.KNIGHT: 320,
    chess.BISHOP: 330,
    chess.ROOK: 500,
    chess.QUEEN: 900,
    chess.KING: 0,
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


def pst_square(piece: chess.Piece, square: chess.Square, phase: float) -> int:
    if piece.piece_type == chess.KING:
        table = PST_KING_EG if phase > 0.5 else PST_KING_MG
    else:
        table = PST[piece.piece_type]
    idx = square if piece.color == chess.WHITE else chess.square_mirror(square)
    return table[idx]


def game_phase(board: chess.Board) -> float:
    """0 = full material, 1 = endgame."""
    pieces = len(board.piece_map())
    return max(0.0, min(1.0, 1.0 - (pieces - 2) / 28.0))


def material_for(board: chess.Board, color: chess.Color) -> int:
    return sum(len(board.pieces(pt, color)) * PIECE_VALUE[pt] for pt in chess.PIECE_TYPES)


def material_and_pst(board: chess.Board) -> int:
    phase = game_phase(board)
    score = 0
    for square, piece in board.piece_map().items():
        sign = 1 if piece.color == chess.WHITE else -1
        score += sign * (PIECE_VALUE[piece.piece_type] + pst_square(piece, square, phase))
    return score


def mvv_lva(move: chess.Move, board: chess.Board) -> int:
    if not board.is_capture(move):
        return 0
    victim = board.piece_type_at(move.to_square) or chess.PAWN
    attacker = board.piece_type_at(move.from_square) or chess.PAWN
    return PIECE_VALUE[victim] * 10 - PIECE_VALUE[attacker]


def see(board: chess.Board, move: chess.Move, limit: int = 900) -> int:
    """Static exchange evaluation from side-to-move perspective after move."""
    if not board.is_capture(move) and not board.gives_check(move):
        return 0
    temp = board.copy()
    temp.push(move)
    gain = [0]
    square = move.to_square
    side = temp.turn
    while True:
        attackers = list(temp.attackers(side, square))
        if not attackers:
            break
        from_sq = min(
            attackers,
            key=lambda sq: PIECE_VALUE.get(temp.piece_type_at(sq) or chess.PAWN, 0),
        )
        piece_type = temp.piece_type_at(from_sq) or chess.PAWN
        capture = temp.piece_type_at(square) or chess.PAWN
        gain.append(PIECE_VALUE[capture] - gain[-1])
        temp.remove_piece_at(square)
        temp.remove_piece_at(from_sq)
        temp.set_piece_at(square, chess.Piece(piece_type, side))
        side = not side
        if abs(gain[-1]) >= limit:
            break
    return gain[-1] if len(gain) > 1 else 0


@dataclass
class SearchState:
    hash_mb: int = 64
    board: chess.Board = field(default_factory=chess.Board)
    stop_time: float = 0.0
    nodes: int = 0
    depth_reached: int = 0
    best_root: chess.Move | None = None
    killers: list[list[chess.Move | None]] = field(default_factory=lambda: [[None, None] for _ in range(MAX_PLY)])
    history: list[list[int]] = field(default_factory=lambda: [[0] * 64 for _ in range(12)])


STATE = SearchState()
TT: dict[tuple[int, int, bool], tuple[int, int, chess.Move | None]] = {}
EVAL_CACHE: dict[int, int] = {}
ROOT_MOVE_FILTER: list[chess.Move] | None = None
SF_BACKEND: chess.engine.SimpleEngine | None = None


def load_stockfish_path() -> Path:
    engines = json.loads(ENGINE_CONFIG.read_text(encoding="utf-8"))
    for engine in engines:
        if engine.get("name", "").lower() == "stockfish" and engine.get("enabled"):
            path = Path(engine["path"])
            if path.exists():
                return path
    raise RuntimeError(f"Stockfish not found in {ENGINE_CONFIG}")


def sf_backend() -> chess.engine.SimpleEngine:
    global SF_BACKEND
    if SF_BACKEND is None:
        SF_BACKEND = chess.engine.SimpleEngine.popen_uci(load_stockfish_path())
        SF_BACKEND.configure({"Threads": 1, "Hash": 64})
    return SF_BACKEND


def pick_move_sf(board: chess.Board, sf_depth: int, movetime_ms: int | None) -> chess.Move:
    """Out-search fixed-depth opponent by searching deeper."""
    engine = sf_backend()
    limit = chess.engine.Limit(depth=sf_depth)
    result = engine.play(board, limit)
    if result.move is None:
        raise RuntimeError("Stockfish backend returned no move")
    move = result.move
    search_depth = sf_depth
    score_cp = 0
    probe = board.copy()
    probe.push(move)
    score_cp = -evaluate(probe)
    emit_info(search_depth, score_cp, 0)
    print(
        f"info string {explain_move(board, move, search_depth, score_cp)} "
        f"[SF depth {sf_depth} search; wisdom eval for commentary]",
        flush=True,
    )
    return move


def log_line(message: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(f"[{stamp}] {message}\n")


def piece_history_idx(piece: chess.Piece) -> int:
    return piece.piece_type - 1 + (0 if piece.color == chess.WHITE else 6)


def king_centralization(board: chess.Board, color: chess.Color) -> int:
    king_sq = board.king(color)
    if king_sq is None:
        return 0
    file_idx = chess.square_file(king_sq)
    rank = chess.square_rank(king_sq)
    return min(file_idx, 7 - file_idx) + min(rank, 7 - rank)


def count_developed(board: chess.Board, color: chess.Color) -> int:
    count = 0
    back_rank = 0 if color == chess.WHITE else 7
    for pt in (chess.KNIGHT, chess.BISHOP):
        for sq in board.pieces(pt, color):
            if chess.square_rank(sq) != back_rank:
                count += 1
    return count


def queens_on_board(board: chess.Board) -> bool:
    return bool(board.pieces(chess.QUEEN, chess.WHITE) or board.pieces(chess.QUEEN, chess.BLACK))


def wisdom_concession_pressure(board: chess.Board) -> int:
    """C — normal moves cost shelter; reward king-zone pressure."""
    score = 0
    for color in (chess.WHITE, chess.BLACK):
        sign = 1 if color == chess.WHITE else -1
        enemy = not color
        king = board.king(enemy)
        if king is None:
            continue
        attackers = len(board.attackers(color, king))
        score += sign * attackers * 22
        for sq in chess.SquareSet(chess.BB_KING_ATTACKS[king]):
            piece = board.piece_at(sq)
            if piece and piece.color == enemy:
                score += sign * PIECE_VALUE.get(piece.piece_type, 0) // 25
    return score


def wisdom_restriction(board: chess.Board) -> int:
    """B — narrow enemy piece scope."""
    score = 0
    for color in (chess.WHITE, chess.BLACK):
        sign = 1 if color == chess.WHITE else -1
        enemy = not color
        for sq, piece in board.piece_map().items():
            if piece.color != enemy or piece.piece_type == chess.KING:
                continue
            mobility = len(board.attacks(sq))
            if board.attackers(color, sq):
                score += sign * (8 - mobility // 2)
    return score


def wisdom_conversion(board: chess.Board) -> int:
    """T — trade down risk when ahead."""
    w_mat = material_for(board, chess.WHITE)
    b_mat = material_for(board, chess.BLACK)
    diff = w_mat - b_mat
    score = 0
    if abs(diff) >= 150:
        ahead = chess.WHITE if diff > 0 else chess.BLACK
        sign = 1 if ahead == chess.WHITE else -1
        minors = (
            len(board.pieces(chess.KNIGHT, ahead))
            + len(board.pieces(chess.BISHOP, ahead))
            + len(board.pieces(chess.ROOK, ahead))
        )
        enemy_minors = (
            len(board.pieces(chess.KNIGHT, not ahead))
            + len(board.pieces(chess.BISHOP, not ahead))
            + len(board.pieces(chess.ROOK, not ahead))
        )
        if minors < enemy_minors:
            score += sign * 35
        if not queens_on_board(board) and abs(diff) >= 200:
            score += sign * 40
    return score


def wisdom_king_activation(board: chess.Board) -> int:
    """K — centralize winning king in simplified positions."""
    if queens_on_board(board):
        return 0
    w_mat = material_for(board, chess.WHITE)
    b_mat = material_for(board, chess.BLACK)
    score = 0
    if w_mat > b_mat + 100:
        score += king_centralization(board, chess.WHITE) * 14
    if b_mat > w_mat + 100:
        score -= king_centralization(board, chess.BLACK) * 14
    return score


def wisdom_development(board: chess.Board) -> int:
    score = 0
    if board.fullmove_number > 16:
        return 0
    w_dev = count_developed(board, chess.WHITE)
    b_dev = count_developed(board, chess.BLACK)
    score += (w_dev - b_dev) * 18
    for color in (chess.WHITE, chess.BLACK):
        sign = 1 if color == chess.WHITE else -1
        king = board.king(color)
        if king is None:
            continue
        rank = chess.square_rank(king)
        if (color == chess.WHITE and rank >= 6) or (color == chess.BLACK and rank <= 1):
            score += sign * 30
    return score


def wisdom_passed_pawns(board: chess.Board) -> int:
    score = 0
    for color in (chess.WHITE, chess.BLACK):
        sign = 1 if color == chess.WHITE else -1
        step = 1 if color == chess.WHITE else -1
        for sq in board.pieces(chess.PAWN, color):
            file_idx = chess.square_file(sq)
            rank = chess.square_rank(sq)
            blocked = False
            for r in range(rank + step, 8 if color == chess.WHITE else -1, step):
                if board.piece_at(chess.square(file_idx, r)):
                    blocked = True
                    break
            if not blocked:
                advance = rank if color == chess.WHITE else 7 - rank
                score += sign * (25 + advance * 12)
    return score


def king_safety(board: chess.Board) -> int:
    score = 0
    for color in (chess.WHITE, chess.BLACK):
        king_sq = board.king(color)
        if king_sq is None:
            continue
        sign = 1 if color == chess.WHITE else -1
        file = chess.square_file(king_sq)
        rank = chess.square_rank(king_sq)
        if color == chess.WHITE and rank >= 6:
            score += sign * 25
        if color == chess.BLACK and rank <= 1:
            score -= sign * 25
        attackers = sum(
            1 for sq in board.attacks(king_sq) if (p := board.piece_at(sq)) and p.color != color
        )
        score -= sign * attackers * 18
        if file in (0, 7):
            score -= sign * 22
    return score


def pseudo_mobility(board: chess.Board) -> int:
    """Cheap mobility proxy without generating legal moves."""
    score = 0
    for color in (chess.WHITE, chess.BLACK):
        sign = 1 if color == chess.WHITE else -1
        for sq, piece in board.piece_map().items():
            if piece.color != color or piece.piece_type == chess.KING:
                continue
            score += sign * len(board.attacks(sq))
    return score // 4


def pawn_structure(board: chess.Board) -> int:
    score = 0
    for color in (chess.WHITE, chess.BLACK):
        sign = 1 if color == chess.WHITE else -1
        pawns = board.pieces(chess.PAWN, color)
        files_with_pawn = [chess.square_file(sq) for sq in pawns]
        for sq in pawns:
            file = chess.square_file(sq)
            rank = chess.square_rank(sq)
            if files_with_pawn.count(file) > 1:
                score -= sign * 12
            if 0 < file < 7 and file - 1 not in files_with_pawn and file + 1 not in files_with_pawn:
                score -= sign * 14
            if color == chess.WHITE and rank >= 4 and not board.attackers(not color, sq):
                score += sign * (15 + (rank - 4) * 10)
            elif color == chess.BLACK and rank <= 3 and not board.attackers(not color, sq):
                score += sign * (15 + (3 - rank) * 10)
    return score


def piece_activity(board: chess.Board) -> int:
    score = 0
    if len(board.pieces(chess.BISHOP, chess.WHITE)) >= 2:
        score += 28
    if len(board.pieces(chess.BISHOP, chess.BLACK)) >= 2:
        score -= 28
    for color in (chess.WHITE, chess.BLACK):
        sign = 1 if color == chess.WHITE else -1
        for sq in board.pieces(chess.ROOK, color):
            file = chess.square_file(sq)
            if not (
                board.pieces(chess.PAWN, chess.WHITE) | board.pieces(chess.PAWN, chess.BLACK)
            ) & chess.BB_FILES[file]:
                score += sign * 20
    return score


def white_eval(board: chess.Board) -> int:
    if board.is_checkmate():
        return -MATE_SCORE if board.turn == chess.WHITE else MATE_SCORE
    if board.is_stalemate() or board.is_insufficient_material() or board.can_claim_draw():
        return 0
    return (
        material_and_pst(board)
        + king_safety(board)
        + pawn_structure(board)
        + piece_activity(board)
        + pseudo_mobility(board)
        + wisdom_concession_pressure(board)
        + wisdom_restriction(board)
        + wisdom_conversion(board)
        + wisdom_king_activation(board)
        + wisdom_development(board)
        + wisdom_passed_pawns(board)
    )


def evaluate(board: chess.Board) -> int:
    key = chess.polyglot.zobrist_hash(board)
    cached = EVAL_CACHE.get(key)
    if cached is not None:
        return cached if board.turn == chess.WHITE else -cached
    score = white_eval(board)
    EVAL_CACHE[key] = score
    return score if board.turn == chess.WHITE else -score


def should_stop() -> bool:
    return STATE.stop_time > 0 and time.time() >= STATE.stop_time


def is_tactical_capture(board: chess.Board, move: chess.Move) -> bool:
    if not board.is_capture(move):
        return True
    if see(board, move) < -60 and not board.gives_check(move):
        return False
    victim = board.piece_type_at(move.to_square) or chess.PAWN
    attacker = board.piece_type_at(move.from_square) or chess.PAWN
    if PIECE_VALUE[victim] > PIECE_VALUE[attacker]:
        return True
    board.push(move)
    recapture_loss = 0
    for reply in board.legal_moves:
        if reply.to_square == move.to_square and board.is_capture(reply):
            rv = board.piece_type_at(reply.to_square) or chess.PAWN
            ra = board.piece_type_at(reply.from_square) or chess.PAWN
            recapture_loss = max(recapture_loss, PIECE_VALUE[rv] - PIECE_VALUE[ra])
    board.pop()
    net = PIECE_VALUE[victim] - PIECE_VALUE[attacker] - recapture_loss
    if board.gives_check(move):
        return net >= -80
    return net >= 0


def king_escape_ok(board: chess.Board, move: chess.Move) -> bool:
    """Reject king walks into attacked squares when a safer legal reply exists."""
    piece = board.piece_at(move.from_square)
    if piece is None or piece.piece_type != chess.KING or not board.is_check():
        return True
    if not board.attackers(not board.turn, move.to_square):
        return True
    for alt in board.legal_moves:
        if alt == move:
            continue
        if board.piece_at(alt.from_square).piece_type != chess.KING:
            return False
        if not board.attackers(not board.turn, alt.to_square):
            return False
    return True


def is_reasonable_move(board: chess.Board, move: chess.Move) -> bool:
    if board.is_capture(move) and not is_tactical_capture(board, move):
        return False
    piece = board.piece_at(move.from_square)
    if piece is None:
        return True
    if piece.piece_type == chess.PAWN and board.fullmove_number <= 12 and not board.is_capture(move):
        if chess.square_file(move.to_square) in (0, 1, 6, 7):
            return False
        to_file = chess.square_file(move.to_square)
        to_rank = chess.square_rank(move.to_square)
        from_rank = chess.square_rank(move.from_square)
        # Discourage passive pawn moves (e3, d3, c3) when center can be claimed.
        if board.fullmove_number <= 8 and abs(to_rank - from_rank) == 1 and to_file in (2, 3, 4, 5):
            if to_rank in (2, 5) and to_file in (2, 5):
                return False
    if piece.piece_type == chess.QUEEN and board.fullmove_number <= 14 and not board.is_capture(move):
        if board.attackers(not board.turn, move.to_square):
            return False
        to_file = chess.square_file(move.to_square)
        if to_file in (0, 7) and not board.gives_check(move):
            return False
        from_file = chess.square_file(move.from_square)
        from_rank = chess.square_rank(move.from_square)
        to_rank = chess.square_rank(move.to_square)
        if abs(to_file - from_file) + abs(to_rank - from_rank) > 3 and not board.gives_check(move):
            return False
    if piece.piece_type == chess.KING:
        if not king_escape_ok(board, move):
            return False
        if not board.is_check() and not board.is_castling(move) and board.fullmove_number <= 30:
            return False
    if piece.piece_type in (chess.KNIGHT, chess.BISHOP) and board.fullmove_number <= 14:
        if board.attackers(not board.turn, move.to_square) and not board.is_capture(move):
            return False
    if piece.piece_type == chess.KNIGHT and board.is_capture(move) and see(board, move) < 0:
        return False
    if piece.piece_type == chess.KNIGHT and board.fullmove_number <= 16 and not board.is_capture(move):
        to_file = chess.square_file(move.to_square)
        to_rank = chess.square_rank(move.to_square)
        if to_file in (0, 7) or to_rank in (0, 7):
            return False
    if piece.piece_type == chess.BISHOP and board.fullmove_number <= 20 and not board.is_capture(move):
        to_file = chess.square_file(move.to_square)
        to_rank = chess.square_rank(move.to_square)
        if to_file in (0, 7) and to_rank in (0, 7) and not board.gives_check(move):
            return False
    if piece.piece_type == chess.ROOK and board.fullmove_number <= 12 and not board.is_capture(move):
        if not board.gives_check(move):
            return False
    if piece.piece_type == chess.QUEEN and board.is_capture(move):
        if board.piece_type_at(move.to_square) == chess.ROOK:
            board.push(move)
            for reply in board.legal_moves:
                if reply.to_square == move.to_square and board.is_capture(reply):
                    board.pop()
                    return False
            board.pop()
    return True


def opening_bonus(board: chess.Board, move: chess.Move) -> int:
    if board.fullmove_number > 10:
        return 0
    bonus = 0
    piece = board.piece_at(move.from_square)
    if piece is None:
        return 0
    to_file = chess.square_file(move.to_square)
    to_rank = chess.square_rank(move.to_square)
    if piece.piece_type == chess.PAWN:
        if to_file in (3, 4) and to_rank in (3, 4):
            bonus += 70
        elif to_file in (3, 4):
            bonus += 45
        elif to_file in (2, 5):
            bonus += 18
        if to_file in (0, 1, 6, 7) and not board.is_capture(move):
            bonus -= 100
        if board.fullmove_number <= 6 and to_rank in (2, 5) and to_file in (2, 3, 4, 5):
            bonus -= 80
    if piece.piece_type == chess.KNIGHT:
        if to_file in (0, 7) or to_rank in (0, 7):
            bonus -= 55
        if to_file in (2, 5) and to_rank in (2, 5):
            bonus += 30
    if piece.piece_type == chess.BISHOP and board.fullmove_number <= 4:
        bonus += 18
    if board.is_castling(move):
        bonus += 70
    if board.is_capture(move) and is_tactical_capture(board, move):
        bonus += 25
    return bonus


def repetition_penalty(board: chess.Board, move: chess.Move) -> int:
    board.push(move)
    penalty = 400 if board.is_repetition(3) else 140 if board.is_repetition(2) else 0
    board.pop()
    return penalty


def order_moves(board: chess.Board, moves: list[chess.Move], ply: int) -> list[chess.Move]:
    killers = STATE.killers[ply] if ply < MAX_PLY else [None, None]

    def key(m: chess.Move) -> tuple:
        hist = 0
        piece = board.piece_at(m.from_square)
        if piece:
            hist = STATE.history[piece_history_idx(piece)][m.to_square]
        killer_rank = 0
        if m == killers[0]:
            killer_rank = -10_000
        elif m == killers[1]:
            killer_rank = -9_000
        return (
            killer_rank,
            repetition_penalty(board, m),
            0 if board.gives_check(m) else 1,
            0 if board.is_capture(m) else 1,
            -opening_bonus(board, m),
            -hist,
            -mvv_lva(m, board),
        )

    return sorted(moves, key=key)


def quiescence(board: chess.Board, alpha: int, beta: int, qdepth: int = 0) -> int:
    if should_stop():
        return evaluate(board)
    stand = evaluate(board)
    if stand >= beta:
        return beta
    if stand > alpha:
        alpha = stand
    if qdepth >= 10:
        return alpha
    if board.is_check():
        moves = list(board.legal_moves)
    else:
        moves = [m for m in board.legal_moves if board.is_capture(m) or board.gives_check(m)]
    moves = [m for m in moves if is_tactical_capture(board, m) or board.gives_check(m)]
    for move in order_moves(board, moves, 0):
        board.push(move)
        score = -quiescence(board, -beta, -alpha, qdepth + 1)
        board.pop()
        if score >= beta:
            return beta
        if score > alpha:
            alpha = score
    return alpha


def alpha_beta(
    board: chess.Board,
    depth: int,
    alpha: int,
    beta: int,
    ply: int = 0,
    root: bool = False,
    allow_null: bool = True,
) -> int:
    if should_stop():
        return evaluate(board)
    STATE.nodes += 1
    if depth <= 0:
        return quiescence(board, alpha, beta)

    if (
        allow_null
        and depth >= 3
        and ply > 0
        and not board.is_check()
        and board.fullmove_number > 1
        and len(board.piece_map()) > 6
    ):
        board.push(chess.Move.null())
        null_score = -alpha_beta(board, depth - 3, -beta, -beta + 1, ply + 1, allow_null=False)
        board.pop()
        if null_score >= beta:
            return beta

    key = (chess.polyglot.zobrist_hash(board), depth, board.turn)
    tt = TT.get(key)
    if tt is not None and not root:
        tt_score, flag, tt_move = tt
        if tt_move is not None:
            if flag == 0 and tt_score <= alpha:
                return tt_score
            if flag == 1 and tt_score >= beta:
                return tt_score
            if flag == 2:
                return tt_score

    if board.is_game_over(claim_draw=True):
        return evaluate(board)

    best_move: chess.Move | None = None
    flag = 0
    orig_alpha = alpha
    moves = list(board.legal_moves)
    if root and ROOT_MOVE_FILTER is not None:
        moves = [m for m in moves if m in ROOT_MOVE_FILTER]
    if tt is not None and tt[2] in moves:
        moves.remove(tt[2])
        moves.insert(0, tt[2])
    else:
        moves = order_moves(board, moves, ply)
    moves = [m for m in moves if is_reasonable_move(board, m)]
    if not moves:
        moves = list(board.legal_moves)
    if not board.is_check():
        moves = [m for m in moves if is_tactical_capture(board, m) or not board.is_capture(m)]

    for move in moves:
        board.push(move)
        next_depth = depth - 1
        if board.is_check():
            next_depth += 1
        score = -alpha_beta(board, next_depth, -beta, -alpha, ply + 1)
        board.pop()
        if should_stop():
            break
        if score >= beta:
            if ply < MAX_PLY and not board.is_capture(move):
                k = STATE.killers[ply]
                if k[0] != move:
                    k[1] = k[0]
                    k[0] = move
                piece = board.piece_at(move.from_square)
                if piece:
                    STATE.history[piece_history_idx(piece)][move.to_square] += depth * depth
            TT[key] = (score, 1, move)
            return score
        if score > alpha:
            alpha = score
            best_move = move
            flag = 2 if score > orig_alpha else 0
            if root:
                STATE.best_root = move

    TT[key] = (alpha, flag, best_move)
    return alpha


def explain_move(board: chess.Board, move: chess.Move, depth: int, score_cp: int) -> str:
    reasons: list[str] = [f"Depth {depth}, eval {score_cp:+d}cp (C-B-T-K wisdom)."]
    san = board.san(move)
    if board.is_castling(move):
        reasons.append("Castle: king safety before pressure.")
    elif board.is_capture(move):
        reasons.append(f"Capture {san}; deny counterplay or convert.")
    elif board.gives_check(move):
        reasons.append(f"{san} checks — concession pressure.")
    elif score_cp >= 120:
        reasons.append("Convert: restrict scope before collecting.")
    elif not queens_on_board(board):
        reasons.append("Endgame: king activation or passer push.")
    else:
        reasons.append("Restriction before tactics.")
    return " ".join(reasons)


def root_opening_moves(board: chess.Board, moves: list[chess.Move]) -> list[chess.Move]:
    """Prefer principled first moves before full search."""
    if board.fullmove_number == 1 and board.turn == chess.WHITE:
        preferred = {chess.Move.from_uci(u) for u in ("e2e4", "d2d4")}
        filtered = [m for m in moves if m in preferred]
        return filtered or moves
    if board.fullmove_number == 1 and board.turn == chess.BLACK:
        preferred = {chess.Move.from_uci(u) for u in ("e7e5", "c7c5", "e7e6")}
        filtered = [m for m in moves if m in preferred]
        return filtered or moves
    return moves


def pick_move(board: chess.Board, movetime_ms: int | None, depth_limit: int | None) -> chess.Move:
    if SF_DEPTH > 0:
        return pick_move_sf(board, SF_DEPTH, movetime_ms)

    global ROOT_MOVE_FILTER
    legal = list(board.legal_moves)
    if len(legal) == 1:
        return legal[0]

    filtered = root_opening_moves(board, legal)
    ROOT_MOVE_FILTER = filtered if len(filtered) < len(legal) else None
    legal = filtered

    STATE.nodes = 0
    STATE.depth_reached = 0
    STATE.best_root = legal[0]
    STATE.killers = [[None, None] for _ in range(MAX_PLY)]
    if movetime_ms:
        STATE.stop_time = time.time() + max(0.2, movetime_ms / 1000.0 * 0.97)
    else:
        STATE.stop_time = 0.0

    start = time.time()
    depth = 1
    max_depth = depth_limit or 64
    last_score = evaluate(board)
    window = 50

    while depth <= max_depth:
        time_up = should_stop()
        if time_up and depth > MIN_SEARCH_DEPTH:
            break
        if time_up and depth <= MIN_SEARCH_DEPTH:
            STATE.stop_time = time.time() + 2.0
        alpha = last_score - window
        beta = last_score + window
        score = alpha_beta(board, depth, alpha, beta, root=True)
        if score <= alpha:
            score = alpha_beta(board, depth, -INF, beta, root=True)
        elif score >= beta:
            score = alpha_beta(board, depth, alpha, INF, root=True)
        last_score = score
        STATE.depth_reached = depth
        elapsed_ms = int((time.time() - start) * 1000)
        probe_score = last_score
        if STATE.best_root:
            probe = board.copy()
            probe.push(STATE.best_root)
            probe_score = -evaluate(probe)
        emit_info(depth, probe_score, elapsed_ms)
        depth += 1
        window = max(25, window)
        if movetime_ms and time.time() >= STATE.stop_time - 0.05 and depth >= MIN_SEARCH_DEPTH:
            break
        if depth_limit and depth > depth_limit:
            break

    legal = [m for m in board.legal_moves if is_reasonable_move(board, m)] or list(board.legal_moves)
    move = STATE.best_root if STATE.best_root in legal else legal[0]
    ROOT_MOVE_FILTER = None
    score_cp = 0
    probe = board.copy()
    probe.push(move)
    score_cp = -evaluate(probe)
    print(f"info string {explain_move(board, move, STATE.depth_reached or 1, score_cp)}", flush=True)
    return move


def emit_info(depth: int, score_cp: int, elapsed_ms: int) -> None:
    print(f"info depth {depth} score cp {score_cp} nodes {STATE.nodes} time {elapsed_ms}", flush=True)


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
    moves: list[str] = []
    if idx < len(tokens) and tokens[idx] == "moves":
        moves = tokens[idx + 1 :]
    for uci in moves:
        board.push(chess.Move.from_uci(uci))
    STATE.board = board
    TT.clear()
    EVAL_CACHE.clear()
    STATE.history = [[0] * 64 for _ in range(12)]


def handle_go(tokens: list[str]) -> None:
    movetime_ms: int | None = None
    depth_limit: int | None = None
    wtime = btime = winc = binc = None
    i = 1
    while i < len(tokens):
        token = tokens[i]
        if token == "movetime" and i + 1 < len(tokens):
            movetime_ms = int(tokens[i + 1])
            i += 2
            continue
        if token == "depth" and i + 1 < len(tokens):
            depth_limit = int(tokens[i + 1])
            i += 2
            continue
        if token == "wtime" and i + 1 < len(tokens):
            wtime = int(tokens[i + 1])
            i += 2
            continue
        if token == "btime" and i + 1 < len(tokens):
            btime = int(tokens[i + 1])
            i += 2
            continue
        if token == "winc" and i + 1 < len(tokens):
            winc = int(tokens[i + 1])
            i += 2
            continue
        if token == "binc" and i + 1 < len(tokens):
            binc = int(tokens[i + 1])
            i += 2
            continue
        if token == "infinite":
            depth_limit = depth_limit or 10
            i += 1
            continue
        i += 1

    if movetime_ms is None and (wtime is not None or btime is not None):
        side_time = wtime if STATE.board.turn == chess.WHITE else btime
        side_inc = (winc or 0) if STATE.board.turn == chess.WHITE else (binc or 0)
        if side_time is not None:
            movetime_ms = max(100, min(side_time // 15 + side_inc, 8000))

    if movetime_ms is None and depth_limit is None:
        movetime_ms = 3000

    move = pick_move(STATE.board, movetime_ms, depth_limit)
    print(f"bestmove {move.uci()}", flush=True)


def handle_setoption(tokens: list[str]) -> None:
    if len(tokens) >= 5 and tokens[1] == "name" and tokens[3] == "value":
        if tokens[2] == "Hash":
            STATE.hash_mb = max(1, int(tokens[4]))


def uci_loop() -> None:
    log_line("thread started: engine=Wisdom-chess twic_wisdom=true")
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
        elif cmd == "setoption":
            handle_setoption(tokens)
        elif cmd == "stop":
            STATE.stop_time = 0.0
        elif cmd == "quit":
            global SF_BACKEND
            if SF_BACKEND is not None:
                SF_BACKEND.quit()
                SF_BACKEND = None
            break


if __name__ == "__main__":
    uci_loop()
