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
REPLAY_SCHEMA_VERSION = 2
MAX_TRAINING_RECORDS_PER_OPENING_SIGNATURE = 64
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
    "value_material": 1.0,
    "value_mobility": 0.08,
    "value_king_safety": 0.10,
}
WEIGHT_BOUNDS = {
    "bias": (-2.0, 2.0),
    "capture_value": (0.15, 4.0),
    "material_delta": (0.35, 4.0),
    "gives_check": (-3.0, 4.0),
    "promotion_value": (0.5, 4.0),
    "center_to": (0.0, 4.0),
    "development": (0.0, 4.0),
    "castle": (0.0, 4.0),
    "conversion_stall": (-6.0, 0.5),
    "moved_piece_risk": (-8.0, 0.5),
    "value_material": (0.25, 12.0),
    "value_mobility": (0.0, 6.0),
    "value_king_safety": (-8.0, 8.0),
}
FOUNDATIONAL_PROGRESS_FEATURES = {
    "capture_value",
    "material_delta",
    "promotion_value",
    "center_to",
    "development",
    "castle",
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
DELIBERATIVE_LOCAL_SEARCH_PLIES = 4
DELIBERATIVE_LOCAL_SEARCH_WIDTH = 8
DELIBERATIVE_QUIESCENCE_PLIES = 0
DELIBERATIVE_QUIESCENCE_WIDTH = 4
ZERO_ALPHA_BETA_DEFAULT_TIME_LIMIT_MS = 300
ZERO_ALPHA_BETA_MATE_SCORE = 100000.0
ZERO_ALPHA_BETA_INF = 200000.0
ZERO_ALPHA_BETA_MIN_DEPTH = 2
ZERO_ALPHA_BETA_MAX_DEPTH = 5
ZERO_ALPHA_BETA_SEARCH_WIDTH = 28
ZERO_ALPHA_BETA_QUIESCENCE_WIDTH = 5
ZERO_ALPHA_BETA_QUIESCENCE_PLIES = 2
ZERO_FAST_CENTER = {chess.C4, chess.D4, chess.E4, chess.F4, chess.C5, chess.D5, chess.E5, chess.F5}
ZERO_FAST_EXTENDED_CENTER = {
    chess.C3,
    chess.D3,
    chess.E3,
    chess.F3,
    chess.C4,
    chess.D4,
    chess.E4,
    chess.F4,
    chess.C5,
    chess.D5,
    chess.E5,
    chess.F5,
    chess.C6,
    chess.D6,
    chess.E6,
    chess.F6,
}
NOVELTY_REPLAY_LOOKBACK = 8192
SELF_PLAY_NOVELTY_WEIGHT = 0.55


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


def repetition_bucket(board: chess.Board) -> str:
    if board.can_claim_threefold_repetition():
        return "threefold_claimable"
    if board.is_repetition(2):
        return "repeated"
    return "none"


def position_key(fen: str, repetition: str = "none") -> str:
    fields = str(fen).split()
    if len(fields) < 5:
        return " ".join(fields)
    return " ".join([*fields[:5], f"rep:{repetition or 'none'}"])


def replay_identity(board: chess.Board) -> dict:
    return {
        "schema_version": REPLAY_SCHEMA_VERSION,
        "fen_key": position_key(board.fen(), repetition_bucket(board)),
        "side_to_move": "white" if board.turn == chess.WHITE else "black",
        "castling": board.castling_xfen(),
        "en_passant": chess.square_name(board.ep_square) if board.ep_square is not None else "-",
        "halfmove_clock": board.halfmove_clock,
        "repetition_bucket": repetition_bucket(board),
    }


def replay_record_key(record: dict) -> str:
    identity = record.get("state_identity")
    if isinstance(identity, dict) and identity.get("fen_key"):
        return str(identity["fen_key"])
    return position_key(str(record.get("fen", "")), str(record.get("repetition_bucket", "none")))


def stable_id(payload: object, length: int = 12) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:length]


def clamp_learned_weights(weights: dict[str, float]) -> dict[str, float]:
    clamped = {name: float(weights.get(name, default)) for name, default in DEFAULT_WEIGHTS.items()}
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
    training_metrics: dict[str, float] = field(default_factory=dict)

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
            training_metrics=dict(data.get("training_metrics", {})) if isinstance(data.get("training_metrics"), dict) else {},
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
            "training_metrics": self.training_metrics,
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
    root_visit_counts: dict[str, int]
    root_visit_policy: dict[str, float]
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
            "root_visit_counts": self.root_visit_counts,
            "root_visit_policy": self.root_visit_policy,
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


def apply_root_noise(root: PuctNode, rng: random.Random, alpha: float = 0.3, fraction: float = 0.25) -> None:
    children = list(root.children.values())
    if len(children) <= 1 or fraction <= 0.0:
        return
    alpha = max(0.01, float(alpha))
    noise = [rng.gammavariate(alpha, 1.0) for _ in children]
    total = sum(noise) or 1.0
    for child, raw in zip(children, noise):
        child.prior = ((1.0 - fraction) * child.prior) + (fraction * (raw / total))


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


def square_zone(square: chess.Square, color: bool) -> str:
    file_index = chess.square_file(square)
    rank_index = chess.square_rank(square)
    file_zone = "center" if file_index in {2, 3, 4, 5} else "edge"
    if file_index <= 2:
        wing = "queenside"
    elif file_index >= 5:
        wing = "kingside"
    else:
        wing = "center"
    relative_rank = rank_index if color == chess.WHITE else 7 - rank_index
    if relative_rank <= 1:
        rank_zone = "home"
    elif relative_rank <= 3:
        rank_zone = "near"
    elif relative_rank <= 5:
        rank_zone = "far"
    else:
        rank_zone = "back"
    return f"{wing}:{file_zone}:{rank_zone}"


def material_delta_bucket(value: float) -> str:
    if value >= 0.5:
        return "wins_material"
    if value > 0.05:
        return "small_gain"
    if value <= -0.5:
        return "sacrifices_material"
    if value < -0.05:
        return "small_loss"
    return "material_hold"


def human_novelty_key(board: chess.Board, move: chess.Move) -> str:
    mover = board.piece_at(move.from_square)
    features = move_features(board, move)
    role_tags = sorted(move_role_tags(board, move, features))
    refutation_status = str(refutation_check(board, move).get("status", "ok"))
    payload = {
        "piece": mover.symbol().lower() if mover else "unknown",
        "roles": role_tags,
        "plan": plan_intent_for_move(board, move, features),
        "from_zone": square_zone(move.from_square, board.turn),
        "to_zone": square_zone(move.to_square, board.turn),
        "material": material_delta_bucket(features["material_delta"]),
        "risk": "risky" if refutation_status in {"watch", "unsafe"} or features["moved_piece_risk"] >= 0.5 else "safe",
    }
    return stable_id(payload, length=16)


def build_novelty_archive(records: Iterable[dict]) -> dict[str, int]:
    archive: dict[str, int] = {}
    for record in records:
        novelty = record.get("novelty", {})
        key = str(novelty.get("key") or record.get("novelty_key") or "")
        if not key:
            try:
                board = chess.Board(str(record["fen"]))
                move = chess.Move.from_uci(str(record["chosen_move"]))
            except (KeyError, ValueError):
                continue
            if move not in board.legal_moves:
                continue
            key = human_novelty_key(board, move)
        archive[key] = archive.get(key, 0) + int(record.get("repeat_count", 1) or 1)
    return archive


def novelty_profile(
    board: chess.Board,
    move: chess.Move,
    archive_counts: dict[str, int] | None = None,
    game_counts: dict[str, int] | None = None,
) -> dict:
    archive_counts = archive_counts or {}
    game_counts = game_counts or {}
    features = move_features(board, move)
    refutation = refutation_check(board, move)
    key = human_novelty_key(board, move)
    archive_count = int(archive_counts.get(key, 0) or 0)
    game_count = int(game_counts.get(key, 0) or 0)
    role_tags = sorted(move_role_tags(board, move, features))
    tags = []
    if archive_count == 0:
        tags.append("archive_new")
    elif archive_count <= 2:
        tags.append("archive_rare")
    else:
        tags.append("archive_familiar")
    tags.append("line_new" if game_count == 0 else "line_repeated")
    refutation_status = str(refutation.get("status", "ok"))
    tags.append("safe_refutation" if refutation_status in {"ok", "terminal"} else "needs_refutation")
    if "creates_threat" in role_tags:
        tags.append("creates_new_problem")
    if "pawn_break" in role_tags:
        tags.append("structure_change")
    if features["conversion_stall"] <= 0.0:
        tags.append("conversion_progress")
    score = 0.0
    score += 0.45 if archive_count == 0 else 0.22 if archive_count <= 2 else 0.0
    score += 0.25 if game_count == 0 else -0.10
    score += 0.12 if "creates_threat" in role_tags or "pawn_break" in role_tags else 0.0
    score += 0.08 if features["conversion_stall"] <= 0.0 else -0.12
    score -= 0.25 if refutation_status == "unsafe" else 0.10 if refutation_status == "watch" else 0.0
    score -= 0.15 if features["moved_piece_risk"] >= 0.5 else 0.0
    return {
        "schema": "zero-human-novelty-v1",
        "key": key,
        "score": round(max(-0.5, min(1.0, score)), 6),
        "tags": tags,
        "archive_count": archive_count,
        "game_count": game_count,
        "role_tags": role_tags,
        "plan_intent": plan_intent_for_move(board, move, features),
        "refutation_status": refutation_status,
        "training_sources": {source: False for source in FORBIDDEN_TRAINING_SOURCES},
    }


