from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import chess


ENGINE_DIR = Path(__file__).resolve().parent
ROOT = ENGINE_DIR.parents[1]
RESEARCH_DIR = ENGINE_DIR / "research"
SELFPLAY_DIR = RESEARCH_DIR / "selfplay"
NETWORK_DIR = RESEARCH_DIR / "networks"
WISDOM_DIR = RESEARCH_DIR / "wisdom"
CURRENT_NETWORK_PATH = NETWORK_DIR / "current-network.json"
CANDIDATE_NETWORK_PATH = NETWORK_DIR / "candidate-network.json"
REPLAY_BUFFER_PATH = RESEARCH_DIR / "replay-buffer.jsonl"
PROMOTION_LOG_PATH = RESEARCH_DIR / "promotion-log.jsonl"
SUMMARY_PATH = RESEARCH_DIR / "summary.json"
WISDOM_LATEST_JSON_PATH = WISDOM_DIR / "latest-wisdom-delta.json"
WISDOM_LATEST_MARKDOWN_PATH = WISDOM_DIR / "latest-wisdom-delta.md"
WISDOM_LOG_PATH = WISDOM_DIR / "wisdom-log.jsonl"
ZERO_MEMORY_PATH = ENGINE_DIR / "MEMORY.md"
ZERO_KNOWLEDGEBASE_DIR = ENGINE_DIR / "knowledgebase"
ENGINE_CONFIG_PATH = Path.home() / "AppData/Roaming/org.encroissant.app/engines/engines.json"

MOVE_PROMOTIONS = ["", "n", "b", "r", "q"]
MOVE_VOCAB_SIZE = 64 * 64 * len(MOVE_PROMOTIONS)
BOARD_PLANE_NAMES = [
    "white_pawn",
    "white_knight",
    "white_bishop",
    "white_rook",
    "white_queen",
    "white_king",
    "black_pawn",
    "black_knight",
    "black_bishop",
    "black_rook",
    "black_queen",
    "black_king",
    "white_to_move",
    "white_kingside_castle",
    "white_queenside_castle",
    "black_kingside_castle",
    "black_queenside_castle",
    "en_passant_target",
]
PIECE_TO_PLANE = {
    (chess.WHITE, chess.PAWN): 0,
    (chess.WHITE, chess.KNIGHT): 1,
    (chess.WHITE, chess.BISHOP): 2,
    (chess.WHITE, chess.ROOK): 3,
    (chess.WHITE, chess.QUEEN): 4,
    (chess.WHITE, chess.KING): 5,
    (chess.BLACK, chess.PAWN): 6,
    (chess.BLACK, chess.KNIGHT): 7,
    (chess.BLACK, chess.BISHOP): 8,
    (chess.BLACK, chess.ROOK): 9,
    (chess.BLACK, chess.QUEEN): 10,
    (chess.BLACK, chess.KING): 11,
}
PIECE_VALUES = {
    chess.PAWN: 100,
    chess.KNIGHT: 300,
    chess.BISHOP: 300,
    chess.ROOK: 500,
    chess.QUEEN: 900,
    chess.KING: 0,
}
SELF_PLAY_DRAW_PENALTY = -0.05
SELF_PLAY_FAILED_CONVERSION_DRAW_PENALTY = -0.18
SELF_PLAY_DEFENSIVE_DRAW_PENALTY = -0.02
DEFAULT_TRAINING_REPLAY_LIMIT = 4096
TRAINING_REPLAY_RECENT_FRACTION = 0.5
DEFAULT_WEIGHTS = {
    "bias": 0.0,
    "capture_value": 1.0,
    "material_delta": 1.6,
    "gives_check": 0.35,
    "promotion_value": 1.4,
    "center_to": 0.28,
    "development": 0.18,
    "castle": 0.32,
    "conversion_stall": 0.0,
    "moved_piece_risk": -1.15,
    "king_move_early": -0.25,
    "value_material": 1.0,
    "value_mobility": 0.08,
    "value_king_safety": 0.10,
}
WEIGHT_BOUNDS = {
    "bias": (-2.0, 2.0),
    "capture_value": (-4.0, 4.0),
    "material_delta": (-4.0, 4.0),
    "gives_check": (-3.0, 4.0),
    "promotion_value": (-4.0, 4.0),
    "center_to": (-4.0, 4.0),
    "development": (-4.0, 4.0),
    "castle": (-4.0, 4.0),
    "conversion_stall": (-6.0, 0.5),
    "moved_piece_risk": (-8.0, 0.5),
    "king_move_early": (-4.0, 1.0),
    "value_material": (-12.0, 12.0),
    "value_mobility": (-6.0, 6.0),
    "value_king_safety": (-8.0, 8.0),
}
FORBIDDEN_TRAINING_SOURCES = ("stockfish", "lc0", "leela", "maia", "human_games", "opening_book", "tablebase")
FEN_RE = re.compile(
    r"\b(?:[pnbrqkPNBRQK1-8]+/){7}[pnbrqkPNBRQK1-8]+\s+[wb]\s+(?:K?Q?k?q?|-)\s+(?:[a-h][36]|-)\s+\d+\s+\d+\b"
)
UCI_RE = re.compile(r"\b[a-h][1-8][a-h][1-8][qrbn]?\b")
DELIBERATIVE_CONTROLLER = "deliberative-human-v1"
DELIBERATIVE_REFUTATION_CANDIDATE_LIMIT = 12
DELIBERATIVE_SAFE_CANDIDATE_LIMIT = 4
DELIBERATIVE_RETURNED_CANDIDATE_LIMIT = 8


def ensure_dirs() -> None:
    SELFPLAY_DIR.mkdir(parents=True, exist_ok=True)
    NETWORK_DIR.mkdir(parents=True, exist_ok=True)
    WISDOM_DIR.mkdir(parents=True, exist_ok=True)


def now_stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def file_line_count(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return sum(1 for _ in handle)
    except OSError:
        return 0


def material_balance(board: chess.Board, color: bool) -> int:
    total = 0
    for piece in board.piece_map().values():
        value = PIECE_VALUES.get(piece.piece_type, 0)
        total += value if piece.color == color else -value
    return total


def board_planes(board: chess.Board) -> dict:
    planes = [[[0 for _ in range(8)] for _ in range(8)] for _ in BOARD_PLANE_NAMES]
    for square, piece in board.piece_map().items():
        plane = PIECE_TO_PLANE[(piece.color, piece.piece_type)]
        rank = chess.square_rank(square)
        file = chess.square_file(square)
        planes[plane][rank][file] = 1
    fill_planes = {
        12: board.turn == chess.WHITE,
        13: board.has_kingside_castling_rights(chess.WHITE),
        14: board.has_queenside_castling_rights(chess.WHITE),
        15: board.has_kingside_castling_rights(chess.BLACK),
        16: board.has_queenside_castling_rights(chess.BLACK),
    }
    for plane_index, enabled in fill_planes.items():
        if enabled:
            for rank in range(8):
                for file in range(8):
                    planes[plane_index][rank][file] = 1
    if board.ep_square is not None:
        planes[17][chess.square_rank(board.ep_square)][chess.square_file(board.ep_square)] = 1
    return {
        "shape": [len(planes), 8, 8],
        "plane_names": list(BOARD_PLANE_NAMES),
        "planes": planes,
    }


def move_to_index(move: chess.Move) -> int:
    promotion_index = MOVE_PROMOTIONS.index(chess.piece_symbol(move.promotion) if move.promotion else "")
    return ((move.from_square * 64) + move.to_square) * len(MOVE_PROMOTIONS) + promotion_index


def index_to_move(index: int) -> chess.Move:
    promotion_index = index % len(MOVE_PROMOTIONS)
    base = index // len(MOVE_PROMOTIONS)
    to_square = base % 64
    from_square = base // 64
    promotion_text = MOVE_PROMOTIONS[promotion_index]
    return chess.Move(from_square, to_square, promotion=chess.Piece.from_symbol(promotion_text).piece_type if promotion_text else None)


def legal_move_mask(board: chess.Board) -> dict:
    moves = list(board.legal_moves)
    indices = sorted(move_to_index(move) for move in moves)
    return {
        "size": MOVE_VOCAB_SIZE,
        "indices": indices,
        "moves": [move.uci() for move in moves],
    }


def position_key(fen: str) -> str:
    return " ".join(str(fen).split()[:4])


def stable_id(payload: object, length: int = 12) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:length]


