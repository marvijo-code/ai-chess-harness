"""Standalone UCI chess engine — alpha-beta search, no repo knowledge files."""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import chess


ENGINE_NAME = "Composer-chess"
ENGINE_AUTHOR = "Cursor"
ENGINE_DIR = Path(__file__).resolve().parent
ROOT = Path(__file__).resolve().parents[2]
TRAINING_RULES_PATH = ENGINE_DIR / "training-rules.json"
LOG_DIR = ROOT / "out" / "composer-chess-logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH = LOG_DIR / f"composer-chess-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}.log"

MATE_SCORE = 100_000
INF = MATE_SCORE * 2
MIN_SEARCH_DEPTH = 4


def load_training_rules() -> dict:
    if not TRAINING_RULES_PATH.exists():
        return {"bans": [], "min_search_depth": MIN_SEARCH_DEPTH, "movetime_fraction": 0.85}
    try:
        return json.loads(TRAINING_RULES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"bans": [], "min_search_depth": MIN_SEARCH_DEPTH, "movetime_fraction": 0.85}


TRAINING_RULES = load_training_rules()
TRAINING_BANS = set(TRAINING_RULES.get("bans") or [])
MIN_SEARCH_DEPTH = int(TRAINING_RULES.get("min_search_depth") or MIN_SEARCH_DEPTH)
MOVETIME_FRACTION = float(TRAINING_RULES.get("movetime_fraction") or 0.85)


def refresh_training_rules() -> None:
    global TRAINING_RULES, TRAINING_BANS, MIN_SEARCH_DEPTH, MOVETIME_FRACTION
    TRAINING_RULES = load_training_rules()
    TRAINING_BANS = set(TRAINING_RULES.get("bans") or [])
    MIN_SEARCH_DEPTH = int(TRAINING_RULES.get("min_search_depth") or 4)
    MOVETIME_FRACTION = float(TRAINING_RULES.get("movetime_fraction") or 0.85)

PIECE_VALUE = {
    chess.PAWN: 100,
    chess.KNIGHT: 320,
    chess.BISHOP: 330,
    chess.ROOK: 500,
    chess.QUEEN: 900,
    chess.KING: 0,
}

# Midgame piece-square tables (white perspective; flip rank for black).
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
PST_KING = [
    -30, -40, -40, -50, -50, -40, -40, -30,
    -30, -40, -40, -50, -50, -40, -40, -30,
    -30, -40, -40, -50, -50, -40, -40, -30,
    -30, -40, -40, -50, -50, -40, -40, -30,
    -20, -30, -30, -40, -40, -30, -30, -20,
    -10, -20, -20, -20, -20, -20, -20, -10,
    20, 20, 0, 0, 0, 0, 20, 20,
    20, 30, 10, 0, 0, 10, 30, 20,
]

PST = {
    chess.PAWN: PST_PAWN,
    chess.KNIGHT: PST_KNIGHT,
    chess.BISHOP: PST_BISHOP,
    chess.ROOK: PST_ROOK,
    chess.QUEEN: PST_QUEEN,
    chess.KING: PST_KING,
}


def pst_square(piece: chess.Piece, square: chess.Square) -> int:
    table = PST[piece.piece_type]
    idx = square if piece.color == chess.WHITE else chess.square_mirror(square)
    return table[idx]


def material_and_pst(board: chess.Board) -> int:
    score = 0
    for square, piece in board.piece_map().items():
        sign = 1 if piece.color == chess.WHITE else -1
        score += sign * (PIECE_VALUE[piece.piece_type] + pst_square(piece, square))
    return score


def mvv_lva(move: chess.Move, board: chess.Board) -> int:
    if not board.is_capture(move):
        return 0
    victim = board.piece_type_at(move.to_square) or chess.PAWN
    attacker = board.piece_type_at(move.from_square) or chess.PAWN
    return PIECE_VALUE[victim] * 10 - PIECE_VALUE[attacker]


@dataclass
class SearchState:
    hash_mb: int = 16
    board: chess.Board = field(default_factory=chess.Board)
    stop_time: float = 0.0
    nodes: int = 0
    depth_reached: int = 0
    best_root: chess.Move | None = None


STATE = SearchState()
TT: dict[tuple[int, int, bool], tuple[int, int, chess.Move | None]] = {}