def deliberative_score(
    board: chess.Board,
    child: PuctNode,
    root_visits: int,
    features: dict[str, float],
    refutation: dict,
    local_tactical_score: float = 0.0,
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
        + 0.35 * features["castle"]
        + 0.08 * features["development"]
        + 0.07 * features["center_to"]
        - 0.24 * features["conversion_stall"]
        - 0.62 * features["moved_piece_risk"]
        - 0.72 * refutation_penalty
        - 0.33 * forcing_reply_penalty
        - refutation_status_penalty
        + 1.25 * local_tactical_score
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
        if features["moved_piece_risk"] >= 0.5:
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


def deliberative_candidate_rows(
    board: chess.Board,
    root: PuctNode,
    limit: int | None = 8,
    local_search_plies: int = DELIBERATIVE_LOCAL_SEARCH_PLIES,
) -> list[dict]:
    rows = []
    root_visits = max(1, root.visit_count)
    children = [child for child in root.children.values() if child.move is not None]
    tactical_cache: dict[tuple[str, bool, int], float] = {}
    if limit is not None and len(children) > limit:
        selected = {child.move.uci(): child for child in select_deliberative_candidate_children(board, root, children, limit) if child.move}
        tactical_ranked = sorted(
            children,
            key=lambda child: (
                local_tactical_score(board, child.move, plies=local_search_plies, cache=tactical_cache) if child.move else -1_000_000.0,
                child.visit_count,
                child.move.uci() if child.move else "",
            ),
            reverse=True,
        )
        for child in tactical_ranked[: max(1, DELIBERATIVE_SAFE_CANDIDATE_LIMIT)]:
            assert child.move is not None
            selected[child.move.uci()] = child
        children = sorted(
            selected.values(),
            key=lambda child: (
                local_tactical_score(board, child.move, plies=local_search_plies, cache=tactical_cache) if child.move else -1_000_000.0,
                child.visit_count,
                child.move.uci() if child.move else "",
            ),
            reverse=True,
        )[:limit]
    for child in children:
        assert child.move is not None
        features = move_features(board, child.move)
        refutation = refutation_check(board, child.move)
        local_score_cp = local_tactical_score(board, child.move, plies=local_search_plies, cache=tactical_cache)
        local_score = normalize_local_tactical_score(local_score_cp)
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
                "human_score": deliberative_score(board, child, root_visits, features, refutation, local_score),
                "local_tactical_score": round(local_score, 6),
                "local_tactical_score_cp": int(round(local_score_cp)),
                "risk": round(features["moved_piece_risk"], 6),
                "gives_check": bool(features["gives_check"]),
                "capture_value": round(features["capture_value"], 6),
                "created_threats": created_threats(board, child.move),
                "refutation": refutation,
            }
        )
    rows.sort(key=lambda item: (-item["human_score"], -item["visits"], -item["value"], -item["prior"], item["uci"]))
    return rows[:limit] if limit is not None else rows


def select_deliberative_child(
    board: chess.Board,
    root: PuctNode,
    local_search_plies: int = DELIBERATIVE_LOCAL_SEARCH_PLIES,
) -> tuple[PuctNode, list[dict]]:
    candidates = deliberative_candidate_rows(
        board,
        root,
        limit=DELIBERATIVE_REFUTATION_CANDIDATE_LIMIT,
        local_search_plies=local_search_plies,
    )
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
    root_noise: bool = False,
    root_noise_alpha: float = 0.3,
    root_noise_fraction: float = 0.25,
    rng: random.Random | None = None,
) -> ZeroSearchResult:
    if not any(board.legal_moves):
        raise ValueError("no legal moves available")
    if visits <= 0:
        raise ValueError("visits must be positive")
    network = network or PolicyValueNetwork.load()
    root = PuctNode(board.copy(stack=False))
    root_value = expand(root, network)
    if root_noise:
        apply_root_noise(root, rng or random.Random(), root_noise_alpha, root_noise_fraction)
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
    root_visit_counts = {uci: child.visit_count for uci, child in root.children.items()}
    visit_total = sum(root_visit_counts.values()) or 1
    root_visit_policy = {uci: count / visit_total for uci, count in root_visit_counts.items()}
    candidate_local_plies = 1 if time_limit_ms is None else 2
    puct_best, candidates = select_deliberative_child(
        board,
        root,
        local_search_plies=candidate_local_plies,
    )
    assert puct_best.move is not None
    alpha_beta_target_depth = alpha_beta_depth_for_visits(completed_visits or visits, time_limit_ms)
    adaptive_depth = False
    if time_limit_ms is None:
        adapted_depth = adaptive_alpha_beta_depth(board, alpha_beta_target_depth)
        adaptive_depth = adapted_depth > alpha_beta_target_depth
        alpha_beta_target_depth = adapted_depth
    alpha_beta = alpha_beta_root_search(
        board,
        max_depth=alpha_beta_target_depth,
        time_limit_ms=time_limit_ms,
    )
    alpha_beta["adaptive_depth"] = adaptive_depth
    raw_alpha_move = alpha_beta["move"] if isinstance(alpha_beta.get("move"), chess.Move) else puct_best.move
    safety = (
        {
            "move": raw_alpha_move,
            "overridden": False,
            "proposed": raw_alpha_move.uci(),
            "selected": raw_alpha_move.uci(),
            "proposed_score_cp": alpha_beta.get("score_cp"),
            "selected_score_cp": alpha_beta.get("score_cp"),
            "threshold_cp": 0,
            "training_sources": {source: False for source in FORBIDDEN_TRAINING_SOURCES},
        }
        if alpha_beta.get("fast_safety_root")
        else material_safety_override(board, raw_alpha_move)
    )
    if safety.get("overridden"):
        alpha_beta["raw_move"] = raw_alpha_move
        alpha_beta["move"] = safety["move"]
        alpha_beta["safety_override"] = safety
    else:
        alpha_beta["safety_override"] = safety
    best_move = alpha_beta["move"] if isinstance(alpha_beta.get("move"), chess.Move) else puct_best.move
    candidates = merge_alpha_beta_candidate(board, candidates, alpha_beta)
    explanation = explain_choice(board, best_move, candidates)
    explanation["puct_role"] = "policy/value telemetry and fast calculation support; deterministic alpha-beta is the final live arbiter"
    explanation["calculation_verifier"]["method"] = (
        "deterministic first-principles alpha-beta final arbiter with local legal-move scan, "
        "bounded tactical search, and PUCT visit/value support"
    )
    explanation["calculation_verifier"]["alpha_beta"] = {
        "selected": True,
        "score_cp": alpha_beta.get("score_cp"),
        "depth": alpha_beta.get("depth"),
        "target_depth": alpha_beta.get("target_depth"),
        "nodes": alpha_beta.get("nodes"),
        "time_limit_ms": alpha_beta.get("time_limit_ms"),
        "time_up": alpha_beta.get("time_up"),
        "adaptive_depth": alpha_beta.get("adaptive_depth", False),
        "fast_safety_root": alpha_beta.get("fast_safety_root", False),
        "safety_override": {key: value for key, value in (alpha_beta.get("safety_override") or {}).items() if key != "move"},
        "training_sources": alpha_beta.get("training_sources"),
    }
    comment = public_comment(explanation)
    return ZeroSearchResult(
        move=best_move,
        network_id=network.network_id,
        root_value=root_value,
        visits=completed_visits,
        nodes=count_nodes(root) + int(alpha_beta.get("nodes", 0) or 0),
        candidates=candidates,
        root_visit_counts=root_visit_counts,
        root_visit_policy=root_visit_policy,
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
    novelty_archive: dict[str, int] | None = None,
    game_novelty_counts: dict[str, int] | None = None,
) -> tuple[chess.Move, dict]:
    greedy_move = result.move
    greedy_novelty = novelty_profile(board, greedy_move, novelty_archive, game_novelty_counts)
    if ply > exploration_plies or temperature <= 0 or len(result.candidates) <= 1:
        return greedy_move, {
            "selection": "greedy",
            "greedy_move": greedy_move.uci(),
            "temperature": temperature,
            "exploration_plies": exploration_plies,
            "novelty": greedy_novelty,
            "novelty_policy": "human_readable_novelty_pressure",
        }
    legal = {move.uci(): move for move in board.legal_moves}
    candidates = [row for row in result.candidates if row.get("uci") in legal]
    if len(candidates) <= 1:
        return greedy_move, {
            "selection": "greedy",
            "greedy_move": greedy_move.uci(),
            "temperature": temperature,
            "exploration_plies": exploration_plies,
            "novelty": greedy_novelty,
            "novelty_policy": "human_readable_novelty_pressure",
        }
    scored = []
    for row in candidates:
        move = legal[str(row["uci"])]
        novelty = novelty_profile(board, move, novelty_archive, game_novelty_counts)
        score = float(row.get("human_score", 0.0)) + (SELF_PLAY_NOVELTY_WEIGHT * float(novelty["score"]))
        scored.append((row, novelty, score))
    max_score = max(score for _, _, score in scored)
    weights = [math.exp((score - max_score) / max(0.01, temperature)) for _, _, score in scored]
    chosen, chosen_novelty, chosen_score = rng.choices(scored, weights=weights, k=1)[0]
    move = legal[str(chosen["uci"])]
    return move, {
        "selection": "exploratory" if move != greedy_move else "greedy",
        "greedy_move": greedy_move.uci(),
        "temperature": temperature,
        "exploration_plies": exploration_plies,
        "candidate_count": len(candidates),
        "novelty": chosen_novelty,
        "novelty_adjusted_score": round(chosen_score, 6),
        "novelty_policy": "human_readable_novelty_pressure",
    }