def clamp_learned_weights(weights: dict[str, float]) -> dict[str, float]:
    clamped = dict(weights)
    for name, (low, high) in WEIGHT_BOUNDS.items():
        if name in clamped:
            clamped[name] = max(low, min(high, float(clamped[name])))
    return clamped


def moved_piece_risk(board: chess.Board, move: chess.Move) -> float:
    mover = board.piece_at(move.from_square)
    if mover is None:
        return 0.0
    after = board.copy(stack=False)
    after.push(move)
    moved_after = after.piece_at(move.to_square)
    if moved_after is None:
        return 0.0
    worst = 0
    for reply in after.legal_moves:
        if after.is_capture(reply) and reply.to_square == move.to_square:
            reply_board = after.copy(stack=False)
            before = material_balance(after, mover.color)
            reply_board.push(reply)
            swing = before - material_balance(reply_board, mover.color)
            worst = max(worst, swing)
    return min(1.0, max(0.0, worst / 900.0))


def move_features(board: chess.Board, move: chess.Move) -> dict[str, float]:
    color = board.turn
    mover = board.piece_at(move.from_square)
    before_balance = material_balance(board, color)
    after = board.copy(stack=False)
    after.push(move)
    after_balance = material_balance(after, color)
    captured = board.piece_at(move.to_square)
    if board.is_en_passant(move):
        offset = -8 if color == chess.WHITE else 8
        captured = board.piece_at(move.to_square + offset)
    to_file = chess.square_file(move.to_square)
    to_rank = chess.square_rank(move.to_square)
    from_rank = chess.square_rank(move.from_square)
    development = 0.0
    if mover and mover.piece_type in {chess.KNIGHT, chess.BISHOP}:
        development = 1.0 if (color == chess.WHITE and from_rank == 0) or (color == chess.BLACK and from_rank == 7) else 0.0
    pawn_progress = 0.0
    if mover and mover.piece_type == chess.PAWN:
        pawn_progress = 1.0 if (color == chess.WHITE and to_rank > from_rank) or (color == chess.BLACK and to_rank < from_rank) else 0.0
    conversion_stall = 0.0
    if before_balance >= 500:
        progress = (
            board.is_capture(move)
            or bool(move.promotion)
            or board.gives_check(move)
            or board.is_castling(move)
            or bool(pawn_progress)
            or bool(development)
            or bool(created_threats(board, move))
        )
        conversion_stall = 0.0 if progress else 1.0
    return {
        "bias": 1.0,
        "capture_value": (PIECE_VALUES.get(captured.piece_type, 0) / 900.0) if captured else 0.0,
        "material_delta": max(-1.0, min(1.0, (after_balance - before_balance) / 900.0)),
        "gives_check": 1.0 if board.gives_check(move) else 0.0,
        "promotion_value": (PIECE_VALUES.get(move.promotion, 0) / 900.0) if move.promotion else 0.0,
        "center_to": 1.0 if to_file in {3, 4} and to_rank in {3, 4} else 0.0,
        "development": development,
        "castle": 1.0 if board.is_castling(move) else 0.0,
        "conversion_stall": conversion_stall,
        "moved_piece_risk": moved_piece_risk(board, move),
        "king_move_early": 1.0 if mover and mover.piece_type == chess.KING and board.fullmove_number <= 12 and not board.is_castling(move) else 0.0,
    }


def board_value_features(board: chess.Board) -> dict[str, float]:
    color = board.turn
    own_king = board.king(color)
    enemy_king = board.king(not color)
    own_attackers = len(board.attackers(not color, own_king)) if own_king is not None else 0
    enemy_attackers = len(board.attackers(color, enemy_king)) if enemy_king is not None else 0
    return {
        "value_material": max(-1.0, min(1.0, material_balance(board, color) / 1200.0)),
        "value_mobility": max(-1.0, min(1.0, len(list(board.legal_moves)) / 40.0)),
        "value_king_safety": max(-1.0, min(1.0, (enemy_attackers - own_attackers) / 4.0)),
    }


@dataclass
class NetworkEvaluation:
    priors: dict[str, float]
    value: float


@dataclass
class PolicyValueNetwork:
    network_id: str = "zero-bootstrap"
    generation: int = 0
    created_at: str = field(default_factory=now_stamp)
    training_steps: int = 0
    source_positions: int = 0
    weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))

    @classmethod
    def load(cls, path: Path = CURRENT_NETWORK_PATH) -> "PolicyValueNetwork":
        ensure_dirs()
        if not path.exists():
            network = cls()
            network.save(path)
            return network
        data = json.loads(path.read_text(encoding="utf-8"))
        weights = dict(DEFAULT_WEIGHTS)
        weights.update({key: float(value) for key, value in data.get("weights", {}).items()})
        weights = clamp_learned_weights(weights)
        return cls(
            network_id=str(data.get("network_id") or "zero-bootstrap"),
            generation=int(data.get("generation") or 0),
            created_at=str(data.get("created_at") or now_stamp()),
            training_steps=int(data.get("training_steps") or 0),
            source_positions=int(data.get("source_positions") or 0),
            weights=weights,
        )

    def to_dict(self) -> dict:
        return {
            "schema": "zero-policy-value-network-v1",
            "network_id": self.network_id,
            "generation": self.generation,
            "created_at": self.created_at,
            "training_steps": self.training_steps,
            "source_positions": self.source_positions,
            "weights": self.weights,
            "training_sources": {source: False for source in FORBIDDEN_TRAINING_SOURCES},
        }

    def save(self, path: Path = CURRENT_NETWORK_PATH) -> None:
        ensure_dirs()
        path.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def move_logit(self, board: chess.Board, move: chess.Move) -> float:
        features = move_features(board, move)
        return sum(self.weights.get(name, 0.0) * value for name, value in features.items())

    def value(self, board: chess.Board) -> float:
        if board.is_game_over(claim_draw=True):
            return terminal_value(board)
        features = board_value_features(board)
        raw = sum(self.weights.get(name, 0.0) * value for name, value in features.items())
        return math.tanh(raw)

    def evaluate(self, board: chess.Board, legal_moves: Iterable[chess.Move] | None = None) -> NetworkEvaluation:
        moves = list(legal_moves if legal_moves is not None else board.legal_moves)
        if not moves:
            return NetworkEvaluation({}, self.value(board))
        logits = {move.uci(): self.move_logit(board, move) for move in moves}
        max_logit = max(logits.values())
        exp_values = {uci: math.exp(logit - max_logit) for uci, logit in logits.items()}
        total = sum(exp_values.values()) or 1.0
        return NetworkEvaluation({uci: value / total for uci, value in exp_values.items()}, self.value(board))


@dataclass
class PuctNode:
    board: chess.Board
    prior: float = 1.0
    move: chess.Move | None = None
    visit_count: int = 0
    value_sum: float = 0.0
    children: dict[str, "PuctNode"] = field(default_factory=dict)
    expanded: bool = False

    @property
    def value(self) -> float:
        return self.value_sum / self.visit_count if self.visit_count else 0.0


@dataclass
class ZeroSearchResult:
    move: chess.Move
    network_id: str
    root_value: float
    visits: int
    nodes: int
    candidates: list[dict]
    explanation: dict
    comment: str

    def to_dict(self) -> dict:
        return {
            "move": self.move.uci(),
            "network_id": self.network_id,
            "root_value": self.root_value,
            "visits": self.visits,
            "nodes": self.nodes,
            "candidates": self.candidates,
            "explanation": self.explanation,
            "comment": self.comment,
        }