def log_line(message: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {message}"
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def side_name(board: chess.Board) -> str:
    return "white" if board.turn == chess.WHITE else "black"


def piece_label(board: chess.Board, square: chess.Square) -> str:
    piece = board.piece_at(square)
    if piece is None:
        return "empty"
    names = {
        chess.PAWN: "pawn",
        chess.KNIGHT: "knight",
        chess.BISHOP: "bishop",
        chess.ROOK: "rook",
        chess.QUEEN: "queen",
        chess.KING: "king",
    }
    return names.get(piece.piece_type, "piece")


def explain_move(board: chess.Board, move: chess.Move, depth: int, score_cp: int) -> str:
    reasons: list[str] = [f"Depth {depth}, eval {score_cp:+d}cp after search."]
    san = board.san(move)
    if board.is_castling(move):
        reasons.append("Castle: tuck the king and activate the rook.")
    elif board.is_en_passant(move):
        reasons.append("En passant recapture restores pawn structure tempo.")
    elif board.is_capture(move):
        victim = piece_label(board, move.to_square)
        attacker = piece_label(board, move.from_square)
        if board.gives_check(move):
            reasons.append(f"Capture on {chess.square_name(move.to_square)} ({victim}) with check; forcing line.")
        else:
            reasons.append(f"Win {victim} with {attacker}; recapture scan looked sound.")
    elif board.gives_check(move):
        reasons.append(f"{san} gives check; opponent must defend king first.")
    elif board.fullmove_number <= 8:
        if board.piece_type_at(move.from_square) == chess.PAWN:
            reasons.append("Claim or contest central squares before piece skirmishes.")
        elif board.piece_type_at(move.from_square) == chess.KNIGHT:
            reasons.append("Develop a knight toward the center, not the rim.")
        elif board.piece_type_at(move.from_square) == chess.BISHOP:
            reasons.append("Develop a bishop toward open lines.")
        else:
            reasons.append("Finish development instead of shuffling the same piece.")
    else:
        probe = board.copy()
        probe.push(move)
        if probe.is_repetition(2):
            reasons.append("Reject repetition; need progress, not a draw shuffle.")
        elif score_cp >= 150:
            reasons.append("Convert the advantage; improve worst-placed piece or restrict counterplay.")
        elif score_cp <= -150:
            reasons.append("Defend: reduce king exposure and break opponent threats.")
        else:
            reasons.append("Quiet improving move; restrict opponent pieces before tactics.")
    return " ".join(reasons)


def log_decision(board: chess.Board, move: chess.Move, depth: int, score_cp: int, go_args: dict) -> None:
    legal_count = board.legal_moves.count()
    log_line(
        f"decision prompt: side={side_name(board)} fen={board.fen()} legal_moves={legal_count}"
    )
    log_line(f"decision comment: {explain_move(board, move, depth, score_cp)}")
    log_line(
        f"bestmove {move.uci()} from fen={board.fen()} go={go_args}"
    )


def in_check(board: chess.Board) -> bool:
    return board.is_check()


def king_safety(board: chess.Board) -> int:
    """Penalize exposed king; reward castled king behind pawns."""
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
        attackers = 0
        for sq in board.attacks(king_sq):
            piece = board.piece_at(sq)
            if piece and piece.color != color:
                attackers += 1
        score -= sign * attackers * 15
        if file in (0, 7):
            score -= sign * 20
    return score


def mobility(board: chess.Board) -> int:
    own = board.legal_moves.count()
    temp = board.copy()
    temp.turn = not board.turn
    opp = temp.legal_moves.count()
    return (own - opp) * 3


def white_eval(board: chess.Board) -> int:
    """Centipawns from White's point of view (check/draw neutral)."""
    if board.is_checkmate():
        return -MATE_SCORE if board.turn == chess.WHITE else MATE_SCORE
    if board.is_stalemate() or board.is_insufficient_material() or board.can_claim_draw():
        return 0
    return (
        material_and_pst(board)
        + king_safety(board)
        + pawn_structure(board)
        + piece_activity(board)
        + mobility(board)
    )


def evaluate(board: chess.Board) -> int:
    """Centipawns for the side to move."""
    score = white_eval(board)
    return score if board.turn == chess.WHITE else -score


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
            if file > 0 and file < 7 and not any(chess.square_file(s) == file - 1 for s in pawns):
                if not any(chess.square_file(s) == file + 1 for s in pawns):
                    score -= sign * 14
            if color == chess.WHITE:
                if rank >= 4 and not board.attackers(not color, sq):
                    score += sign * (15 + (rank - 4) * 10)
            elif rank <= 3 and not board.attackers(not color, sq):
                score += sign * (15 + (3 - rank) * 10)
    return score


def piece_activity(board: chess.Board) -> int:
    score = 0
    white_bishops = len(board.pieces(chess.BISHOP, chess.WHITE))
    black_bishops = len(board.pieces(chess.BISHOP, chess.BLACK))
    if white_bishops >= 2:
        score += 25
    if black_bishops >= 2:
        score -= 25
    for color in (chess.WHITE, chess.BLACK):
        sign = 1 if color == chess.WHITE else -1
        for sq in board.pieces(chess.ROOK, color):
            file = chess.square_file(sq)
            file_pawns = [
                s
                for s in board.pieces(chess.PAWN, chess.WHITE) | board.pieces(chess.PAWN, chess.BLACK)
                if chess.square_file(s) == file
            ]
            if not file_pawns:
                score += sign * 18
    return score


def should_stop() -> bool:
    return STATE.stop_time > 0 and time.time() >= STATE.stop_time


def is_tactical_capture(board: chess.Board, move: chess.Move) -> bool:
    if not board.is_capture(move):
        return True
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
            swing = PIECE_VALUE[rv] - PIECE_VALUE[ra]
            recapture_loss = max(recapture_loss, swing)
    board.pop()
    net = PIECE_VALUE[victim] - PIECE_VALUE[attacker] - recapture_loss
    if board.gives_check(move):
        return net >= -80
    return net >= 0


def is_reasonable_move(board: chess.Board, move: chess.Move) -> bool:
    """Prune obvious opening and king-march blunders before search."""
    if board.is_capture(move) and not is_tactical_capture(board, move):
        return False
    piece = board.piece_at(move.from_square)
    if piece is None:
        return True
    if piece.piece_type == chess.PAWN and board.fullmove_number <= 12 and not board.is_capture(move):
        to_file = chess.square_file(move.to_square)
        if to_file in (0, 1, 6, 7):
            return False
    if piece.piece_type == chess.QUEEN and board.fullmove_number <= 12 and not board.is_capture(move):
        if board.attackers(not board.turn, move.to_square):
            return False
    if piece.piece_type == chess.KING and not board.is_check() and not board.is_castling(move):
        if board.fullmove_number <= 24:
            to_file = chess.square_file(move.to_square)
            to_rank = chess.square_rank(move.to_square)
            if to_file in (0, 7):
                return False
            if board.turn == chess.WHITE and to_rank >= 4:
                return False
            if board.turn == chess.BLACK and to_rank <= 3:
                return False
    if piece.piece_type == chess.QUEEN and board.is_capture(move):
        victim = board.piece_type_at(move.to_square)
        if victim == chess.ROOK:
            board.push(move)
            for reply in board.legal_moves:
                if reply.to_square == move.to_square and board.is_capture(reply):
                    board.pop()
                    return False
            board.pop()
    if "bishop_corner_shuffle" in TRAINING_BANS and piece.piece_type == chess.BISHOP:
        if board.fullmove_number <= 22 and not board.is_capture(move):
            tf = chess.square_file(move.to_square)
            tr = chess.square_rank(move.to_square)
            if tf in (0, 7) and tr in (0, 7):
                return False
    if "early_queen_a_h_file" in TRAINING_BANS and piece.piece_type == chess.QUEEN:
        if board.fullmove_number <= 16 and not board.is_capture(move):
            if chess.square_file(move.to_square) in (0, 7):
                return False
    if "passive_opening_pawn" in TRAINING_BANS and piece.piece_type == chess.PAWN:
        if board.fullmove_number <= 8 and not board.is_capture(move):
            tr = chess.square_rank(move.to_square)
            if tr in (2, 5):
                return False
    return True


def opening_bonus(board: chess.Board, move: chess.Move) -> int:
    """Prefer sensible development in the first dozen plies."""
    if board.fullmove_number > 10:
        return 0
    bonus = 0
    piece = board.piece_at(move.from_square)
    if piece is None:
        return 0
    to_file = chess.square_file(move.to_square)
    to_rank = chess.square_rank(move.to_square)
    if piece.piece_type == chess.PAWN:
        if to_file in (3, 4):
            bonus += 45
        elif to_file in (2, 5):
            bonus += 15
        if to_file in (0, 1, 6, 7) and not board.is_capture(move):
            bonus -= 90
    if piece.piece_type == chess.KNIGHT:
        if to_file in (0, 7) or to_rank in (0, 7):
            bonus -= 50
        if to_file in (2, 5) and to_rank in (2, 5):
            bonus += 25
    if piece.piece_type == chess.BISHOP and board.fullmove_number <= 4:
        bonus += 15
    if board.is_castling(move):
        bonus += 60
    if board.is_capture(move) and is_tactical_capture(board, move):
        bonus += 20
    return bonus


def repetition_penalty(board: chess.Board, move: chess.Move) -> int:
    """Discourage shuffling when not clearly winning."""
    board.push(move)
    penalty = 0
    if board.is_repetition(2):
        penalty += 120
    if board.is_repetition(3):
        penalty += 400
    board.pop()
    piece = board.piece_at(move.from_square)
    if piece and piece.piece_type in {chess.ROOK, chess.QUEEN}:
        if board.fullmove_number > 8 and not board.is_capture(move) and not board.gives_check(move):
            board.push(move)
            if board.is_check():
                penalty += 40
            board.pop()
    return penalty


def order_moves(board: chess.Board, moves: list[chess.Move]) -> list[chess.Move]:
    return sorted(
        moves,
        key=lambda m: (
            repetition_penalty(board, m),
            0 if board.gives_check(m) else 1,
            0 if board.is_capture(m) else 1,
            -opening_bonus(board, m),
            -mvv_lva(m, board),
        ),
    )


def quiescence(board: chess.Board, alpha: int, beta: int) -> int:
    if should_stop():
        return evaluate(board)
    stand = evaluate(board)
    if stand >= beta:
        return beta
    if stand > alpha:
        alpha = stand
    if in_check(board):
        moves = list(board.legal_moves)
    else:
        moves = [m for m in board.legal_moves if board.is_capture(m) or board.gives_check(m)]
    moves = [m for m in moves if is_tactical_capture(board, m) or board.gives_check(m)]
    for move in order_moves(board, moves):
        board.push(move)
        score = -quiescence(board, -beta, -alpha)
        board.pop()
        if score >= beta:
            return beta
        if score > alpha:
            alpha = score
    return alpha


def alpha_beta(board: chess.Board, depth: int, alpha: int, beta: int, root: bool = False, allow_null: bool = True) -> int:
    if should_stop():
        return evaluate(board)
    STATE.nodes += 1
    if depth <= 0:
        return quiescence(board, alpha, beta)

    if (
        allow_null
        and depth >= 3
        and not in_check(board)
        and board.fullmove_number > 1
        and len(board.piece_map()) > 6
    ):
        board.push(chess.Move.null())
        null_score = -alpha_beta(board, depth - 3, -beta, -beta + 1, allow_null=False)
        board.pop()
        if null_score >= beta:
            return beta

    key = (board.fen(), depth, board.turn)
    tt = TT.get(key)
    if tt is not None:
        tt_score, _flag, tt_move = tt
        if tt_move is not None and not root:
            if _flag == 0 and tt_score <= alpha:
                return tt_score
            if _flag == 1 and tt_score >= beta:
                return tt_score
            if _flag == 2:
                return tt_score

    if board.is_game_over(claim_draw=True):
        return evaluate(board)

    best_move: chess.Move | None = None
    flag = 0
    orig_alpha = alpha
    moves = list(board.legal_moves)
    if tt is not None and tt[2] in moves:
        moves.remove(tt[2])
        moves.insert(0, tt[2])
    else:
        moves = order_moves(board, moves)
    moves = [m for m in moves if is_reasonable_move(board, m)]
    if not moves:
        moves = list(board.legal_moves)
    if not in_check(board):
        moves = [m for m in moves if is_tactical_capture(board, m) or not board.is_capture(m)]

    for move in moves:
        board.push(move)
        next_depth = depth - 1
        if in_check(board):
            next_depth += 1
        score = -alpha_beta(board, next_depth, -beta, -alpha)
        board.pop()
        if should_stop():
            break
        if score >= beta:
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


def pick_move(board: chess.Board, movetime_ms: int | None, depth_limit: int | None) -> chess.Move:
    refresh_training_rules()
    legal = list(board.legal_moves)
    if len(legal) == 1:
        return legal[0]

    STATE.nodes = 0
    STATE.depth_reached = 0
    STATE.best_root = legal[0]
    if movetime_ms:
        STATE.stop_time = time.time() + max(0.05, movetime_ms / 1000.0 * MOVETIME_FRACTION)
    else:
        STATE.stop_time = 0.0

    start = time.time()
    depth = 1
    max_depth = depth_limit or 64
    while depth <= max_depth:
        if should_stop() and depth > MIN_SEARCH_DEPTH:
            break
        if should_stop() and depth <= MIN_SEARCH_DEPTH:
            STATE.stop_time = time.time() + 1.5
        alpha_beta(board, depth, -INF, INF, root=True)
        STATE.depth_reached = depth
        elapsed_ms = int((time.time() - start) * 1000)
        score = evaluate(board) if STATE.best_root is None else None
        if STATE.best_root is not None:
            probe = board.copy()
            probe.push(STATE.best_root)
            score = -evaluate(probe)
        emit_info(depth, score or 0, elapsed_ms)
        depth += 1
        if depth_limit and depth > depth_limit:
            break

    legal = list(STATE.board.legal_moves)
    legal = [m for m in legal if is_reasonable_move(STATE.board, m)] or legal
    scored: list[tuple[int, chess.Move]] = []
    for candidate in order_moves(STATE.board, legal):
        probe = STATE.board.copy()
        probe.push(candidate)
        scored.append((evaluate(probe), candidate))
    scored.sort(key=lambda item: item[0], reverse=True)
    if scored and (scored[0][0] - (scored[1][0] if len(scored) > 1 else scored[0][0] - 1)) < 5:
        for _, candidate in scored:
            probe = STATE.board.copy()
            probe.push(candidate)
            if not probe.is_repetition(2):
                STATE.best_root = candidate
                break

    move = STATE.best_root or legal[0]
    if move not in legal:
        move = legal[0]
    score_cp = 0
    if move in legal:
        probe = STATE.board.copy()
        probe.push(move)
        score_cp = -evaluate(probe)
    go_args = {"movetime": movetime_ms, "depth": depth_limit}
    thought = explain_move(STATE.board, move, STATE.depth_reached or 1, score_cp)
    print(f"info string {thought}", flush=True)
    return move


def emit_info(depth: int, score_cp: int, elapsed_ms: int) -> None:
    print(f"info depth {depth} score cp {score_cp} nodes {STATE.nodes} time {elapsed_ms}", flush=True)


def handle_position(tokens: list[str]) -> None:
    idx = 1
    moves: list[str] = []
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
        idx += 1
        moves = tokens[idx:]
    for uci in moves:
        board.push(chess.Move.from_uci(uci))
    STATE.board = board
    TT.clear()


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
            depth_limit = depth_limit or 8
            i += 1
            continue
        i += 1

    if movetime_ms is None and (wtime is not None or btime is not None):
        side_time = wtime if STATE.board.turn == chess.WHITE else btime
        side_inc = (winc or 0) if STATE.board.turn == chess.WHITE else (binc or 0)
        if side_time is not None:
            movetime_ms = max(50, min(side_time // 20 + side_inc, 3000))

    if movetime_ms is None and depth_limit is None:
        movetime_ms = 1000

    move = pick_move(STATE.board, movetime_ms, depth_limit)
    print(f"bestmove {move.uci()}", flush=True)


def handle_setoption(tokens: list[str]) -> None:
    if len(tokens) >= 5 and tokens[1] == "name" and tokens[3] == "value":
        name = tokens[2]
        value = tokens[4]
        if name == "Hash":
            STATE.hash_mb = max(1, int(value))


def uci_loop() -> None:
    log_line("thread started: engine=Composer-chess context=composer-chess first_principles=true")
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
            print("option name Hash type spin default 16 min 1 max 512")
            print("uciok", flush=True)
        elif cmd == "isready":
            print("readyok", flush=True)
        elif cmd == "ucinewgame":
            TT.clear()
            STATE.board = chess.Board()
        elif cmd == "position":
            handle_position(tokens)
        elif cmd == "go":
            handle_go(tokens)
        elif cmd == "setoption":
            handle_setoption(tokens)
        elif cmd == "stop":
            STATE.stop_time = 0.0
        elif cmd == "quit":
            break


if __name__ == "__main__":
    uci_loop()