def count_nodes(node: PuctNode) -> int:
    return 1 + sum(count_nodes(child) for child in node.children.values())


def candidate_rows(
    board: chess.Board,
    root: PuctNode,
    limit: int = 5,
    local_search_plies: int = DELIBERATIVE_LOCAL_SEARCH_PLIES,
) -> list[dict]:
    return deliberative_candidate_rows(board, root, limit=limit, local_search_plies=local_search_plies)


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


def static_zero_evaluation(board: chess.Board, color: bool) -> float:
    outcome = board.outcome(claim_draw=True)
    if outcome is not None:
        if outcome.winner is None:
            return 0.0
        return 100000.0 if outcome.winner == color else -100000.0
    score = float(material_balance(board, color))
    center = {chess.C4, chess.D4, chess.E4, chess.F4, chess.C5, chess.D5, chess.E5, chess.F5}
    own_attack_squares = 0
    enemy_attack_squares = 0
    loose_material = 0.0
    for square, piece in board.piece_map().items():
        sign = 1.0 if piece.color == color else -1.0
        rank = chess.square_rank(square)
        file_index = chess.square_file(square)
        relative_rank = rank if piece.color == chess.WHITE else 7 - rank
        if square in center:
            score += sign * 14.0
        if piece.piece_type == chess.PAWN:
            score += sign * relative_rank * 5.0
            if file_index in {3, 4}:
                score += sign * 4.0
        elif piece.piece_type in {chess.KNIGHT, chess.BISHOP}:
            if relative_rank >= 2:
                score += sign * 12.0
        elif piece.piece_type == chess.ROOK:
            if relative_rank >= 3:
                score += sign * 6.0
        attacks = len(board.attacks(square))
        if piece.color == color:
            own_attack_squares += attacks
        else:
            enemy_attack_squares += attacks
        if piece.piece_type != chess.KING:
            attackers = len(board.attackers(not piece.color, square))
            defenders = len(board.attackers(piece.color, square))
            if attackers:
                value = float(PIECE_VALUES.get(piece.piece_type, 0))
                if defenders == 0:
                    loose_material += sign * -0.48 * value
                elif attackers > defenders:
                    loose_material += sign * -0.24 * value
                elif value >= 500:
                    loose_material += sign * -0.08 * value
    score += (own_attack_squares - enemy_attack_squares) * 1.5
    score += loose_material
    own_king = board.king(color)
    enemy_king = board.king(not color)
    if own_king is not None:
        score -= 42.0 * len(board.attackers(not color, own_king))
        score += king_development_safety(board, color, own_king)
    if enemy_king is not None:
        score += 42.0 * len(board.attackers(color, enemy_king))
        score -= king_development_safety(board, not color, enemy_king)
    score += opening_discipline_score(board, color)
    score -= opening_discipline_score(board, not color)
    return score


def minor_development_count(board: chess.Board, color: bool) -> int:
    back_rank = 0 if color == chess.WHITE else 7
    count = 0
    for piece_type in (chess.KNIGHT, chess.BISHOP):
        for square in board.pieces(piece_type, color):
            if chess.square_rank(square) != back_rank:
                count += 1
    return count


def is_castled_shape(board: chess.Board, color: bool, king_square: chess.Square) -> bool:
    if color == chess.WHITE:
        return (
            (king_square == chess.G1 and board.piece_at(chess.F1) == chess.Piece(chess.ROOK, color))
            or (king_square == chess.C1 and board.piece_at(chess.D1) == chess.Piece(chess.ROOK, color))
        )
    return (
        (king_square == chess.G8 and board.piece_at(chess.F8) == chess.Piece(chess.ROOK, color))
        or (king_square == chess.C8 and board.piece_at(chess.D8) == chess.Piece(chess.ROOK, color))
    )


def opening_discipline_score(board: chess.Board, color: bool) -> float:
    if board.fullmove_number > 14:
        return 0.0
    score = 0.0
    developed = minor_development_count(board, color)
    home_queen_square = chess.D1 if color == chess.WHITE else chess.D8
    queen_squares = list(board.pieces(chess.QUEEN, color))
    if queen_squares and queen_squares[0] != home_queen_square and developed < 2:
        score -= 320.0
    king_square = board.king(color)
    home_king_square = chess.E1 if color == chess.WHITE else chess.E8
    if king_square is not None and king_square != home_king_square and not is_castled_shape(board, color, king_square):
        score -= 350.0
    if developed >= 2:
        score += 85.0
    if developed >= 3:
        score += 70.0
    return score


def king_development_safety(board: chess.Board, color: bool, king_square: chess.Square) -> float:
    home_rank = 0 if color == chess.WHITE else 7
    central_home = {chess.D1, chess.E1} if color == chess.WHITE else {chess.D8, chess.E8}
    score = 0.0
    if is_castled_shape(board, color, king_square):
        score += 180.0
    if king_square in central_home:
        score -= 180.0
        center_pawns = [
            board.piece_at(chess.square(file_index, home_rank + (1 if color == chess.WHITE else -1)))
            for file_index in (3, 4)
        ]
        missing_center_pawns = sum(1 for pawn in center_pawns if pawn is None or pawn.color != color or pawn.piece_type != chess.PAWN)
        score -= 80.0 * missing_center_pawns
        enemy_queen = board.pieces(chess.QUEEN, not color)
        enemy_bishops = board.pieces(chess.BISHOP, not color)
        enemy_rooks = board.pieces(chess.ROOK, not color)
        enemy_activity = 0
        for square in [*enemy_queen, *enemy_bishops, *enemy_rooks]:
            if chess.square_rank(square) not in {0, 7}:
                enemy_activity += 1
        score -= 55.0 * enemy_activity
    return score


def fast_piece_activity_bonus(piece_type: chess.PieceType, square: chess.Square, color: bool) -> float:
    file_index = chess.square_file(square)
    rank = chess.square_rank(square)
    relative_rank = rank if color == chess.WHITE else 7 - rank
    center_file = 3.5 - abs(file_index - 3.5)
    center_rank = 3.5 - abs(relative_rank - 3.5)
    edge_penalty = (1 if file_index in {0, 7} else 0) + (1 if rank in {0, 7} else 0)
    if piece_type == chess.PAWN:
        bonus = relative_rank * 7.0
        if file_index in {3, 4}:
            bonus += 12.0
        if square in ZERO_FAST_CENTER:
            bonus += 8.0
        if file_index in {0, 7}:
            bonus -= 7.0
        return bonus
    if piece_type == chess.KNIGHT:
        bonus = (center_file * 9.0) + (center_rank * 6.0) - (edge_penalty * 42.0)
        if relative_rank >= 2:
            bonus += 22.0
        return bonus
    if piece_type == chess.BISHOP:
        bonus = (center_file * 4.0) + (center_rank * 4.0)
        if relative_rank >= 1:
            bonus += 18.0
        return bonus
    if piece_type == chess.ROOK:
        bonus = 0.0
        if relative_rank >= 3:
            bonus += 18.0
        if file_index in {3, 4}:
            bonus += 8.0
        return bonus
    if piece_type == chess.QUEEN:
        bonus = (center_file * 2.0) + (center_rank * 2.0)
        if relative_rank >= 2:
            bonus += 8.0
        return bonus
    if piece_type == chess.KING:
        if relative_rank <= 1 and file_index in {1, 2, 5, 6}:
            return 18.0
        if relative_rank >= 3:
            return -28.0
    return 0.0


def pawn_structure_fast_score(board: chess.Board, color: bool) -> float:
    score = 0.0
    own_files = [0] * 8
    enemy_files = [0] * 8
    own_pawns = list(board.pieces(chess.PAWN, color))
    enemy_pawns = list(board.pieces(chess.PAWN, not color))
    for square in own_pawns:
        own_files[chess.square_file(square)] += 1
    for square in enemy_pawns:
        enemy_files[chess.square_file(square)] += 1
    for file_index, count in enumerate(own_files):
        if count > 1:
            score -= 14.0 * (count - 1)
        if count and not any(own_files[adj] for adj in (file_index - 1, file_index + 1) if 0 <= adj < 8):
            score -= 8.0
    for file_index, count in enumerate(enemy_files):
        if count > 1:
            score += 14.0 * (count - 1)
        if count and not any(enemy_files[adj] for adj in (file_index - 1, file_index + 1) if 0 <= adj < 8):
            score += 8.0
    for square in own_pawns:
        file_index = chess.square_file(square)
        rank = chess.square_rank(square)
        relative_rank = rank if color == chess.WHITE else 7 - rank
        blocked = False
        for enemy_square in enemy_pawns:
            enemy_file = chess.square_file(enemy_square)
            enemy_rank = chess.square_rank(enemy_square)
            if abs(enemy_file - file_index) <= 1:
                if color == chess.WHITE and enemy_rank > rank:
                    blocked = True
                    break
                if color == chess.BLACK and enemy_rank < rank:
                    blocked = True
                    break
        if not blocked:
            score += passed_pawn_fast_bonus(relative_rank)
    enemy_color = not color
    for square in enemy_pawns:
        file_index = chess.square_file(square)
        rank = chess.square_rank(square)
        relative_rank = rank if enemy_color == chess.WHITE else 7 - rank
        blocked = False
        for own_square in own_pawns:
            own_file = chess.square_file(own_square)
            own_rank = chess.square_rank(own_square)
            if abs(own_file - file_index) <= 1:
                if enemy_color == chess.WHITE and own_rank > rank:
                    blocked = True
                    break
                if enemy_color == chess.BLACK and own_rank < rank:
                    blocked = True
                    break
        if not blocked:
            score -= passed_pawn_fast_bonus(relative_rank)
    return score