def terminal_value(board: chess.Board) -> float:
    outcome = board.outcome(claim_draw=True)
    if outcome is None or outcome.winner is None:
        return 0.0
    return 1.0 if outcome.winner == board.turn else -1.0


def expand(node: PuctNode, network: PolicyValueNetwork) -> float:
    if node.board.is_game_over(claim_draw=True):
        node.expanded = True
        return terminal_value(node.board)
    evaluation = network.evaluate(node.board)
    node.children = {}
    for move in node.board.legal_moves:
        child_board = node.board.copy(stack=False)
        child_board.push(move)
        node.children[move.uci()] = PuctNode(child_board, prior=evaluation.priors.get(move.uci(), 0.0), move=move)
    node.expanded = True
    return evaluation.value


def puct_score(parent: PuctNode, child: PuctNode, c_puct: float) -> float:
    q_value = -child.value if child.visit_count else 0.0
    exploration = c_puct * child.prior * math.sqrt(max(1, parent.visit_count)) / (1 + child.visit_count)
    return q_value + exploration


def select_child(node: PuctNode, c_puct: float) -> PuctNode:
    return max(node.children.values(), key=lambda child: (puct_score(node, child, c_puct), child.prior, child.move.uci() if child.move else ""))


def backup(path: list[PuctNode], value: float) -> None:
    for node in reversed(path):
        node.visit_count += 1
        node.value_sum += value
        value = -value


def created_threats(board: chess.Board, move: chess.Move) -> list[dict]:
    color = board.turn
    after = board.copy(stack=False)
    after.push(move)
    piece = after.piece_at(move.to_square)
    if piece is None:
        return []
    threats = []
    for target in after.attacks(move.to_square):
        target_piece = after.piece_at(target)
        if target_piece and target_piece.color != color and PIECE_VALUES[target_piece.piece_type] >= 300:
            threats.append(
                {
                    "square": chess.square_name(target),
                    "piece": target_piece.symbol(),
                    "value_cp": PIECE_VALUES[target_piece.piece_type],
                }
            )
    threats.sort(key=lambda item: (-item["value_cp"], item["square"]))
    return threats[:4]


def move_role_tags(board: chess.Board, move: chess.Move, features: dict[str, float] | None = None) -> list[str]:
    features = features or move_features(board, move)
    mover = board.piece_at(move.from_square)
    tags: list[str] = []
    if board.gives_check(move):
        tags.append("forcing_check")
    if board.is_capture(move):
        tags.append("capture")
    if move.promotion:
        tags.append("promotion")
    if board.is_castling(move):
        tags.append("king_safety")
    if features["development"]:
        tags.append("development")
    if features["center_to"]:
        tags.append("center_control")
    if mover and mover.piece_type == chess.PAWN:
        from_rank = chess.square_rank(move.from_square)
        to_rank = chess.square_rank(move.to_square)
        if abs(to_rank - from_rank) == 2 or board.is_capture(move):
            tags.append("pawn_break")
    if created_threats(board, move):
        tags.append("creates_threat")
    if features["moved_piece_risk"] >= 0.5:
        tags.append("tactical_risk")
    return tags or ["quiet_improvement"]


def plan_intent_for_move(board: chess.Board, move: chess.Move, features: dict[str, float] | None = None) -> str:
    features = features or move_features(board, move)
    threats = created_threats(board, move)
    if board.gives_check(move):
        return "force the opponent to answer check before continuing the plan"
    if move.promotion:
        return "convert a passed pawn into material"
    if board.is_capture(move):
        return "win or simplify material only if the reply check stays safe"
    if board.is_castling(move):
        return "improve king safety and connect the rooks"
    if threats:
        target = threats[0]
        return f"create pressure on {target['piece']} at {target['square']}"
    if features["development"]:
        return "develop a piece toward the fight"
    if features["center_to"]:
        return "contest central squares and improve mobility"
    return "improve piece coordination without taking immediate tactical risk"


def refutation_check(board: chess.Board, move: chess.Move) -> dict:
    reply = best_reply_summary(board, move)
    swing = int(reply.get("material_swing_cp", 0) or 0)
    if reply.get("san") == "terminal":
        status = "terminal"
    elif reply.get("is_checkmate"):
        status = "unsafe"
    elif swing >= 500:
        status = "unsafe"
    elif swing >= 300 or moved_piece_risk(board, move) >= 0.5 or reply.get("gives_check"):
        status = "watch"
    else:
        status = "ok"
    return {
        "status": status,
        "reply": reply,
        "note": (
            "opponent has a major material refutation"
            if status == "unsafe"
            else "opponent reply needs checking"
            if status == "watch"
            else "no immediate material refutation found"
        ),
    }


def deliberative_score(
    board: chess.Board,
    child: PuctNode,
    root_visits: int,
    features: dict[str, float],
    refutation: dict,
) -> float:
    assert child.move is not None
    value = -child.value
    visit_share = child.visit_count / max(1, root_visits)
    threat_bonus = min(0.2, sum(item["value_cp"] for item in created_threats(board, child.move)) / 4500.0)
    refutation_penalty = min(1.0, max(0.0, float(refutation.get("reply", {}).get("material_swing_cp", 0)) / 900.0))
    forcing_reply_penalty = 1.0 if refutation.get("reply", {}).get("is_checkmate") else 0.25 if refutation.get("reply", {}).get("gives_check") else 0.0
    refutation_status = str(refutation.get("status", "ok"))
    forcing_multiplier = 0.0 if refutation_status == "unsafe" else 0.35 if refutation_status == "watch" else 1.0
    refutation_status_penalty = 1.05 if refutation_status == "unsafe" else 0.30 if refutation_status == "watch" else 0.0
    forcing_bonus = forcing_multiplier * (
        0.16 * features["gives_check"]
        + 0.12 * features["capture_value"]
        + 0.26 * features["promotion_value"]
        + threat_bonus
    )
    score = (
        0.35 * visit_share
        + 0.25 * child.prior
        + 0.30 * value
        + forcing_bonus
        + 0.10 * features["castle"]
        + 0.08 * features["development"]
        + 0.07 * features["center_to"]
        - 0.24 * features["conversion_stall"]
        - 0.62 * features["moved_piece_risk"]
        - 0.72 * refutation_penalty
        - 0.33 * forcing_reply_penalty
        - refutation_status_penalty
        - 0.18 * features["king_move_early"]
    )
    return round(score, 6)


def cheap_deliberative_child_score(board: chess.Board, child: PuctNode, root_visits: int) -> tuple[float, int, str]:
    assert child.move is not None
    features = move_features(board, child.move)
    threat_bonus = min(0.2, sum(item["value_cp"] for item in created_threats(board, child.move)) / 4500.0)
    score = (
        0.35 * (child.visit_count / max(1, root_visits))
        + 0.25 * child.prior
        + 0.30 * (-child.value)
        + 0.16 * features["gives_check"]
        + 0.12 * features["capture_value"]
        + 0.26 * features["promotion_value"]
        + threat_bonus
        + 0.10 * features["castle"]
        + 0.08 * features["development"]
        + 0.07 * features["center_to"]
        - 0.24 * features["conversion_stall"]
        - 0.62 * features["moved_piece_risk"]
        - 0.18 * features["king_move_early"]
    )
    return (round(score, 6), child.visit_count, child.move.uci())


def safe_deliberative_child_score(board: chess.Board, child: PuctNode) -> tuple[float, float, float, float, int, str]:
    assert child.move is not None
    features = move_features(board, child.move)
    is_forcing = bool(features["gives_check"]) or board.is_capture(child.move) or bool(child.move.promotion)
    quiet_or_safety = (not is_forcing) or bool(features["castle"])
    return (
        1.0 if quiet_or_safety else 0.0,
        -features["moved_piece_risk"],
        -features["conversion_stall"],
        features["material_delta"],
        child.visit_count,
        child.move.uci(),
    )


