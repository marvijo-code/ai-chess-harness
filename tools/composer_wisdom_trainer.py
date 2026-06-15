"""Local Composer wisdom training from PGN analysis — no API calls."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import chess
import chess.pgn

ROOT = Path(__file__).resolve().parents[1]
WISDOM_PATH = ROOT / "engines" / "composer-chess" / "composer-wisdom.md"
TRAINING_RULES_PATH = ROOT / "engines" / "composer-chess" / "training-rules.json"
TRAINING_DIR = ROOT / "out" / "composer-training"
BASELINE_PATH = TRAINING_DIR / "wisdom-baseline.md"
STATE_PATH = TRAINING_DIR / "train-state.json"
MANUAL_WISDOM = ROOT / "docs" / "manual-wisdom-master-games.md"

PGN_DIRS = [
    ROOT / "out" / "composer-depth-matches",
    ROOT / "out" / "wisdom-depth-matches",
]

FAILURE_LESSONS: dict[str, tuple[str, str, str]] = {
    "king_march": (
        "Do not march the king into the center or open wings when not forced; shelter first.",
        "C",
        "king_march_opening",
    ),
    "early_queen": (
        "Early queen sorties need a tactical receipt; otherwise development and king safety come first.",
        "C",
        "early_queen_a_h_file",
    ),
    "flank_pawn": (
        "Do not push a/h pawns in the opening unless capturing; contest the center first.",
        "B",
        "flank_pawn_opening",
    ),
    "repetition_draw": (
        "When ahead, reject repetition shuffles; make progress that denies counterplay.",
        "T",
        "repetition_when_ahead",
    ),
    "unsound_capture": (
        "Reject captures that lose material on recapture without forcing follow-up.",
        "C",
        "unsound_knight_capture",
    ),
    "bishop_shuffle": (
        "Do not shuffle bishops to back-rank corners without a concrete tactical point.",
        "B",
        "bishop_corner_shuffle",
    ),
    "passive_opening": (
        "Claim or contest central squares with pawns before quiet piece shuffles.",
        "B",
        "passive_opening_pawn",
    ),
    "slow_development": (
        "Develop at least two minor pieces before repeated moves with the same piece.",
        "B",
        "slow_minor_development",
    ),
}

TWIC_THEME_HINTS = {
    "C": "Deny counterplay before collecting (Bedrock).",
    "B": "Restrict scope before tactics (Beam).",
    "T": "Trade down risk when ahead (Timber).",
    "K": "Activate the king only after simplification (Key).",
}


@dataclass
class FailureHit:
    tag: str
    move_number: int
    san: str


@dataclass
class TrainingResult:
    games_scanned: int = 0
    lessons_added: int = 0
    principles_added: int = 0
    rules_added: int = 0
    chars_added: int = 0
    significant: bool = False
    delta: dict = field(default_factory=dict)


def file_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def read_wisdom() -> str:
    if not WISDOM_PATH.exists():
        return "# Composer-chess wisdom\n\n## Principles\n\n## Training log\n"
    return WISDOM_PATH.read_text(encoding="utf-8")


def principles_block(text: str) -> str:
    match = re.search(r"## Principles\n(.*?)(\n## |\Z)", text, re.S)
    return match.group(1) if match else ""


def snapshot_baseline() -> dict:
    TRAINING_DIR.mkdir(parents=True, exist_ok=True)
    text = read_wisdom()
    BASELINE_PATH.write_text(text, encoding="utf-8")
    payload = {
        "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "hash": file_hash(text),
        "principles_hash": file_hash(principles_block(text)),
        "length": len(text),
    }
    return payload


def wisdom_delta(baseline: dict) -> dict:
    text = read_wisdom()
    current = {
        "hash": file_hash(text),
        "principles_hash": file_hash(principles_block(text)),
        "length": len(text),
    }
    return {
        "baseline": baseline,
        "current": current,
        "chars_added": max(0, current["length"] - baseline["length"]),
        "principles_changed": current["principles_hash"] != baseline["principles_hash"],
        "file_changed": current["hash"] != baseline["hash"],
    }


def is_significant_delta(delta: dict, min_chars: int = 200) -> bool:
    return bool(
        delta.get("principles_changed")
        or int(delta.get("chars_added", 0)) >= min_chars
    )


def composer_color_from_headers(game: chess.pgn.Game) -> chess.Color | None:
    white = game.headers.get("White", "")
    black = game.headers.get("Black", "")
    if "Composer" in white:
        return chess.WHITE
    if "Composer" in black:
        return chess.BLACK
    if "Wisdom" in white:
        return chess.WHITE
    if "Wisdom" in black:
        return chess.BLACK
    return None


def composer_won(result: str, color: chess.Color) -> bool:
    return (result == "1-0" and color == chess.WHITE) or (result == "0-1" and color == chess.BLACK)


def detect_failures(game: chess.pgn.Game, composer: chess.Color) -> list[FailureHit]:
    result = game.headers.get("Result", "*")
    if composer_won(result, composer):
        return []
    hits: list[FailureHit] = []
    board = game.board()
    developed: set[int] = set()
    piece_moves: dict[int, int] = {}
    repetition_seen = False

    for move in game.mainline_moves():
        mover = board.turn
        piece = board.piece_at(move.from_square)
        if piece is None:
            break
        move_no = board.fullmove_number
        if mover == composer:
            san = board.san(move)
            if piece.piece_type == chess.KING and not board.is_check() and not board.is_castling(move):
                if move_no <= 24:
                    hits.append(FailureHit("king_march", move_no, san))
            if piece.piece_type == chess.QUEEN and move_no <= 14 and not board.is_capture(move):
                to_file = chess.square_file(move.to_square)
                if to_file in (0, 7) or board.attackers(not composer, move.to_square):
                    hits.append(FailureHit("early_queen", move_no, san))
            if piece.piece_type == chess.PAWN and move_no <= 12 and not board.is_capture(move):
                if chess.square_file(move.to_square) in (0, 1, 6, 7):
                    hits.append(FailureHit("flank_pawn", move_no, san))
            if piece.piece_type == chess.BISHOP and move_no <= 20 and not board.is_capture(move):
                tf = chess.square_file(move.to_square)
                tr = chess.square_rank(move.to_square)
                if tf in (0, 7) and tr in (0, 7):
                    hits.append(FailureHit("bishop_shuffle", move_no, san))
            if piece.piece_type == chess.PAWN and move_no <= 8:
                tf = chess.square_file(move.to_square)
                tr = chess.square_rank(move.to_square)
                if tf in (2, 3, 4, 5) and tr in (2, 5) and not board.is_capture(move):
                    hits.append(FailureHit("passive_opening", move_no, san))
            if board.is_capture(move):
                board.push(move)
                bad = False
                if board.is_check():
                    board.pop()
                else:
                    for reply in board.legal_moves:
                        if reply.to_square == move.to_square and board.is_capture(reply):
                            bad = True
                            break
                    board.pop()
                if bad and piece.piece_type in {chess.KNIGHT, chess.BISHOP}:
                    hits.append(FailureHit("unsound_capture", move_no, san))
            if piece.piece_type in {chess.KNIGHT, chess.BISHOP}:
                from_rank = chess.square_rank(move.from_square)
                home = 0 if composer == chess.WHITE else 7
                if from_rank == home:
                    developed.add(move.from_square)
            piece_moves[piece.piece_type] = piece_moves.get(piece.piece_type, 0) + 1
            if piece_moves[piece.piece_type] >= 3 and move_no <= 16 and not board.is_capture(move):
                hits.append(FailureHit("slow_development", move_no, san))
            board.push(move)
            if board.is_repetition(3):
                repetition_seen = True
        else:
            board.push(move)

    if repetition_seen and result == "1/2-1/2":
        hits.append(FailureHit("repetition_draw", 0, "repetition"))
    return hits


def existing_principles(text: str) -> set[str]:
    block = principles_block(text)
    return {line.strip().lstrip("- ").lower() for line in block.splitlines() if line.strip().startswith("-")}


def append_principle(text: str, principle: str) -> tuple[str, bool]:
    norm = principle.strip().lower()
    if norm in existing_principles(text):
        return text, False
    if "## Principles" not in text:
        text += "\n## Principles\n"
    parts = text.split("## Principles", 1)
    head = parts[0] + "## Principles"
    tail = parts[1] if len(parts) > 1 else "\n"
    if "\n## " in tail:
        body, rest = tail.split("\n## ", 1)
        new_tail = body.rstrip() + f"\n- {principle}\n\n## " + rest
    else:
        new_tail = tail.rstrip() + f"\n- {principle}\n"
    return head + new_tail, True


def append_training_log(text: str, block: str) -> tuple[str, int]:
    marker = "## Training log"
    if marker not in text:
        text = text.rstrip() + f"\n\n{marker}\n"
    chars = len(block)
    if marker in text:
        head, tail = text.split(marker, 1)
        text = head + marker + tail.rstrip() + "\n" + block
    else:
        text += block
    return text, chars


def load_training_rules() -> dict:
    if TRAINING_RULES_PATH.exists():
        return json.loads(TRAINING_RULES_PATH.read_text(encoding="utf-8"))
    return {
        "schema": "composer-training-rules-v1",
        "min_search_depth": 4,
        "movetime_fraction": 0.92,
        "bans": [],
        "updated_at": None,
    }


def save_training_rules(rules: dict) -> None:
    rules["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    TRAINING_RULES_PATH.parent.mkdir(parents=True, exist_ok=True)
    TRAINING_RULES_PATH.write_text(json.dumps(rules, indent=2) + "\n", encoding="utf-8")


def apply_rule_tag(rules: dict, tag: str) -> bool:
    lesson = FAILURE_LESSONS.get(tag)
    if not lesson:
        return False
    ban = lesson[2]
    bans = rules.setdefault("bans", [])
    if ban in bans:
        return False
    bans.append(ban)
    rules["min_search_depth"] = min(8, int(rules.get("min_search_depth", 4)) + 1)
    rules["movetime_fraction"] = min(0.98, float(rules.get("movetime_fraction", 0.92)) + 0.01)
    return True


def iter_pgns(limit: int | None = None) -> list[Path]:
    files: list[Path] = []
    for directory in PGN_DIRS:
        if directory.exists():
            files.extend(sorted(directory.glob("*.pgn"), key=lambda p: p.stat().st_mtime, reverse=True))
    if limit:
        return files[:limit]
    return files


def train_from_pgns(
    pgns: list[Path] | None = None,
    min_significant_chars: int = 200,
) -> TrainingResult:
    result = TrainingResult()
    text = read_wisdom()
    before_len = len(text)
    rules = load_training_rules()
    tags_seen: set[str] = set()

    for path in pgns or iter_pgns(limit=40):
        try:
            game = chess.pgn.read_game(path.open(encoding="utf-8"))
        except (OSError, chess.InvalidMoveError, ValueError):
            continue
        if game is None:
            continue
        composer = composer_color_from_headers(game)
        if composer is None:
            continue
        result.games_scanned += 1
        for hit in detect_failures(game, composer):
            if hit.tag in tags_seen:
                continue
            tags_seen.add(hit.tag)
            lesson = FAILURE_LESSONS.get(hit.tag)
            if not lesson:
                continue
            principle, layer, _ban = lesson
            twic = TWIC_THEME_HINTS.get(layer, "")
            text, added = append_principle(text, principle)
            if added:
                result.principles_added += 1
            stamp = time.strftime("%Y-%m-%d")
            block = f"""