def passed_pawn_fast_bonus(relative_rank: int) -> float:
    bonus = max(0.0, float(relative_rank) - 1.0) * 14.0
    if relative_rank >= 5:
        bonus += 70.0
    if relative_rank >= 6:
        bonus += 150.0
    if relative_rank >= 7:
        bonus += 320.0
    return bonus


def loose_minor_major_fast_score(board: chess.Board, color: bool) -> float:
    score = 0.0
    for square, piece in board.piece_map().items():
        if piece.piece_type in {chess.PAWN, chess.KING}:
            continue
        value = float(PIECE_VALUES.get(piece.piece_type, 0))
        attackers = board.attackers(not piece.color, square)
        if not attackers:
            continue
        defenders = board.attackers(piece.color, square)
        attacker_values = [
            PIECE_VALUES.get(attacker.piece_type, 0)
            for attacker_square in attackers
            if (attacker := board.piece_at(attacker_square)) is not None
        ]
        least_attacker = min(attacker_values) if attacker_values else value
        penalty = 0.0
        if not defenders:
            penalty = value * 0.58
        elif len(attackers) > len(defenders) and least_attacker <= value:
            penalty = value * 0.28
        elif piece.piece_type in {chess.ROOK, chess.QUEEN} and least_attacker < value:
            penalty = value * 0.10
        if penalty:
            score += (-penalty if piece.color == color else penalty)
    return score


def fast_king_safety_score(board: chess.Board, color: bool) -> float:
    king_square = board.king(color)
    if king_square is None:
        return -900.0
    score = 0.0
    home_rank = 0 if color == chess.WHITE else 7
    home_king_square = chess.E1 if color == chess.WHITE else chess.E8
    if is_castled_shape(board, color, king_square):
        score += 220.0
    elif king_square != home_king_square and board.fullmove_number <= 18:
        score -= 420.0
    elif king_square == home_king_square and board.fullmove_number <= 14:
        score -= 130.0
        for file_index in (3, 4):
            pawn = board.piece_at(chess.square(file_index, home_rank + (1 if color == chess.WHITE else -1)))
            if pawn is None or pawn.color != color or pawn.piece_type != chess.PAWN:
                score -= 65.0
    enemy_attackers = len(board.attackers(not color, king_square))
    own_defenders = len(board.attackers(color, king_square))
    score -= 52.0 * enemy_attackers
    score += 16.0 * own_defenders
    shield_rank = chess.square_rank(king_square) + (1 if color == chess.WHITE else -1)
    if 0 <= shield_rank < 8:
        for file_index in range(max(0, chess.square_file(king_square) - 1), min(7, chess.square_file(king_square) + 1) + 1):
            shield = board.piece_at(chess.square(file_index, shield_rank))
            if shield is not None and shield.color == color and shield.piece_type == chess.PAWN:
                score += 18.0
    return score


def terminal_score_for_color(board: chess.Board, color: bool, ply: int = 0) -> float | None:
    if board.halfmove_clock >= 100 or board.is_insufficient_material():
        return 0.0
    if not board.is_check():
        return None
    if any(board.legal_moves):
        return None
    winner = not board.turn
    score = ZERO_ALPHA_BETA_MATE_SCORE - float(ply)
    return score if winner == color else -score


def fast_zero_leaf_evaluation(board: chess.Board, color: bool) -> float:
    terminal = terminal_score_for_color(board, color)
    if terminal is not None:
        return terminal
    score = 0.0
    bishop_counts = {chess.WHITE: 0, chess.BLACK: 0}
    for square, piece in board.piece_map().items():
        sign = 1.0 if piece.color == color else -1.0
        score += sign * float(PIECE_VALUES.get(piece.piece_type, 0))
        score += sign * fast_piece_activity_bonus(piece.piece_type, square, piece.color)
        if square in ZERO_FAST_CENTER:
            score += sign * 12.0
        elif square in ZERO_FAST_EXTENDED_CENTER:
            score += sign * 5.0
        if piece.piece_type == chess.BISHOP:
            bishop_counts[piece.color] += 1
    if bishop_counts[color] >= 2:
        score += 35.0
    if bishop_counts[not color] >= 2:
        score -= 35.0
    score += pawn_structure_fast_score(board, color)
    score += fast_king_safety_score(board, color)
    score -= fast_king_safety_score(board, not color)
    score += opening_discipline_score(board, color)
    score -= opening_discipline_score(board, not color)
    if board.turn == color:
        score += min(35.0, board.legal_moves.count() * 1.1)
    else:
        score -= min(35.0, board.legal_moves.count() * 1.1)
    return score


def material_capped_safety_score(board: chess.Board, color: bool) -> float:
    terminal = terminal_score_for_color(board, color)
    if terminal is not None:
        return terminal
    raw_material = float(material_balance(board, color))
    safety_score = fast_zero_leaf_evaluation(board, color) + loose_minor_major_fast_score(board, color)
    return min(safety_score, raw_material + 180.0)


def alpha_beta_cache_key(board: chess.Board) -> object:
    key_func = getattr(board, "_transposition_key", None)
    if callable(key_func):
        return key_func()
    return board.fen()


def tactical_move_order_score(board: chess.Board, move: chess.Move) -> tuple[float, str]:
    mover = board.piece_at(move.from_square)
    captured = board.piece_at(move.to_square)
    if board.is_en_passant(move):
        offset = -8 if board.turn == chess.WHITE else 8
        captured = board.piece_at(move.to_square + offset)
    captured_value = PIECE_VALUES.get(captured.piece_type, 0) if captured else 0
    mover_value = PIECE_VALUES.get(mover.piece_type, 0) if mover else 0
    promotion_value = PIECE_VALUES.get(move.promotion, 0) if move.promotion else 0
    score = float((captured_value * 10) - mover_value + promotion_value)
    if board.gives_check(move):
        score += 600.0
    if move.to_square in {chess.C4, chess.D4, chess.E4, chess.F4, chess.C5, chess.D5, chess.E5, chess.F5}:
        score += 25.0
    if mover and mover.piece_type == chess.PAWN:
        from_rank = chess.square_rank(move.from_square)
        to_rank = chess.square_rank(move.to_square)
        if (mover.color == chess.WHITE and to_rank > from_rank) or (mover.color == chess.BLACK and to_rank < from_rank):
            score += 12.0
    if mover and mover.piece_type in {chess.KNIGHT, chess.BISHOP}:
        from_rank = chess.square_rank(move.from_square)
        if (mover.color == chess.WHITE and from_rank == 0) or (mover.color == chess.BLACK and from_rank == 7):
            score += 18.0
    return (score, move.uci())


def ordered_tactical_moves(board: chess.Board, width: int = DELIBERATIVE_LOCAL_SEARCH_WIDTH) -> list[chess.Move]:
    moves = list(board.legal_moves)
    moves.sort(key=lambda move: tactical_move_order_score(board, move), reverse=True)
    return moves[: max(1, int(width))]


def ordered_noisy_moves(board: chess.Board, width: int = DELIBERATIVE_QUIESCENCE_WIDTH) -> list[chess.Move]:
    moves = [
        move
        for move in board.legal_moves
        if board.is_capture(move) or board.gives_check(move) or bool(move.promotion)
    ]
    moves.sort(key=lambda move: tactical_move_order_score(board, move), reverse=True)
    return moves[: max(1, int(width))]


def quiescence_search(
    board: chess.Board,
    root_color: bool,
    alpha: float,
    beta: float,
    depth: int = DELIBERATIVE_QUIESCENCE_PLIES,
    width: int = DELIBERATIVE_QUIESCENCE_WIDTH,
) -> float:
    stand_pat = static_zero_evaluation(board, root_color)
    if depth <= 0 or board.is_game_over(claim_draw=True):
        return stand_pat
    moves = ordered_noisy_moves(board, width=width)
    if not moves:
        return stand_pat
    if board.turn == root_color:
        value = stand_pat
        alpha = max(alpha, value)
        for move in moves:
            child = board.copy(stack=False)
            child.push(move)
            value = max(value, quiescence_search(child, root_color, alpha, beta, depth - 1, width))
            alpha = max(alpha, value)
            if alpha >= beta:
                break
        return value
    value = stand_pat
    beta = min(beta, value)
    for move in moves:
        child = board.copy(stack=False)
        child.push(move)
        value = min(value, quiescence_search(child, root_color, alpha, beta, depth - 1, width))
        beta = min(beta, value)
        if alpha >= beta:
            break
    return value