def select_deliberative_candidate_children(
    board: chess.Board,
    root: PuctNode,
    children: list[PuctNode],
    limit: int,
) -> list[PuctNode]:
    root_visits = max(1, root.visit_count)
    prelimit = max(limit, DELIBERATIVE_REFUTATION_CANDIDATE_LIMIT)
    safety_slots = min(DELIBERATIVE_SAFE_CANDIDATE_LIMIT, max(0, prelimit - 1))
    primary_limit = max(1, prelimit - safety_slots)
    cheap_ranked = sorted(
        children,
        key=lambda child: cheap_deliberative_child_score(board, child, root_visits),
        reverse=True,
    )
    selected: dict[str, PuctNode] = {}
    for child in cheap_ranked[:primary_limit]:
        assert child.move is not None
        selected[child.move.uci()] = child
    safe_ranked = sorted(
        [child for child in children if child.move and child.move.uci() not in selected],
        key=lambda child: safe_deliberative_child_score(board, child),
        reverse=True,
    )
    for child in safe_ranked:
        assert child.move is not None
        features = move_features(board, child.move)
        if features["moved_piece_risk"] >= 0.5 or features["king_move_early"]:
            continue
        selected[child.move.uci()] = child
        if len(selected) >= prelimit:
            break
    for child in cheap_ranked:
        assert child.move is not None
        if len(selected) >= prelimit:
            break
        selected.setdefault(child.move.uci(), child)
    return list(selected.values())


def deliberative_candidate_rows(board: chess.Board, root: PuctNode, limit: int | None = 8) -> list[dict]:
    rows = []
    root_visits = max(1, root.visit_count)
    children = [child for child in root.children.values() if child.move is not None]
    if limit is not None and len(children) > limit:
        children = select_deliberative_candidate_children(board, root, children, limit)
    for child in children:
        assert child.move is not None
        features = move_features(board, child.move)
        refutation = refutation_check(board, child.move)
        rows.append(
            {
                "uci": child.move.uci(),
                "san": board.san(child.move),
                "role_tags": move_role_tags(board, child.move, features),
                "plan_intent": plan_intent_for_move(board, child.move, features),
                "visits": child.visit_count,
                "visit_share": round(child.visit_count / root_visits, 6),
                "prior": round(child.prior, 6),
                "value": round(-child.value, 6),
                "human_score": deliberative_score(board, child, root_visits, features, refutation),
                "risk": round(features["moved_piece_risk"], 6),
                "gives_check": bool(features["gives_check"]),
                "capture_value": round(features["capture_value"], 6),
                "created_threats": created_threats(board, child.move),
                "refutation": refutation,
            }
        )
    rows.sort(key=lambda item: (-item["human_score"], -item["visits"], -item["value"], -item["prior"], item["uci"]))
    return rows[:limit] if limit is not None else rows


def select_deliberative_child(board: chess.Board, root: PuctNode) -> tuple[PuctNode, list[dict]]:
    candidates = deliberative_candidate_rows(board, root, limit=DELIBERATIVE_REFUTATION_CANDIDATE_LIMIT)
    if not candidates:
        raise ValueError("deliberative controller produced no candidates")
    best_uci = candidates[0]["uci"]
    child = root.children[best_uci]
    if child.move is None:
        raise ValueError("selected deliberative child has no move")
    return child, candidates[:DELIBERATIVE_RETURNED_CANDIDATE_LIMIT]


def run_mcts(
    board: chess.Board,
    network: PolicyValueNetwork | None = None,
    visits: int = 32,
    c_puct: float = 1.5,
    time_limit_ms: int | None = None,
) -> ZeroSearchResult:
    if not any(board.legal_moves):
        raise ValueError("no legal moves available")
    if visits <= 0:
        raise ValueError("visits must be positive")
    network = network or PolicyValueNetwork.load()
    root = PuctNode(board.copy(stack=False))
    root_value = expand(root, network)
    deadline = time.monotonic() + (time_limit_ms / 1000.0) if time_limit_ms else None
    completed_visits = 0
    for _ in range(visits):
        if deadline and time.monotonic() >= deadline and completed_visits:
            break
        node = root
        search_path = [node]
        while node.expanded and node.children:
            node = select_child(node, c_puct)
            search_path.append(node)
        value = expand(node, network)
        backup(search_path, value)
        completed_visits += 1
    if not root.children:
        raise ValueError("search produced no children")
    best, candidates = select_deliberative_child(board, root)
    assert best.move is not None
    explanation = explain_choice(board, best.move, candidates)
    comment = public_comment(explanation)
    return ZeroSearchResult(
        move=best.move,
        network_id=network.network_id,
        root_value=root_value,
        visits=completed_visits,
        nodes=count_nodes(root),
        candidates=candidates,
        explanation=explanation,
        comment=comment,
    )


def select_self_play_move(
    board: chess.Board,
    result: ZeroSearchResult,
    rng: random.Random,
    ply: int,
    exploration_plies: int = 10,
    temperature: float = 1.15,
) -> tuple[chess.Move, dict]:
    greedy_move = result.move
    if ply > exploration_plies or temperature <= 0 or len(result.candidates) <= 1:
        return greedy_move, {
            "selection": "greedy",
            "greedy_move": greedy_move.uci(),
            "temperature": temperature,
            "exploration_plies": exploration_plies,
        }
    legal = {move.uci(): move for move in board.legal_moves}
    candidates = [row for row in result.candidates if row.get("uci") in legal]
    if len(candidates) <= 1:
        return greedy_move, {
            "selection": "greedy",
            "greedy_move": greedy_move.uci(),
            "temperature": temperature,
            "exploration_plies": exploration_plies,
        }
    max_score = max(float(row.get("human_score", 0.0)) for row in candidates)
    weights = [math.exp((float(row.get("human_score", 0.0)) - max_score) / max(0.01, temperature)) for row in candidates]
    chosen = rng.choices(candidates, weights=weights, k=1)[0]
    move = legal[str(chosen["uci"])]
    return move, {
        "selection": "exploratory" if move != greedy_move else "greedy",
        "greedy_move": greedy_move.uci(),
        "temperature": temperature,
        "exploration_plies": exploration_plies,
        "candidate_count": len(candidates),
    }


def count_nodes(node: PuctNode) -> int:
    return 1 + sum(count_nodes(child) for child in node.children.values())


def candidate_rows(board: chess.Board, root: PuctNode, limit: int = 5) -> list[dict]:
    return deliberative_candidate_rows(board, root, limit=limit)


def best_reply_summary(board: chess.Board, move: chess.Move) -> dict:
    after = board.copy(stack=False)
    color = board.turn
    after.push(move)
    if after.is_game_over(claim_draw=True):
        return {"uci": "", "san": "terminal", "material_swing_cp": 0, "gives_check": False, "is_checkmate": False}
    best = None
    best_rank = None
    before = material_balance(after, color)
    for reply in after.legal_moves:
        reply_board = after.copy(stack=False)
        san = after.san(reply)
        gives_check = after.gives_check(reply)
        reply_board.push(reply)
        swing = before - material_balance(reply_board, color)
        is_checkmate = reply_board.is_checkmate()
        candidate = {
            "uci": reply.uci(),
            "san": san,
            "material_swing_cp": swing,
            "gives_check": gives_check,
            "is_capture": after.is_capture(reply),
            "is_checkmate": is_checkmate,
        }
        rank = (1 if is_checkmate else 0, swing, 1 if gives_check else 0, 1 if candidate["is_capture"] else 0)
        if best is None or rank > best_rank:
            best = candidate
            best_rank = rank
    return best or {"uci": "", "san": "", "material_swing_cp": 0, "gives_check": False, "is_capture": False, "is_checkmate": False}


def threat_map(board: chess.Board) -> dict:
    color = board.turn
    threats = []
    for square, piece in board.piece_map().items():
        if piece.color != color or piece.piece_type == chess.KING:
            continue
        attackers = list(board.attackers(not color, square))
        defenders = list(board.attackers(color, square))
        if attackers and PIECE_VALUES[piece.piece_type] >= 300:
            threats.append(
                {
                    "square": chess.square_name(square),
                    "piece": piece.symbol(),
                    "attackers": len(attackers),
                    "defenders": len(defenders),
                    "value_cp": PIECE_VALUES[piece.piece_type],
                }
            )
    threats.sort(key=lambda item: (-item["value_cp"], item["square"]))
    return {"loose_high_value_pieces": threats[:6]}