### {stamp} — Training from `{path.name}` ({hit.tag})

**Layer {layer}:** {twic}

**Lesson:** {principle}

**Signal:** move {hit.move_number} `{hit.san}` in a non-win.

**Rule:** local ban `{_ban}` (no API).
"""
            text, added_chars = append_training_log(text, block)
            result.chars_added += added_chars
            result.lessons_added += 1
            if apply_rule_tag(rules, hit.tag):
                result.rules_added += 1

    WISDOM_PATH.write_text(text, encoding="utf-8")
    save_training_rules(rules)
    result.chars_added = max(result.chars_added, len(text) - before_len)
    baseline = json.loads(STATE_PATH.read_text(encoding="utf-8"))["baseline"] if STATE_PATH.exists() else snapshot_baseline()
    delta = wisdom_delta(baseline)
    result.delta = delta
    result.significant = is_significant_delta(delta, min_significant_chars)
    return result


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    TRAINING_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Train Composer wisdom locally from PGNs")
    parser.add_argument("--snapshot-baseline", action="store_true")
    parser.add_argument("--train", action="store_true")
    parser.add_argument("--min-chars", type=int, default=200)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.snapshot_baseline:
        baseline = snapshot_baseline()
        state = load_state()
        state["baseline"] = baseline
        save_state(state)
        print(json.dumps(baseline, indent=2) if args.json else baseline)
        return

    if args.train:
        state = load_state()
        if "baseline" not in state:
            state["baseline"] = snapshot_baseline()
            save_state(state)
        outcome = train_from_pgns(min_significant_chars=args.min_chars)
        state["last_training"] = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "games_scanned": outcome.games_scanned,
            "lessons_added": outcome.lessons_added,
            "principles_added": outcome.principles_added,
            "rules_added": outcome.rules_added,
            "significant": outcome.significant,
            "delta": outcome.delta,
        }
        save_state(state)
        print(json.dumps(outcome, default=lambda o: o.__dict__, indent=2) if args.json else outcome)
        return

    parser.print_help()


if __name__ == "__main__":
    main()