def local_tactical_search(
    board: chess.Board,
    root_color: bool,
    depth: int,
    alpha: float,
    beta: float,
    width: int = DELIBERATIVE_LOCAL_SEARCH_WIDTH,
    cache: dict[tuple[str, bool, int], float] | None = None,
) -> float:
    if depth <= 0 or board.is_game_over(claim_draw=True):
        return quiescence_search(board, root_color, alpha, beta)
    key = (board.fen(), root_color, int(depth))
    if cache is not None and key in cache:
        return cache[key]
    moves = ordered_tactical_moves(board, width=width)
    cutoff = False
    if board.turn == root_color:
        value = -1_000_000.0
        for move in moves:
            child = board.copy(stack=False)
            child.push(move)
            value = max(value, local_tactical_search(child, root_color, depth - 1, alpha, beta, width, cache))
            alpha = max(alpha, value)
            if alpha >= beta:
                cutoff = True
                break
        if cache is not None and not cutoff:
            cache[key] = value
        return value
    value = 1_000_000.0
    for move in moves:
        child = board.copy(stack=False)
        child.push(move)
        value = min(value, local_tactical_search(child, root_color, depth - 1, alpha, beta, width, cache))
        beta = min(beta, value)
        if alpha >= beta:
            cutoff = True
            break
    if cache is not None and not cutoff:
        cache[key] = value
    return value


def local_tactical_score(
    board: chess.Board,
    move: chess.Move,
    plies: int = DELIBERATIVE_LOCAL_SEARCH_PLIES,
    width: int = DELIBERATIVE_LOCAL_SEARCH_WIDTH,
    cache: dict[tuple[str, bool, int], float] | None = None,
) -> float:
    root_color = board.turn
    child = board.copy(stack=False)
    child.push(move)
    return local_tactical_search(
        child,
        root_color,
        max(0, int(plies) - 1),
        -1_000_000.0,
        1_000_000.0,
        width=max(1, int(width)),
        cache=cache,
    )


def normalize_local_tactical_score(score_cp: float) -> float:
    return max(-4.0, min(4.0, float(score_cp) / 600.0))


def local_search_plies_for_visits(visits: int) -> int:
    if int(visits) >= 16:
        return DELIBERATIVE_LOCAL_SEARCH_PLIES
    return 2


def alpha_beta_depth_for_visits(visits: int, time_limit_ms: int | None = None) -> int:
    if time_limit_ms is None:
        return 3 if int(visits) >= 32 else 2
    if time_limit_ms is not None and time_limit_ms <= 600:
        return 3
    if int(visits) >= 48:
        return ZERO_ALPHA_BETA_MAX_DEPTH
    if int(visits) >= 16:
        return 4
    return 3


def has_advanced_enemy_passer(board: chess.Board, color: bool) -> bool:
    own_pawns = list(board.pieces(chess.PAWN, color))
    enemy_color = not color
    for square in board.pieces(chess.PAWN, enemy_color):
        file_index = chess.square_file(square)
        rank = chess.square_rank(square)
        relative_rank = rank if enemy_color == chess.WHITE else 7 - rank
        if relative_rank < 5:
            continue
        blocked = False
        for own_square in own_pawns:
            own_file = chess.square_file(own_square)
            own_rank = chess.square_rank(own_square)
            if abs(own_file - file_index) <= 1:
                if enemy_color == chess.WHITE and own_rank > rank:
                    blocked = True
                    break
                if enemy_color == chess.BLACK and own_rank < rank:
                    blocked = True
                    break
        if not blocked:
            return True
    return False


def king_zone_pressure(board: chess.Board, color: bool) -> int:
    king_square = board.king(color)
    if king_square is None:
        return 4
    king_file = chess.square_file(king_square)
    king_rank = chess.square_rank(king_square)
    pressure = len(board.attackers(not color, king_square)) * 2
    for square, piece in board.piece_map().items():
        if piece.color == color or piece.piece_type == chess.PAWN:
            continue
        distance = max(abs(chess.square_file(square) - king_file), abs(chess.square_rank(square) - king_rank))
        if piece.piece_type == chess.QUEEN and distance <= 4:
            pressure += 2
        elif piece.piece_type == chess.ROOK and distance <= 3:
            pressure += 2
        elif piece.piece_type in {chess.BISHOP, chess.KNIGHT} and distance <= 2:
            pressure += 1
    return pressure


def tactical_alert_for_deeper_search(board: chess.Board) -> bool:
    color = board.turn
    if board.is_check():
        return True
    piece_count = len(board.piece_map())
    if piece_count <= 14 or board.fullmove_number >= 36:
        return True
    if material_balance(board, color) <= -250:
        return True
    if has_advanced_enemy_passer(board, color):
        return True
    if king_zone_pressure(board, color) >= 3:
        return True
    return False


def adaptive_alpha_beta_depth(board: chess.Board, base_depth: int) -> int:
    target = max(ZERO_ALPHA_BETA_MIN_DEPTH, int(base_depth))
    piece_count = len(board.piece_map())
    if piece_count <= 10 or board.fullmove_number >= 55:
        return max(target, 4)
    if target < 3 and tactical_alert_for_deeper_search(board):
        return 3
    return target


def alpha_beta_terminal_score(board: chess.Board, ply: int = 0) -> float | None:
    return terminal_score_for_color(board, board.turn, ply=ply)


def alpha_beta_static_eval(board: chess.Board) -> float:
    terminal = alpha_beta_terminal_score(board)
    if terminal is not None:
        return terminal
    return fast_zero_leaf_evaluation(board, board.turn)


def alpha_beta_move_order_score(board: chess.Board, move: chess.Move, preferred: chess.Move | None = None) -> tuple[float, str]:
    if preferred is not None and move == preferred:
        return (1_000_000.0, move.uci())
    mover = board.piece_at(move.from_square)
    captured = board.piece_at(move.to_square)
    if board.is_en_passant(move):
        offset = -8 if board.turn == chess.WHITE else 8
        captured = board.piece_at(move.to_square + offset)
    captured_value = PIECE_VALUES.get(captured.piece_type, 0) if captured else 0
    mover_value = PIECE_VALUES.get(mover.piece_type, 0) if mover else 0
    promotion_value = PIECE_VALUES.get(move.promotion, 0) if move.promotion else 0
    to_file = chess.square_file(move.to_square)
    to_rank = chess.square_rank(move.to_square)
    from_rank = chess.square_rank(move.from_square)
    score = float((captured_value * 16) - mover_value + (promotion_value * 12))
    if board.gives_check(move):
        score += 4500.0
    if board.is_castling(move):
        score += 1600.0
    if to_file in {3, 4} and to_rank in {3, 4}:
        score += 260.0
    elif to_file in {2, 3, 4, 5} and to_rank in {2, 3, 4, 5}:
        score += 90.0
    if mover and mover.piece_type in {chess.KNIGHT, chess.BISHOP}:
        back_rank = 0 if mover.color == chess.WHITE else 7
        if from_rank == back_rank:
            score += 180.0
        if to_file in {0, 7} or to_rank in {0, 7}:
            score -= 220.0
    if mover and mover.piece_type == chess.PAWN:
        if to_file in {3, 4}:
            score += 140.0
        if to_file in {0, 7} and not board.is_capture(move):
            score -= 120.0
    if mover and mover.piece_type == chess.QUEEN and board.fullmove_number <= 10 and not board.is_capture(move):
        score -= 160.0
    if mover and mover.piece_type == chess.KING and not board.is_castling(move) and not board.is_check():
        if board.fullmove_number <= 18:
            score -= 260.0
    return (score, move.uci())