def explain_choice(board: chess.Board, move: chess.Move, candidates: list[dict]) -> dict:
    features = move_features(board, move)
    selected = next((candidate for candidate in candidates if candidate.get("uci") == move.uci()), {})
    reply = selected.get("refutation", {}).get("reply") if selected else best_reply_summary(board, move)
    plan = str(selected.get("plan_intent") or plan_intent_for_move(board, move, features))
    bucket_counts: dict[str, int] = {}
    for candidate in candidates:
        for tag in candidate.get("role_tags", []):
            bucket_counts[tag] = bucket_counts.get(tag, 0) + 1
    return {
        "reasoning_controller": DELIBERATIVE_CONTROLLER,
        "puct_role": "fast calculation support and prior/value verifier, not the defining Zero architecture",
        "selected_move": {"uci": move.uci(), "san": board.san(move)},
        "candidate_moves": candidates,
        "candidate_generation": {
            "policy": "human-style buckets: checks, captures, threats, development, king safety, center control, and pawn breaks",
            "bucket_counts": bucket_counts,
            "candidate_count": len(candidates),
        },
        "threat_map": threat_map(board),
        "plan_continuity": plan,
        "opponent_best_reply": reply,
        "calculation_verifier": {
            "method": "local legal-move scan for immediate material refutations plus PUCT visit/value support",
            "selected_refutation": selected.get("refutation") or refutation_check(board, move),
        },
        "tactical_blunder_check": {
            "moved_piece_risk": features["moved_piece_risk"],
            "status": "watch" if features["moved_piece_risk"] >= 0.5 or (reply or {}).get("material_swing_cp", 0) >= 500 else "ok",
        },
        "clock_policy": "LLM concept synthesis is post-game; move-time calculation uses bounded local deterministic checks.",
    }


def public_comment(explanation: dict) -> str:
    selected = explanation.get("selected_move", {})
    top = explanation.get("candidate_moves", [{}])[0]
    plan = explanation.get("plan_continuity", "chosen by PUCT search")
    status = explanation.get("tactical_blunder_check", {}).get("status", "ok")
    suffix = " after tactical check" if status == "ok" else " with material risk noted"
    move_label = selected.get("san") or selected.get("uci") or top.get("san") or top.get("uci") or "move"
    return f"{move_label}: {plan}{suffix}."


def choose_zero_move(board: chess.Board, visits: int = 32, time_limit_ms: int | None = None, c_puct: float = 1.5) -> ZeroSearchResult:
    return run_mcts(board, PolicyValueNetwork.load(), visits=visits, c_puct=c_puct, time_limit_ms=time_limit_ms)


def game_result_value(result: str, color: bool) -> float:
    if result == "1-0":
        return 1.0 if color == chess.WHITE else -1.0
    if result == "0-1":
        return 1.0 if color == chess.BLACK else -1.0
    if result == "1/2-1/2":
        return SELF_PLAY_DRAW_PENALTY
    return 0.0


def self_play_draw_outcome(board: chess.Board, color: bool) -> float:
    balance = material_balance(board, color)
    if balance >= 900:
        return SELF_PLAY_FAILED_CONVERSION_DRAW_PENALTY
    if balance >= 500:
        return (SELF_PLAY_DRAW_PENALTY + SELF_PLAY_FAILED_CONVERSION_DRAW_PENALTY) / 2
    if balance <= -900:
        return SELF_PLAY_DEFENSIVE_DRAW_PENALTY
    return SELF_PLAY_DRAW_PENALTY


def self_play_material_outcome(board: chess.Board, color: bool) -> float:
    balance = material_balance(board, color)
    if abs(balance) < 100:
        return 0.0
    return max(-1.0, min(1.0, balance / 900.0))


def record_has_risky_forcing_non_win(record: dict, outcome: float) -> bool:
    if outcome >= 0.0:
        return False
    try:
        board = chess.Board(record["fen"])
        move = chess.Move.from_uci(record["chosen_move"])
    except (KeyError, ValueError):
        return False
    if move not in board.legal_moves:
        return False
    is_forcing = board.gives_check(move) or board.is_capture(move) or bool(move.promotion) or bool(created_threats(board, move))
    if not is_forcing:
        return False
    return refutation_check(board, move).get("status") in {"watch", "unsafe"}


def record_has_failed_conversion_stall(record: dict, outcome: float) -> bool:
    if outcome >= 0.0:
        return False
    try:
        board = chess.Board(record["fen"])
        move = chess.Move.from_uci(record["chosen_move"])
    except (KeyError, ValueError):
        return False
    if move not in board.legal_moves:
        return False
    return move_features(board, move).get("conversion_stall", 0.0) > 0.0


def training_feature_scale(record: dict, feature_name: str, outcome: float) -> float:
    outcome_source = str(record.get("outcome_source", ""))
    if feature_name == "bias" and outcome < 0.0 and outcome_source == "terminal_draw_non_win_penalty":
        return 0.0
    if feature_name == "conversion_stall":
        return 2.2 if record_has_failed_conversion_stall(record, outcome) else 0.0
    if record_has_risky_forcing_non_win(record, outcome):
        if feature_name == "moved_piece_risk":
            return 2.0
        if feature_name in {"gives_check", "capture_value", "promotion_value", "material_delta"}:
            return 1.6
    return 1.0


def self_play_game(
    network: PolicyValueNetwork,
    visits: int = 16,
    max_plies: int = 160,
    seed: int | None = None,
    exploration_plies: int = 10,
    temperature: float = 1.15,
) -> dict:
    rng = random.Random(seed)
    board = chess.Board()
    records = []
    for ply in range(1, max_plies + 1):
        if board.is_game_over(claim_draw=True):
            break
        result = run_mcts(board, network, visits=visits, c_puct=1.5)
        visit_total = sum(max(0, item["visits"]) for item in result.candidates) or 1
        legal_mask = legal_move_mask(board)
        move, selection = select_self_play_move(
            board,
            result,
            rng,
            ply=ply,
            exploration_plies=exploration_plies,
            temperature=temperature,
        )
        records.append(
            {
                "schema": "zero-selfplay-position-v1",
                "source": "zero_self_play",
                "fen": board.fen(),
                "position_key": position_key(board.fen()),
                "side_to_move": "white" if board.turn == chess.WHITE else "black",
                "legal_move_indices": legal_mask["indices"],
                "visit_policy": {item["uci"]: item["visits"] / visit_total for item in result.candidates},
                "chosen_move": move.uci(),
                "chosen_move_index": move_to_index(move),
                "selection": selection["selection"],
                "greedy_move": selection["greedy_move"],
                "exploration_temperature": selection["temperature"],
                "network_id": network.network_id,
                "training_sources": {source: False for source in FORBIDDEN_TRAINING_SOURCES},
            }
        )
        board.push(move)
        if board.can_claim_draw() and rng.random() < 0.02:
            break
    outcome = board.outcome(claim_draw=True)
    result_text = board.result(claim_draw=True) if outcome else "*"
    outcome_source = "terminal_draw_non_win_penalty" if result_text == "1/2-1/2" else "terminal" if outcome else "self_material_adjudication"
    for record in records:
        color = chess.WHITE if record["side_to_move"] == "white" else chess.BLACK
        if result_text == "1/2-1/2":
            record["outcome"] = self_play_draw_outcome(board, color)
        elif result_text != "*":
            record["outcome"] = game_result_value(result_text, color)
        else:
            record["outcome"] = self_play_material_outcome(board, color)
        record["outcome_source"] = outcome_source
    return {
        "schema": "zero-selfplay-game-v1",
        "generated_at": now_stamp(),
        "network_id": network.network_id,
        "self_play_policy": {
            "selection": "bounded_seeded_exploration",
            "seed": seed,
            "exploration_plies": exploration_plies,
            "temperature": temperature,
            "training_sources": {source: False for source in FORBIDDEN_TRAINING_SOURCES},
        },
        "result": result_text,
        "outcome_source": outcome_source,
        "plies": len(records),
        "records": records,
    }