def ordered_alpha_beta_moves(
    board: chess.Board,
    preferred: chess.Move | None = None,
    width: int | None = None,
) -> list[chess.Move]:
    moves = list(board.legal_moves)
    moves.sort(key=lambda move: alpha_beta_move_order_score(board, move, preferred), reverse=True)
    if width is not None and width > 0 and not board.is_check():
        forcing = [move for move in moves if board.gives_check(move) or board.is_capture(move) or bool(move.promotion)]
        selected: dict[str, chess.Move] = {move.uci(): move for move in forcing[: max(4, width // 3)]}
        for move in moves:
            selected.setdefault(move.uci(), move)
            if len(selected) >= width:
                break
        return list(selected.values())
    return moves


def alpha_beta_quiescence(
    board: chess.Board,
    alpha: float,
    beta: float,
    deadline: float | None,
    stats: dict[str, object],
    qdepth: int = 0,
) -> float:
    if deadline is not None and time.monotonic() >= deadline:
        stats["time_up"] = True
        return alpha_beta_static_eval(board)
    stand_pat = alpha_beta_static_eval(board)
    if stand_pat >= beta:
        return beta
    if stand_pat > alpha:
        alpha = stand_pat
    if qdepth >= ZERO_ALPHA_BETA_QUIESCENCE_PLIES or terminal_score_for_color(board, board.turn) is not None:
        return alpha
    if board.is_check():
        moves = ordered_alpha_beta_moves(board, width=ZERO_ALPHA_BETA_QUIESCENCE_WIDTH)
    else:
        noisy = [move for move in board.legal_moves if board.is_capture(move) or board.gives_check(move) or bool(move.promotion)]
        noisy.sort(key=lambda move: alpha_beta_move_order_score(board, move), reverse=True)
        moves = noisy[:ZERO_ALPHA_BETA_QUIESCENCE_WIDTH]
    for move in moves:
        board.push(move)
        score = -alpha_beta_quiescence(board, -beta, -alpha, deadline, stats, qdepth + 1)
        board.pop()
        if score >= beta:
            return beta
        if score > alpha:
            alpha = score
    return alpha


def alpha_beta_search(
    board: chess.Board,
    depth: int,
    alpha: float,
    beta: float,
    deadline: float | None,
    stats: dict[str, object],
    table: dict[tuple[object, int], tuple[float, int, chess.Move | None]],
    ply: int = 0,
) -> float:
    if deadline is not None and time.monotonic() >= deadline:
        stats["time_up"] = True
        return alpha_beta_static_eval(board)
    terminal = alpha_beta_terminal_score(board, ply=ply)
    if terminal is not None:
        return terminal
    stats["nodes"] = int(stats.get("nodes", 0)) + 1
    if depth <= 0:
        return alpha_beta_quiescence(board, alpha, beta, deadline, stats)
    key = (alpha_beta_cache_key(board), int(depth))
    alpha_original = alpha
    tt_move: chess.Move | None = None
    cached = table.get(key)
    if cached is not None:
        cached_score, flag, cached_move = cached
        tt_move = cached_move
        if flag == 0:
            return cached_score
        if flag < 0 and cached_score <= alpha:
            return cached_score
        if flag > 0 and cached_score >= beta:
            return cached_score
    best_move: chess.Move | None = None
    width = ZERO_ALPHA_BETA_SEARCH_WIDTH if depth >= 3 and not board.is_check() else None
    moves = ordered_alpha_beta_moves(board, preferred=tt_move, width=width)
    if not moves:
        return -ZERO_ALPHA_BETA_MATE_SCORE + float(ply) if board.is_check() else 0.0
    for move in moves:
        board.push(move)
        extension = 1 if board.is_check() and depth <= 2 else 0
        score = -alpha_beta_search(board, depth - 1 + extension, -beta, -alpha, deadline, stats, table, ply + 1)
        board.pop()
        if bool(stats.get("time_up")):
            break
        if score >= beta:
            table[key] = (score, 1, move)
            return score
        if score > alpha:
            alpha = score
            best_move = move
    flag = 0 if alpha_original < alpha < beta else -1 if alpha <= alpha_original else 1
    table[key] = (alpha, flag, best_move)
    return alpha


def alpha_beta_root_search(
    board: chess.Board,
    max_depth: int,
    time_limit_ms: int | None = None,
) -> dict:
    legal = list(board.legal_moves)
    if not legal:
        raise ValueError("no legal moves available")
    for move in ordered_alpha_beta_moves(board, width=12):
        board.push(move)
        is_mate = board.is_checkmate()
        board.pop()
        if is_mate:
            return {
                "move": move,
                "score_cp": int(ZERO_ALPHA_BETA_MATE_SCORE),
                "depth": 1,
                "target_depth": max(1, int(max_depth)),
                "nodes": 1,
                "time_limit_ms": time_limit_ms if time_limit_ms is not None else ZERO_ALPHA_BETA_DEFAULT_TIME_LIMIT_MS,
                "time_up": False,
                "training_sources": {source: False for source in FORBIDDEN_TRAINING_SOURCES},
            }
    effective_limit_ms = time_limit_ms if time_limit_ms is not None else 0
    deadline = time.monotonic() + max(0.05, effective_limit_ms / 1000.0) if effective_limit_ms > 0 else None
    table: dict[tuple[object, int], tuple[float, int, chess.Move | None]] = {}
    stats: dict[str, object] = {"nodes": 0, "time_up": False}
    best_move = legal[0]
    best_score = -ZERO_ALPHA_BETA_INF
    depth_reached = 0
    target_depth = max(ZERO_ALPHA_BETA_MIN_DEPTH, min(ZERO_ALPHA_BETA_MAX_DEPTH, int(max_depth)))
    for depth in range(1, target_depth + 1):
        if bool(stats.get("time_up")) and depth > 1:
            break
        depth_best = best_move
        depth_score = -ZERO_ALPHA_BETA_INF
        alpha = -ZERO_ALPHA_BETA_INF
        beta = ZERO_ALPHA_BETA_INF
        moves = ordered_alpha_beta_moves(board, preferred=best_move)
        for move in moves:
            if deadline is not None and time.monotonic() >= deadline and depth > 1:
                stats["time_up"] = True
                break
            board.push(move)
            score = -alpha_beta_search(board, depth - 1, -beta, -alpha, deadline, stats, table, ply=1)
            board.pop()
            if bool(stats.get("time_up")) and depth > 1:
                break
            if score > depth_score:
                depth_score = score
                depth_best = move
            if score > alpha:
                alpha = score
        if depth_score > -ZERO_ALPHA_BETA_INF:
            best_move = depth_best
            best_score = depth_score
            depth_reached = depth
    return {
        "move": best_move,
        "score_cp": int(round(best_score if best_score > -ZERO_ALPHA_BETA_INF else alpha_beta_static_eval(board))),
        "depth": depth_reached,
        "target_depth": target_depth,
        "nodes": int(stats.get("nodes", 0)),
        "time_limit_ms": effective_limit_ms,
        "time_up": bool(stats.get("time_up")),
        "training_sources": {source: False for source in FORBIDDEN_TRAINING_SOURCES},
    }


def worst_reply_static_score(board: chess.Board, move: chess.Move, color: bool) -> float:
    after = board.copy(stack=False)
    after.push(move)
    terminal = terminal_score_for_color(after, color, ply=1)
    if terminal is not None:
        return terminal
    replies = list(after.legal_moves)
    if not replies:
        return material_capped_safety_score(after, color)
    worst = ZERO_ALPHA_BETA_INF
    for reply in replies:
        after.push(reply)
        if after.is_checkmate():
            score = -ZERO_ALPHA_BETA_MATE_SCORE
        else:
            score = material_capped_safety_score(after, color)
        after.pop()
        worst = min(worst, score)
    return worst


def worst_reply_material_floor(board: chess.Board, move: chess.Move, color: bool) -> float:
    after = board.copy(stack=False)
    after.push(move)
    terminal = terminal_score_for_color(after, color, ply=1)
    if terminal is not None:
        return terminal
    replies = list(after.legal_moves)
    if not replies:
        return float(material_balance(after, color))
    floor = ZERO_ALPHA_BETA_INF
    for reply in replies:
        after.push(reply)
        terminal = terminal_score_for_color(after, color, ply=2)
        score = terminal if terminal is not None else float(material_balance(after, color))
        after.pop()
        floor = min(floor, score)
    return floor


def material_safety_override(
    board: chess.Board,
    proposed: chess.Move,
    threshold_cp: float = 120.0,
    scan_width: int = 18,
) -> dict:
    color = board.turn
    current_material = float(material_balance(board, color))
    proposed_score = worst_reply_static_score(board, proposed, color)
    proposed_material_floor = worst_reply_material_floor(board, proposed, color)
    best_move = proposed
    best_score = proposed_score
    best_material_floor = proposed_material_floor
    for move in ordered_alpha_beta_moves(board, preferred=proposed, width=scan_width):
        score = worst_reply_static_score(board, move, color)
        material_floor = worst_reply_material_floor(board, move, color)
        if score > best_score or (score == best_score and material_floor > best_material_floor):
            best_score = score
            best_move = move
            best_material_floor = material_floor
    sacrifice_guard = (
        best_move != proposed
        and (current_material - proposed_material_floor) >= 250.0
        and (best_material_floor - proposed_material_floor) >= 90.0
        and (best_score - proposed_score) >= 40.0
    )
    overridden = best_move != proposed and ((best_score - proposed_score) >= threshold_cp or sacrifice_guard)
    return {
        "move": best_move if overridden else proposed,
        "overridden": overridden,
        "proposed": proposed.uci(),
        "selected": best_move.uci() if overridden else proposed.uci(),
        "proposed_score_cp": int(round(proposed_score)),
        "selected_score_cp": int(round(best_score if overridden else proposed_score)),
        "proposed_material_floor_cp": int(round(proposed_material_floor)),
        "selected_material_floor_cp": int(round(best_material_floor if overridden else proposed_material_floor)),
        "sacrifice_guard": bool(sacrifice_guard),
        "threshold_cp": int(round(threshold_cp)),
        "training_sources": {source: False for source in FORBIDDEN_TRAINING_SOURCES},
    }


def fast_safety_root_search(board: chess.Board, width: int = 18) -> dict:
    moves = ordered_alpha_beta_moves(board, width=width)
    if not moves:
        raise ValueError("no legal moves available")
    best_move = moves[0]
    best_score = -ZERO_ALPHA_BETA_INF
    nodes = 0
    for move in moves:
        score = worst_reply_static_score(board, move, board.turn)
        nodes += 1
        if score > best_score:
            best_score = score
            best_move = move
    return {
        "move": best_move,
        "score_cp": int(round(best_score)),
        "depth": 2,
        "target_depth": 2,
        "nodes": nodes,
        "time_limit_ms": 0,
        "time_up": False,
        "fast_safety_root": True,
        "training_sources": {source: False for source in FORBIDDEN_TRAINING_SOURCES},
    }


def merge_alpha_beta_candidate(board: chess.Board, candidates: list[dict], search: dict) -> list[dict]:
    move = search.get("move")
    if not isinstance(move, chess.Move):
        return candidates
    rows = [dict(candidate) for candidate in candidates]
    selected = None
    for row in rows:
        if row.get("uci") == move.uci():
            selected = row
            break
    if selected is None:
        features = move_features(board, move)
        refutation = refutation_check(board, move)
        local_score_cp = local_tactical_score(board, move, plies=1)
        selected = {
            "uci": move.uci(),
            "san": board.san(move),
            "role_tags": move_role_tags(board, move, features),
            "plan_intent": plan_intent_for_move(board, move, features),
            "visits": 0,
            "visit_share": 0.0,
            "prior": 0.0,
            "value": 0.0,
            "human_score": 0.0,
            "local_tactical_score": round(normalize_local_tactical_score(local_score_cp), 6),
            "local_tactical_score_cp": int(round(local_score_cp)),
            "risk": round(features["moved_piece_risk"], 6),
            "gives_check": bool(features["gives_check"]),
            "capture_value": round(features["capture_value"], 6),
            "created_threats": created_threats(board, move),
            "refutation": refutation,
        }
        rows.append(selected)
    selected["alpha_beta_selected"] = True
    selected["alpha_beta_score_cp"] = int(search.get("score_cp", 0) or 0)
    selected["alpha_beta_depth"] = int(search.get("depth", 0) or 0)
    selected["alpha_beta_nodes"] = int(search.get("nodes", 0) or 0)
    selected["human_score"] = max(float(selected.get("human_score", 0.0) or 0.0), 8.0 + (selected["alpha_beta_score_cp"] / 10000.0))
    rows.sort(key=lambda row: (0 if row.get("alpha_beta_selected") else 1, -float(row.get("human_score", 0.0) or 0.0), str(row.get("uci", ""))))
    return rows[:DELIBERATIVE_RETURNED_CANDIDATE_LIMIT]


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
            "method": "local legal-move scan, bounded tactical search, and PUCT visit/value support",
            "selected_refutation": selected.get("refutation") or refutation_check(board, move),
            "selected_local_tactical_score_cp": selected.get("local_tactical_score_cp"),
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


def classify_terminal_kind(board: chess.Board, capped: bool = False) -> str:
    if capped:
        return "capped_draw"
    outcome = board.outcome(claim_draw=True)
    if outcome is None:
        return "in_progress"
    if outcome.winner is not None:
        return "checkmate" if board.is_checkmate() else "decisive"
    if board.can_claim_threefold_repetition() or board.is_repetition(3):
        return "repetition_draw"
    if board.can_claim_fifty_moves() or board.is_fifty_moves():
        return "fifty_move_draw"
    if board.is_stalemate():
        return "stalemate_draw"
    if board.is_insufficient_material():
        return "insufficient_material_draw"
    return "true_draw"


def wdl_target(outcome: float) -> dict[str, float]:
    if outcome > 0.0:
        return {"win": 1.0, "draw": 0.0, "loss": 0.0}
    if outcome < 0.0:
        return {"win": 0.0, "draw": 0.0, "loss": 1.0}
    return {"win": 0.0, "draw": 1.0, "loss": 0.0}


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
        if feature_name == "gives_check":
            return 1.6
        if feature_name in {"capture_value", "promotion_value"}:
            return 0.35
    if outcome < 0.0 and feature_name in FOUNDATIONAL_PROGRESS_FEATURES:
        return 0.0
    return 1.0


def self_play_game(
    network: PolicyValueNetwork,
    visits: int = 16,
    max_plies: int = 160,
    seed: int | None = None,
    exploration_plies: int = 10,
    temperature: float = 1.15,
    root_noise_alpha: float = 0.3,
    root_noise_fraction: float = 0.25,
    visit_jitter_fraction: float = 0.25,
) -> dict:
    rng = random.Random(seed)
    board = chess.Board()
    records = []
    chosen_moves = []
    novelty_archive = build_novelty_archive(load_replay_records(max_records=NOVELTY_REPLAY_LOOKBACK))
    game_novelty_counts: dict[str, int] = {}
    for ply in range(1, max_plies + 1):
        if board.is_game_over(claim_draw=True):
            break
        state_identity = replay_identity(board)
        low_visits = max(1, int(visits * (1.0 - max(0.0, visit_jitter_fraction))))
        high_visits = max(low_visits, int(math.ceil(visits * (1.0 + max(0.0, visit_jitter_fraction)))))
        search_visits = rng.randint(low_visits, high_visits)
        result = run_mcts(
            board,
            network,
            visits=search_visits,
            c_puct=1.5,
            root_noise=True,
            root_noise_alpha=root_noise_alpha,
            root_noise_fraction=root_noise_fraction,
            rng=rng,
        )
        legal_mask = legal_move_mask(board)
        move, selection = select_self_play_move(
            board,
            result,
            rng,
            ply=ply,
            exploration_plies=exploration_plies,
            temperature=temperature,
            novelty_archive=novelty_archive,
            game_novelty_counts=game_novelty_counts,
        )
        novelty = dict(selection.get("novelty") or novelty_profile(board, move, novelty_archive, game_novelty_counts))
        novelty_key = str(novelty.get("key", ""))
        chosen_moves.append(move.uci())
        records.append(
            {
                "schema": "zero-selfplay-position-v2",
                "replay_schema_version": REPLAY_SCHEMA_VERSION,
                "source": "zero_self_play",
                "fen": board.fen(),
                "position_key": state_identity["fen_key"],
                "state_identity": state_identity,
                "repetition_bucket": state_identity["repetition_bucket"],
                "side_to_move": "white" if board.turn == chess.WHITE else "black",
                "legal_move_indices": legal_mask["indices"],
                "root_visit_counts": result.root_visit_counts,
                "visit_policy": result.root_visit_policy,
                "chosen_move": move.uci(),
                "chosen_move_index": move_to_index(move),
                "selection": selection["selection"],
                "greedy_move": selection["greedy_move"],
                "exploration_temperature": selection["temperature"],
                "novelty": novelty,
                "novelty_key": novelty_key,
                "novelty_score": novelty.get("score", 0.0),
                "network_id": network.network_id,
                "search_visits_requested": search_visits,
                "training_sources": {source: False for source in FORBIDDEN_TRAINING_SOURCES},
            }
        )
        if novelty_key:
            game_novelty_counts[novelty_key] = game_novelty_counts.get(novelty_key, 0) + 1
        board.push(move)
        if board.can_claim_draw() and rng.random() < 0.02:
            break
    outcome = board.outcome(claim_draw=True)
    result_text = board.result(claim_draw=True) if outcome else "*"
    terminal_kind = classify_terminal_kind(board, capped=outcome is None)
    outcome_source = "terminal_draw_non_win_penalty" if result_text == "1/2-1/2" else "terminal" if outcome else "self_material_adjudication"
    opening_signature = stable_id(chosen_moves[:8])
    for record in records:
        color = chess.WHITE if record["side_to_move"] == "white" else chess.BLACK
        if result_text == "1/2-1/2":
            record["outcome"] = self_play_draw_outcome(board, color)
        elif result_text != "*":
            record["outcome"] = game_result_value(result_text, color)
        else:
            record["outcome"] = self_play_material_outcome(board, color)
        record["outcome_source"] = outcome_source
        record["terminal_kind"] = terminal_kind
        record["wdl_target"] = {"win": 0.0, "draw": 1.0, "loss": 0.0} if terminal_kind.endswith("_draw") else wdl_target(record["outcome"])
        record["opening_signature"] = opening_signature
    return {
        "schema": "zero-selfplay-game-v2",
        "replay_schema_version": REPLAY_SCHEMA_VERSION,
        "generated_at": now_stamp(),
        "network_id": network.network_id,
        "self_play_policy": {
            "selection": "bounded_seeded_exploration",
            "seed": seed,
            "exploration_plies": exploration_plies,
            "temperature": temperature,
            "root_noise_alpha": root_noise_alpha,
            "root_noise_fraction": root_noise_fraction,
            "visit_jitter_fraction": visit_jitter_fraction,
            "novelty": {
                "selection": "human_readable_novelty_pressure",
                "archive_lookback": NOVELTY_REPLAY_LOOKBACK,
                "weight": SELF_PLAY_NOVELTY_WEIGHT,
                "distinct_keys": len(game_novelty_counts),
            },
            "training_sources": {source: False for source in FORBIDDEN_TRAINING_SOURCES},
        },
        "result": result_text,
        "outcome_source": outcome_source,
        "terminal_kind": terminal_kind,
        "opening_signature": opening_signature,
        "plies": len(records),
        "records": records,
    }


def dedupe_records(records: Iterable[dict]) -> list[dict]:
    merged: dict[str, dict] = {}
    for record in records:
        key = replay_record_key(record)
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
            key = replay_record_key(existing)
            if not key:
                continue
            existing_index[key] = len(existing_records)
            existing_records.append(existing)
    unique = []
    skipped = 0
    updated = 0
    for record in dedupe_records(records):
        key = replay_record_key(record)
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
    novelty = record.get("novelty", {})
    if isinstance(novelty, dict):
        tags = set(str(tag) for tag in novelty.get("tags", []))
        if "archive_new" in tags and "safe_refutation" in tags:
            signal += 0.05
        elif "archive_rare" in tags and "safe_refutation" in tags:
            signal += 0.03
    return signal


def select_training_records(records: list[dict], max_records: int | None = DEFAULT_TRAINING_REPLAY_LIMIT) -> list[dict]:
    if max_records is None or max_records <= 0:
        base = list(records)
    elif len(records) <= max_records:
        base = list(records)
    else:
        recent_count = max(1, int(max_records * TRAINING_REPLAY_RECENT_FRACTION))
        recent_start = max(0, len(records) - recent_count)
        selected_indices = set(range(recent_start, len(records)))
        remaining_slots = max_records - len(selected_indices)
        if remaining_slots > 0:
            older = list(enumerate(records[:recent_start]))
            older.sort(key=lambda item: (replay_training_signal(item[1]), item[0]), reverse=True)
            selected_indices.update(index for index, _ in older[:remaining_slots])
        base = [record for index, record in enumerate(records) if index in selected_indices]
    return cap_duplicate_opening_signatures(base)


def cap_duplicate_opening_signatures(
    records: list[dict],
    limit: int = MAX_TRAINING_RECORDS_PER_OPENING_SIGNATURE,
) -> list[dict]:
    if limit <= 0:
        return list(records)
    counts: dict[str, int] = {}
    selected = []
    for record in records:
        signature = str(record.get("opening_signature") or "")
        if signature:
            current = counts.get(signature, 0)
            if current >= limit:
                continue
            counts[signature] = current + 1
        selected.append(record)
    return selected


def _empty_lesson_counts() -> dict[str, int]:
    return {
        "risky_forcing_non_wins": 0,
        "hanging_piece_non_wins": 0,
        "draw_non_wins": 0,
        "failed_conversion_non_wins": 0,
        "exploration_examples": 0,
        "safe_novelty_examples": 0,
        "risky_novelty_non_wins": 0,
    }


def analyze_self_play_records_for_wisdom(paths: Iterable[Path]) -> dict:
    counts = _empty_lesson_counts()
    games = []
    total_records = 0
    outcome_signal = 0
    novelty_keys: set[str] = set()
    novelty_records = 0
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
            novelty = record.get("novelty", {})
            if isinstance(novelty, dict) and novelty.get("key"):
                novelty_records += 1
                novelty_keys.add(str(novelty.get("key")))
                tags = set(str(tag) for tag in novelty.get("tags", []))
                if "archive_new" in tags and "safe_refutation" in tags:
                    counts["safe_novelty_examples"] += 1
                if outcome <= 0.0 and novelty.get("refutation_status") in {"watch", "unsafe"}:
                    counts["risky_novelty_non_wins"] += 1
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
        "novelty": {
            "records": novelty_records,
            "distinct_keys": len(novelty_keys),
        },
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
    if counts.get("safe_novelty_examples", 0):
        lessons.append(
            {
                "title": "Search for safe novelty before repeating known plans",
                "evidence_count": counts["safe_novelty_examples"],
                "guidance": "Prefer candidate plans that are rare in Zero's own archive while still passing local refutation checks.",
                "status": status,
            }
        )
    if counts.get("risky_novelty_non_wins", 0):
        lessons.append(
            {
                "title": "Novel ideas still need tactical proof",
                "evidence_count": counts["risky_novelty_non_wins"],
                "guidance": "Novel candidate classes should be kept only when current-position reply scans do not mark them watch or unsafe.",
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
            f"- Novelty records: {delta.get('analysis', {}).get('novelty', {}).get('records', 0)}",
            f"- Distinct novelty keys: {delta.get('analysis', {}).get('novelty', {}).get('distinct_keys', 0)}",
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
    policy_loss = 0.0
    value_loss = 0.0
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
            value_scale = 0.25 if str(record.get("terminal_kind", "")) == "capped_draw" else 1.0
            features = move_features(board, move)
            for name, value in features.items():
                scale = training_feature_scale(record, name, outcome)
                weights[name] = weights.get(name, 0.0) + learning_rate * outcome * value * scale * value_scale
            visit_policy = record.get("visit_policy", {})
            if isinstance(visit_policy, dict) and visit_policy:
                evaluation = base.evaluate(board)
                for uci, target_raw in visit_policy.items():
                    try:
                        policy_move = chess.Move.from_uci(str(uci))
                        target = float(target_raw)
                    except (TypeError, ValueError):
                        continue
                    if policy_move not in board.legal_moves:
                        continue
                    prior = float(evaluation.priors.get(policy_move.uci(), 0.0))
                    error = target - prior
                    policy_loss += error * error
                    for name, value in move_features(board, policy_move).items():
                        weights[name] = weights.get(name, 0.0) + (learning_rate * 0.35 * error * value)
            for name, value in board_value_features(board).items():
                weights[name] = weights.get(name, 0.0) + learning_rate * outcome * value * value_scale
            value_estimate = base.value(board)
            value_loss += ((outcome - value_estimate) ** 2) * value_scale
            used += 1
    weights = clamp_learned_weights(weights)
    candidate = PolicyValueNetwork(
        network_id=f"zero-g{base.generation + 1}-{stable_id({'base': base.network_id, 'steps': base.training_steps, 'used': used, 'weights': weights})}",
        generation=base.generation + 1,
        created_at=now_stamp(),
        training_steps=base.training_steps + max(1, epochs),
        source_positions=base.source_positions + used,
        weights=weights,
        training_metrics={
            "records_used": float(used),
            "policy_loss": round(policy_loss / max(1, used), 6),
            "value_loss": round(value_loss / max(1, used), 6),
            "opening_signature_limit": float(MAX_TRAINING_RECORDS_PER_OPENING_SIGNATURE),
        },
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
    commit: bool = True,
    log: bool = True,
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
        "committed": bool(promoted and commit),
        "threshold": threshold,
        "current": incumbent.network_id,
        "candidate": challenger.network_id,
        "match": match,
    }
    if promoted and commit:
        shutil.copyfile(candidate_path, current_path)
        result["reason"] = "candidate met promotion gate" if not force else "forced promotion"
    elif promoted:
        result["reason"] = "candidate met promotion gate pending regression guard" if not force else "forced promotion pending commit"
    else:
        result["reason"] = "candidate did not meet promotion gate"
    if log:
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


def reanalyze_replay_records(
    limit: int = 64,
    visits: int = 16,
    path: Path = REPLAY_BUFFER_PATH,
) -> dict:
    records = load_replay_records(path)
    if not records:
        return {"updated": 0, "path": str(path), "records": 0}
    network = PolicyValueNetwork.load()
    updated_keys: set[str] = set()
    refreshed = 0
    for record in reversed(records):
        if refreshed >= limit:
            break
        key = replay_record_key(record)
        if not key or key in updated_keys:
            continue
        try:
            board = chess.Board(record["fen"])
        except (KeyError, ValueError):
            continue
        if board.is_game_over(claim_draw=True):
            continue
        result = run_mcts(board, network, visits=visits)
        record["visit_policy"] = result.root_visit_policy
        record["root_visit_counts"] = result.root_visit_counts
        record["reanalyzed_at"] = now_stamp()
        record["reanalyzed_with_network"] = network.network_id
        record["reanalysis_training_sources"] = {source: False for source in FORBIDDEN_TRAINING_SOURCES}
        updated_keys.add(key)
        refreshed += 1
    if refreshed:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
    return {
        "updated": refreshed,
        "records": len(records),
        "visits": visits,
        "network_id": network.network_id,
        "path": str(path),
        "training_sources": {source: False for source in FORBIDDEN_TRAINING_SOURCES},
    }


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
    selfplay.add_argument("--exploration-plies", type=int, default=10)
    selfplay.add_argument("--temperature", type=float, default=1.15)

    train = sub.add_parser("train")
    train.add_argument("--epochs", type=int, default=1)
    train.add_argument("--learning-rate", type=float, default=0.05)

    reanalyze = sub.add_parser("reanalyze")
    reanalyze.add_argument("--limit", type=int, default=64)
    reanalyze.add_argument("--visits", type=int, default=16)
    reanalyze.add_argument("--path", type=Path, default=REPLAY_BUFFER_PATH)

    promote = sub.add_parser("promote")
    promote.add_argument("--games", type=int, default=2)
    promote.add_argument("--visits", type=int, default=8)
    promote.add_argument("--threshold", type=float, default=0.55)
    promote.add_argument("--force", action="store_true")

    sub.add_parser("summary")
    args = parser.parse_args()

    if args.command == "self-play":
        paths = run_self_play(args.games, args.visits, args.max_plies, args.seed, args.exploration_plies, args.temperature)
        print(json.dumps({"ok": True, "paths": [str(path) for path in paths]}, indent=2))
    elif args.command == "train":
        network = train_from_replay(learning_rate=args.learning_rate, epochs=args.epochs)
        research_summary()
        print(json.dumps({"ok": True, "candidate": network.to_dict()}, indent=2))
    elif args.command == "promote":
        result = promotion_gate(games=args.games, visits=args.visits, threshold=args.threshold, force=args.force)
        research_summary()
        print(json.dumps({"ok": True, "promotion": result}, indent=2))
    elif args.command == "reanalyze":
        result = reanalyze_replay_records(limit=args.limit, visits=args.visits, path=args.path)
        research_summary()
        print(json.dumps({"ok": True, "reanalysis": result}, indent=2))
    elif args.command == "summary":
        print(json.dumps(research_summary(), indent=2))


if __name__ == "__main__":
    main()