def dedupe_records(records: Iterable[dict]) -> list[dict]:
    merged: dict[str, dict] = {}
    for record in records:
        key = position_key(record.get("fen", ""))
        if not key:
            continue
        if key not in merged:
            item = dict(record)
            item["repeat_count"] = 1
            merged[key] = item
        else:
            merged[key]["repeat_count"] = int(merged[key].get("repeat_count", 1)) + 1
    return list(merged.values())


def append_replay_records(records: Iterable[dict], path: Path = REPLAY_BUFFER_PATH) -> dict:
    ensure_dirs()
    existing_records = []
    existing_index: dict[str, int] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                existing = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = position_key(existing.get("fen", ""))
            if not key:
                continue
            existing_index[key] = len(existing_records)
            existing_records.append(existing)
    unique = []
    skipped = 0
    updated = 0
    for record in dedupe_records(records):
        key = position_key(record.get("fen", ""))
        if key in existing_index:
            existing = existing_records[existing_index[key]]
            if should_update_replay_duplicate(existing, record):
                existing_records[existing_index[key]] = record
                updated += 1
            else:
                skipped += 1
            continue
        existing_index[key] = len(existing_records) + len(unique)
        unique.append(record)
    if updated:
        with path.open("w", encoding="utf-8") as handle:
            for record in [*existing_records, *unique]:
                handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
    else:
        with path.open("a", encoding="utf-8") as handle:
            for record in unique:
                handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
    return {"added": len(unique), "updated_duplicates": updated, "skipped_duplicates": skipped, "path": str(path)}


def write_self_play_game(game: dict) -> Path:
    ensure_dirs()
    path = SELFPLAY_DIR / f"zero-selfplay-{time.strftime('%Y%m%d-%H%M%S')}-{stable_id(game)}.json"
    game["replay_append"] = append_replay_records(game.get("records", []))
    path.write_text(json.dumps(game, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def load_replay_records(path: Path = REPLAY_BUFFER_PATH, max_records: int | None = None) -> list[dict]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        records.append(json.loads(line))
    return records[-max_records:] if max_records else records


def outcome_signal(record: dict) -> float:
    return abs(float(record.get("outcome", 0.0) or 0.0))


def outcome_sign(record: dict) -> int:
    outcome = float(record.get("outcome", 0.0) or 0.0)
    if outcome > 0.0:
        return 1
    if outcome < 0.0:
        return -1
    return 0


def has_forbidden_training_source(record: dict) -> bool:
    sources = record.get("training_sources", {})
    if not isinstance(sources, dict):
        return False
    return any(bool(sources.get(source)) for source in FORBIDDEN_TRAINING_SOURCES)


def should_update_replay_duplicate(existing: dict, incoming: dict) -> bool:
    if has_forbidden_training_source(incoming):
        return False
    existing_signal = outcome_signal(existing)
    incoming_signal = outcome_signal(incoming)
    if incoming_signal <= 0.0:
        return False
    if existing_signal == 0.0:
        return True
    return outcome_sign(existing) == outcome_sign(incoming) and incoming_signal > existing_signal + 1e-9


def replay_training_signal(record: dict) -> float:
    signal = outcome_signal(record)
    if str(record.get("outcome_source", "")).endswith("draw_non_win_penalty"):
        signal += 0.05
    if record_has_failed_conversion_stall(record, float(record.get("outcome", 0.0) or 0.0)):
        signal += 0.15
    if record_has_risky_forcing_non_win(record, float(record.get("outcome", 0.0) or 0.0)):
        signal += 0.10
    if record.get("selection") == "exploratory":
        signal += 0.02
    return signal


def select_training_records(records: list[dict], max_records: int | None = DEFAULT_TRAINING_REPLAY_LIMIT) -> list[dict]:
    if max_records is None or max_records <= 0 or len(records) <= max_records:
        return list(records)
    recent_count = max(1, int(max_records * TRAINING_REPLAY_RECENT_FRACTION))
    recent_start = max(0, len(records) - recent_count)
    selected_indices = set(range(recent_start, len(records)))
    remaining_slots = max_records - len(selected_indices)
    if remaining_slots > 0:
        older = list(enumerate(records[:recent_start]))
        older.sort(key=lambda item: (replay_training_signal(item[1]), item[0]), reverse=True)
        selected_indices.update(index for index, _ in older[:remaining_slots])
    return [record for index, record in enumerate(records) if index in selected_indices]


def _empty_lesson_counts() -> dict[str, int]:
    return {
        "risky_forcing_non_wins": 0,
        "hanging_piece_non_wins": 0,
        "draw_non_wins": 0,
        "failed_conversion_non_wins": 0,
        "exploration_examples": 0,
    }


def analyze_self_play_records_for_wisdom(paths: Iterable[Path]) -> dict:
    counts = _empty_lesson_counts()
    games = []
    total_records = 0
    outcome_signal = 0
    for path in paths:
        try:
            game = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        records = list(game.get("records", []))
        games.append(
            {
                "path": str(path),
                "result": game.get("result"),
                "outcome_source": game.get("outcome_source"),
                "plies": len(records),
            }
        )
        if game.get("result") == "1/2-1/2":
            counts["draw_non_wins"] += len(records)
        for record in records:
            total_records += 1
            outcome = float(record.get("outcome", 0.0) or 0.0)
            if abs(outcome) > 0:
                outcome_signal += 1
            if record.get("selection") == "exploratory":
                counts["exploration_examples"] += 1
            try:
                board = chess.Board(str(record["fen"]))
                move = chess.Move.from_uci(str(record["chosen_move"]))
            except (KeyError, ValueError):
                continue
            if move not in board.legal_moves:
                continue
            features = move_features(board, move)
            refutation = refutation_check(board, move)
            is_non_win = outcome <= 0.0
            is_forcing = bool(features["gives_check"]) or board.is_capture(move) or move.promotion
            if is_non_win and is_forcing and refutation.get("status") in {"watch", "unsafe"}:
                counts["risky_forcing_non_wins"] += 1
            if is_non_win and features["moved_piece_risk"] >= 0.5:
                counts["hanging_piece_non_wins"] += 1
            before = material_balance(board, board.turn)
            after = board.copy(stack=False)
            after.push(move)
            if is_non_win and before >= 900 and material_balance(after, not after.turn) >= 900:
                counts["failed_conversion_non_wins"] += 1
    return {
        "games": games,
        "total_records": total_records,
        "outcome_signal_positions": outcome_signal,
        "counts": counts,
    }


def build_wisdom_lessons(analysis: dict, promoted: bool) -> list[dict]:
    counts = dict(analysis.get("counts", {}))
    status = "active_promoted" if promoted else "candidate_hypothesis"
    lessons = []
    if counts.get("risky_forcing_non_wins", 0):
        lessons.append(
            {
                "title": "Verify forcing moves before trusting them",
                "evidence_count": counts["risky_forcing_non_wins"],
                "guidance": "Checks, captures, and threats need a recapture/refutation scan before their forcing bonus is trusted.",
                "status": status,
            }
        )
    if counts.get("hanging_piece_non_wins", 0):
        lessons.append(
            {
                "title": "Penalize moved pieces that become immediately capturable",
                "evidence_count": counts["hanging_piece_non_wins"],
                "guidance": "A move that puts the moved piece on an attacked square needs enough concrete compensation before it can be preferred.",
                "status": status,
            }
        )
    if counts.get("draw_non_wins", 0):
        lessons.append(
            {
                "title": "Treat repeated drawn self-play as failure to convert",
                "evidence_count": counts["draw_non_wins"],
                "guidance": "When self-play keeps drawing, reward irreversible progress, king safety, and conversion plans over repeatable checks or shuffling.",
                "status": status,
            }
        )
    if counts.get("failed_conversion_non_wins", 0):
        lessons.append(
            {
                "title": "Convert large advantages with low-risk progress",
                "evidence_count": counts["failed_conversion_non_wins"],
                "guidance": "When materially ahead, prefer trades, passed-pawn progress, and threat prevention over speculative tactics.",
                "status": status,
            }
        )
    if counts.get("exploration_examples", 0):
        lessons.append(
            {
                "title": "Exploration created alternative self-play evidence",
                "evidence_count": counts["exploration_examples"],
                "guidance": "Keep early self-play exploration bounded so training sees alternatives without importing outside chess labels.",
                "status": status,
            }
        )
    if not lessons:
        lessons.append(
            {
                "title": "No new tactical concept isolated",
                "evidence_count": 0,
                "guidance": "This cycle did not isolate a reusable human-readable rule; keep the network update behind promotion.",
                "status": "blocked",
            }
        )
    return lessons


def render_wisdom_delta_markdown(delta: dict) -> str:
    lines = [
        "# Human-Readable Zero Wisdom Delta",
        "",
        f"Generated: {delta['generated_at']}",
        f"Candidate: {delta.get('candidate', {}).get('network_id', '')}",
        f"Promotion: {'promoted' if delta.get('promotion', {}).get('promoted') else 'not promoted'}",
        f"Diagnosis: {delta.get('diagnosis', {}).get('status', '')}",
        "",
        "## Lessons",
    ]
    for lesson in delta.get("lessons", []):
        lines.extend(
            [
                f"- {lesson['title']} ({lesson['status']}, evidence {lesson['evidence_count']}): {lesson['guidance']}",
            ]
        )
    lines.extend(
        [
            "",
            "## Cycle Signal",
            f"- Self-play records scanned: {delta.get('analysis', {}).get('total_records', 0)}",
            f"- Outcome-signal positions: {delta.get('analysis', {}).get('outcome_signal_positions', 0)}",
            f"- Replay added: {delta.get('diagnosis', {}).get('replay_added', 0)}",
            f"- Replay updated: {delta.get('diagnosis', {}).get('replay_updated_duplicates', 0)}",
            f"- Duplicate trajectories: {delta.get('diagnosis', {}).get('duplicate_trajectories', False)}",
            "",
            "## Safety",
            "- Training source: Zero self-play only.",
            "- External engines, human games, opening books, and tablebases are evaluation/reference only.",
            "- This artifact stores concepts and aggregate evidence, not exact FEN-to-move answers.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_wisdom_delta(
    self_play_paths: Iterable[Path],
    diagnosis: dict,
    candidate: dict,
    promotion: dict,
    latest_json_path: Path = WISDOM_LATEST_JSON_PATH,
    latest_markdown_path: Path = WISDOM_LATEST_MARKDOWN_PATH,
    log_path: Path = WISDOM_LOG_PATH,
) -> dict:
    ensure_dirs()
    paths = [Path(path) for path in self_play_paths]
    analysis = analyze_self_play_records_for_wisdom(paths)
    promoted = bool(promotion.get("promoted"))
    delta = {
        "schema": "zero-human-wisdom-delta-v1",
        "generated_at": now_stamp(),
        "candidate": {
            "network_id": candidate.get("network_id", ""),
            "generation": candidate.get("generation", 0),
            "source_positions": candidate.get("source_positions", 0),
        },
        "promotion": promotion,
        "diagnosis": diagnosis,
        "analysis": analysis,
        "lessons": build_wisdom_lessons(analysis, promoted),
        "training_sources": {source: False for source in FORBIDDEN_TRAINING_SOURCES},
    }
    latest_json_path.parent.mkdir(parents=True, exist_ok=True)
    latest_json_path.write_text(json.dumps(delta, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    latest_markdown_path.write_text(render_wisdom_delta_markdown(delta), encoding="utf-8")
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(delta, sort_keys=True, separators=(",", ":")) + "\n")
    return {
        "latest_json": str(latest_json_path),
        "latest_markdown": str(latest_markdown_path),
        "log": str(log_path),
        "lesson_count": len(delta["lessons"]),
        "lessons": delta["lessons"],
        "training_sources": delta["training_sources"],
    }


def train_from_replay(
    base: PolicyValueNetwork | None = None,
    records: list[dict] | None = None,
    learning_rate: float = 0.05,
    epochs: int = 1,
    max_records: int | None = DEFAULT_TRAINING_REPLAY_LIMIT,
) -> PolicyValueNetwork:
    base = base or PolicyValueNetwork.load()
    records = records if records is not None else select_training_records(load_replay_records(), max_records=max_records)
    weights = dict(base.weights)
    used = 0
    for _ in range(max(1, epochs)):
        for record in records:
            try:
                board = chess.Board(record["fen"])
                move = chess.Move.from_uci(record["chosen_move"])
            except (KeyError, ValueError):
                continue
            if move not in board.legal_moves:
                continue
            outcome = float(record.get("outcome", 0.0))
            features = move_features(board, move)
            for name, value in features.items():
                scale = training_feature_scale(record, name, outcome)
                weights[name] = weights.get(name, 0.0) + learning_rate * outcome * value * scale
            for name, value in board_value_features(board).items():
                weights[name] = weights.get(name, 0.0) + learning_rate * outcome * value
            used += 1
    weights = clamp_learned_weights(weights)
    candidate = PolicyValueNetwork(
        network_id=f"zero-g{base.generation + 1}-{stable_id({'base': base.network_id, 'steps': base.training_steps, 'used': used, 'weights': weights})}",
        generation=base.generation + 1,
        created_at=now_stamp(),
        training_steps=base.training_steps + max(1, epochs),
        source_positions=base.source_positions + used,
        weights=weights,
    )
    candidate.save(CANDIDATE_NETWORK_PATH)
    return candidate


def play_network_match(
    challenger: PolicyValueNetwork,
    incumbent: PolicyValueNetwork,
    games: int = 2,
    visits: int = 8,
    max_plies: int = 120,
) -> dict:
    challenger_points = 0.0
    rows = []
    for game_index in range(games):
        board = chess.Board()
        challenger_white = game_index % 2 == 0
        for _ in range(max_plies):
            if board.is_game_over(claim_draw=True):
                break
            network = challenger if (board.turn == chess.WHITE) == challenger_white else incumbent
            result = run_mcts(board, network, visits=visits)
            board.push(result.move)
        result_text = board.result(claim_draw=True) if board.outcome(claim_draw=True) else "1/2-1/2"
        if result_text == "1/2-1/2":
            challenger_points += 0.5
        elif (result_text == "1-0" and challenger_white) or (result_text == "0-1" and not challenger_white):
            challenger_points += 1.0
        rows.append({"game": game_index + 1, "challenger_white": challenger_white, "result": result_text})
    score = challenger_points / max(1, games)
    return {
        "games": games,
        "challenger_points": challenger_points,
        "score": score,
        "rows": rows,
    }


def promotion_gate(
    current_path: Path = CURRENT_NETWORK_PATH,
    candidate_path: Path = CANDIDATE_NETWORK_PATH,
    games: int = 2,
    visits: int = 8,
    threshold: float = 0.55,
    force: bool = False,
) -> dict:
    ensure_dirs()
    incumbent = PolicyValueNetwork.load(current_path)
    if not candidate_path.exists():
        return {"promoted": False, "reason": "candidate network missing", "current": incumbent.network_id}
    challenger = PolicyValueNetwork.load(candidate_path)
    match = play_network_match(challenger, incumbent, games=games, visits=visits) if not force else {"games": 0, "score": 1.0, "rows": []}
    promoted = force or match["score"] >= threshold
    result = {
        "generated_at": now_stamp(),
        "promoted": promoted,
        "threshold": threshold,
        "current": incumbent.network_id,
        "candidate": challenger.network_id,
        "match": match,
    }
    if promoted:
        shutil.copyfile(candidate_path, current_path)
        result["reason"] = "candidate met promotion gate" if not force else "forced promotion"
    else:
        result["reason"] = "candidate did not meet promotion gate"
    with PROMOTION_LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n")
    return result


def find_exact_move_rules(text: str) -> list[dict]:
    if not text:
        return []
    violations = []
    for match in FEN_RE.finditer(text):
        start = max(0, match.start() - 80)
        end = min(len(text), match.end() + 140)
        window = text[start:end]
        lower = window.lower()
        if not any(marker in lower for marker in ("best", "play", "preferred", "answer", "move")):
            continue
        move_match = UCI_RE.search(window)
        if move_match:
            violations.append({"fen": match.group(0), "move": move_match.group(0), "context": window.strip()[:220]})
    return violations


def anti_memorization_status() -> dict:
    paths = [ZERO_MEMORY_PATH]
    if ZERO_KNOWLEDGEBASE_DIR.exists():
        paths.extend(path for path in ZERO_KNOWLEDGEBASE_DIR.rglob("*") if path.is_file() and path.suffix.lower() in {".md", ".txt", ".json"})
    violations = []
    for path in paths:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for violation in find_exact_move_rules(text):
            violations.append({"path": str(path), **violation})
    return {
        "ok": not violations,
        "violations": violations[:20],
        "checked_files": len([path for path in paths if path.exists()]),
        "policy": "Exact FEN/opening move-answer rules are rejected from Zero memory and knowledgebase.",
    }


def benchmark_ladder(config_path: Path = ENGINE_CONFIG_PATH) -> list[dict]:
    rows = [
        {"name": "Random legal baseline", "role": "baseline", "training_allowed": False, "available": True},
        {"name": "Codex-chess-learner", "role": "prompt learner", "training_allowed": False, "available": True},
        {"name": "Codex-chess-zero deliberative", "role": "human-reasoning first-principles candidate", "training_allowed": True, "available": True},
        {"name": "Weak Stockfish depth/nodes", "role": "evaluation only", "training_allowed": False, "available": False},
        {"name": "Full Stockfish 18", "role": "evaluation only", "training_allowed": False, "available": False},
        {"name": "Latest Stockfish dev", "role": "optional evaluation only", "training_allowed": False, "available": False},
        {"name": "Lc0 installed network", "role": "evaluation only", "training_allowed": False, "available": False},
        {"name": "Maia reference", "role": "human-style reference only", "training_allowed": False, "available": False},
    ]
    if config_path.exists():
        try:
            engines = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            engines = []
        for engine in engines if isinstance(engines, list) else []:
            name = str(engine.get("name", ""))
            path = Path(str(engine.get("path", "")))
            available = path.exists()
            lower = name.lower()
            if "stockfish" in lower:
                for row in rows:
                    if row["name"] in {"Weak Stockfish depth/nodes", "Full Stockfish 18"}:
                        row.update({"available": available, "path": str(path), "version": str(engine.get("version", ""))})
            if "leela" in lower or "lc0" in lower:
                for row in rows:
                    if row["name"] == "Lc0 installed network":
                        row.update({"available": available, "path": str(path), "version": str(engine.get("version", ""))})
    return rows


def read_latest_promotion() -> dict:
    if not PROMOTION_LOG_PATH.exists():
        return {}
    lines = [line for line in PROMOTION_LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]
    if not lines:
        return {}
    try:
        return json.loads(lines[-1])
    except json.JSONDecodeError:
        return {"error": "latest promotion log row is invalid JSON"}


def concept_summary(max_lines: int = 12) -> list[str]:
    path = ZERO_KNOWLEDGEBASE_DIR / "strategy-lessons.md"
    if not path.exists():
        return []
    lines = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("- ") and "FEN" not in line:
            lines.append(line[2:])
        if len(lines) >= max_lines:
            break
    return lines


def research_summary(write_summary: bool = True) -> dict:
    ensure_dirs()
    current = PolicyValueNetwork.load()
    candidate = PolicyValueNetwork.load(CANDIDATE_NETWORK_PATH) if CANDIDATE_NETWORK_PATH.exists() else None
    summary = {
        "updated_at": now_stamp(),
        "root": str(ENGINE_DIR),
        "current_network": current.to_dict(),
        "candidate_network": candidate.to_dict() if candidate else None,
        "reasoning_controller": {
            "name": DELIBERATIVE_CONTROLLER,
            "policy": "candidate generation and plan/refutation reasoning choose moves; PUCT is bounded calculation support",
            "llm_role": "post-game concept discovery, failure clustering, skill/tool synthesis, and explanations; no per-node search calls",
        },
        "artifacts": {
            "research_dir": str(RESEARCH_DIR),
            "current_network": str(CURRENT_NETWORK_PATH),
            "candidate_network": str(CANDIDATE_NETWORK_PATH),
            "replay_buffer": str(REPLAY_BUFFER_PATH),
            "promotion_log": str(PROMOTION_LOG_PATH),
            "latest_wisdom_delta": str(WISDOM_LATEST_MARKDOWN_PATH),
        },
        "counts": {
            "selfplay_games": len(list(SELFPLAY_DIR.glob("zero-selfplay-*.json"))) if SELFPLAY_DIR.exists() else 0,
            "replay_positions": file_line_count(REPLAY_BUFFER_PATH),
            "promotions": file_line_count(PROMOTION_LOG_PATH),
            "wisdom_deltas": file_line_count(WISDOM_LOG_PATH),
        },
        "latest_promotion": read_latest_promotion(),
        "anti_memorization": anti_memorization_status(),
        "benchmark_ladder": benchmark_ladder(),
        "concepts": concept_summary(),
        "feasibility_gate": (
            "Stockfish parity is not claimed until Zero has sustained self-play volume, trained fast evaluators, "
            "deliberative reasoning gains across promoted generations, and measured Elo growth against the benchmark ladder."
        ),
    }
    if write_summary:
        SUMMARY_PATH.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return summary


def run_self_play(
    games: int,
    visits: int,
    max_plies: int,
    seed: int | None = None,
    exploration_plies: int = 10,
    temperature: float = 1.15,
) -> list[Path]:
    network = PolicyValueNetwork.load()
    paths = []
    for index in range(games):
        game = self_play_game(
            network,
            visits=visits,
            max_plies=max_plies,
            seed=None if seed is None else seed + index,
            exploration_plies=exploration_plies,
            temperature=temperature,
        )
        paths.append(write_self_play_game(game))
    research_summary()
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Codex-chess-zero first-principles research utilities.")
    sub = parser.add_subparsers(dest="command", required=True)

    selfplay = sub.add_parser("self-play")
    selfplay.add_argument("--games", type=int, default=1)
    selfplay.add_argument("--visits", type=int, default=8)
    selfplay.add_argument("--max-plies", type=int, default=80)
    selfplay.add_argument("--seed", type=int)

    train = sub.add_parser("train")
    train.add_argument("--epochs", type=int, default=1)
    train.add_argument("--learning-rate", type=float, default=0.05)

    promote = sub.add_parser("promote")
    promote.add_argument("--games", type=int, default=2)
    promote.add_argument("--visits", type=int, default=8)
    promote.add_argument("--threshold", type=float, default=0.55)
    promote.add_argument("--force", action="store_true")

    sub.add_parser("summary")
    args = parser.parse_args()

    if args.command == "self-play":
        paths = run_self_play(args.games, args.visits, args.max_plies, args.seed)
        print(json.dumps({"ok": True, "paths": [str(path) for path in paths]}, indent=2))
    elif args.command == "train":
        network = train_from_replay(learning_rate=args.learning_rate, epochs=args.epochs)
        research_summary()
        print(json.dumps({"ok": True, "candidate": network.to_dict()}, indent=2))
    elif args.command == "promote":
        result = promotion_gate(games=args.games, visits=args.visits, threshold=args.threshold, force=args.force)
        research_summary()
        print(json.dumps({"ok": True, "promotion": result}, indent=2))
    elif args.command == "summary":
        print(json.dumps(research_summary(), indent=2))


if __name__ == "__main__":
    main()
