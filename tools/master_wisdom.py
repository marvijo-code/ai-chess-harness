from __future__ import annotations

import argparse
import io
import json
import math
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

import chess
import chess.pgn


ROOT = Path(__file__).resolve().parents[1]
SOURCE_INDEX_URL = "https://database.nikonoel.fr/"
SOURCE_NAME = "Lichess Elite Database"
SOURCE_NOTE = (
    "Filtered master/elite games derived from Lichess standard games. "
    "Use only as generalized learner wisdom, not as Zero training data."
)
ENGINE_CONFIG_PATH = Path.home() / "AppData/Roaming/org.encroissant.app/engines/engines.json"
ELITE_ZIP_RE = re.compile(r"lichess_elite_(?P<month>\d{4}-\d{2})\.zip$", re.I)
TEXT_SUFFIXES = {".md", ".json", ".txt", ".yaml", ".yml"}
MASTER_WISDOM_CLOCK_MS = 1_800_000
OFFSET_CHECKPOINT_INTERVAL = 5_000
PIECE_NAMES = {
    chess.PAWN: "pawn",
    chess.KNIGHT: "knight",
    chess.BISHOP: "bishop",
    chess.ROOK: "rook",
    chess.QUEEN: "queen",
    chess.KING: "king",
}


@dataclass(frozen=True)
class MasterWisdomPaths:
    root: Path
    out_dir: Path
    downloads_dir: Path
    matches_dir: Path
    live_pgn_path: Path
    manifest_path: Path
    state_path: Path
    leaderboard_path: Path
    learner_dir: Path
    learner_knowledgebase_dir: Path
    learner_skills_dir: Path
    wisdom_md_path: Path
    wisdom_json_path: Path
    skill_dir: Path
    skill_path: Path


def default_paths(root: Path = ROOT) -> MasterWisdomPaths:
    out_dir = root / "out" / "lichess-master"
    learner_dir = root / "engines" / "codex-chess-learner"
    return MasterWisdomPaths(
        root=root,
        out_dir=out_dir,
        downloads_dir=out_dir / "downloads",
        matches_dir=out_dir / "matches",
        live_pgn_path=root / "out" / "live" / "master-wisdom-live.pgn",
        manifest_path=out_dir / "manifest.json",
        state_path=out_dir / "master-wisdom-state.json",
        leaderboard_path=out_dir / "leaderboard.json",
        learner_dir=learner_dir,
        learner_knowledgebase_dir=learner_dir / "knowledgebase",
        learner_skills_dir=learner_dir / "skills",
        wisdom_md_path=learner_dir / "knowledgebase" / "master-wisdom.md",
        wisdom_json_path=learner_dir / "knowledgebase" / "master-wisdom.json",
        skill_dir=learner_dir / "skills" / "master-game-wisdom",
        skill_path=learner_dir / "skills" / "master-game-wisdom" / "SKILL.md",
    )


class EliteLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._active: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "a":
            href = dict(attrs).get("href") or ""
            self._active = [href, ""]

    def handle_data(self, data: str) -> None:
        if self._active is not None:
            self._active[1] += data

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._active is not None:
            self.links.append((self._active[0], self._active[1].strip()))
            self._active = None


def utc_stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


def request_url(url: str) -> urllib.request.Request:
    return urllib.request.Request(url, headers={"User-Agent": "chess-harness-codex/1.0"})


def fetch_manifest(paths: MasterWisdomPaths | None = None, source_url: str = SOURCE_INDEX_URL) -> dict:
    paths = paths or default_paths()
    html = urllib.request.urlopen(request_url(source_url), timeout=30).read().decode("utf-8", "replace")
    parser = EliteLinkParser()
    parser.feed(html)
    files: list[dict] = []
    for href, label in parser.links:
        url = urljoin(source_url, href)
        match = ELITE_ZIP_RE.search(url)
        if not match:
            continue
        files.append(
            {
                "month": match.group("month"),
                "label": label or match.group("month"),
                "url": url,
                "filename": Path(url).name,
            }
        )
    files.sort(key=lambda item: item["month"])
    manifest = {
        "schema": "lichess-elite-manifest-v1",
        "source": SOURCE_NAME,
        "source_url": source_url,
        "source_note": SOURCE_NOTE,
        "generated_at": utc_stamp(),
        "files": files,
    }
    paths.out_dir.mkdir(parents=True, exist_ok=True)
    paths.manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def load_manifest(paths: MasterWisdomPaths | None = None, refresh: bool = False) -> dict:
    paths = paths or default_paths()
    if refresh or not paths.manifest_path.exists():
        return fetch_manifest(paths)
    return json.loads(paths.manifest_path.read_text(encoding="utf-8"))


def selected_manifest_files(manifest: dict, latest: bool = False, limit: int | None = None, months: list[str] | None = None) -> list[dict]:
    files = list(manifest.get("files") or [])
    if months:
        allowed = set(months)
        files = [item for item in files if item.get("month") in allowed]
    if latest:
        files = list(reversed(files))
    if limit is not None:
        files = files[: max(0, limit)]
    return files


def remote_content_length(url: str) -> int | None:
    try:
        request = request_url(url)
        request.get_method = lambda: "HEAD"
        with urllib.request.urlopen(request, timeout=30) as response:
            value = response.headers.get("Content-Length")
            return int(value) if value else None
    except Exception:
        return None


def download_file(entry: dict, paths: MasterWisdomPaths | None = None) -> dict:
    paths = paths or default_paths()
    paths.downloads_dir.mkdir(parents=True, exist_ok=True)
    url = str(entry["url"])
    target = paths.downloads_dir / str(entry["filename"])
    expected_size = remote_content_length(url)
    if target.exists() and target.stat().st_size > 0 and (expected_size is None or target.stat().st_size == expected_size):
        return {"filename": target.name, "path": str(target), "downloaded": False, "size": target.stat().st_size}

    partial = target.with_suffix(target.suffix + ".part")
    mode = "ab" if partial.exists() else "wb"
    headers = {"User-Agent": "chess-harness-codex/1.0"}
    existing = partial.stat().st_size if partial.exists() else 0
    if existing:
        headers["Range"] = f"bytes={existing}-"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response, partial.open(mode) as handle:
        if existing and response.status == 200:
            handle.seek(0)
            handle.truncate()
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            handle.write(chunk)
    partial.replace(target)
    return {"filename": target.name, "path": str(target), "downloaded": True, "size": target.stat().st_size}


def download_manifest_files(
    paths: MasterWisdomPaths | None = None,
    latest: bool = True,
    limit: int | None = 1,
    months: list[str] | None = None,
    refresh: bool = False,
) -> dict:
    paths = paths or default_paths()
    manifest = load_manifest(paths, refresh=refresh)
    files = selected_manifest_files(manifest, latest=latest, limit=limit, months=months)
    results = [download_file(entry, paths) for entry in files]
    return {"ok": True, "files": results, "manifest": str(paths.manifest_path)}


def load_repo_config(root: Path = ROOT) -> dict:
    config_path = root / "chess-harness.config.json"
    if config_path.exists():
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}
    return {}


def master_wisdom_config(root: Path = ROOT) -> dict:
    return load_repo_config(root).get("masterWisdom") or {}


def repo_config_value(root: Path, dotted_path: str, default=None):
    current = load_repo_config(root)
    for part in dotted_path.split("."):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return default if current is None else current


def initial_state(root: Path = ROOT) -> dict:
    config = master_wisdom_config(root)
    return {
        "schema": "master-wisdom-state-v1",
        "created_at": utc_stamp(),
        "updated_at": utc_stamp(),
        "source": SOURCE_NAME,
        "source_url": str(config.get("sourceUrl") or SOURCE_INDEX_URL),
        "source_note": SOURCE_NOTE,
        "processed_games": 0,
        "processed_files": {},
        "processed_file_offsets": {},
        "processed_file_offset_counts": {},
        "processed_file_totals": {},
        "batch_size": int(config.get("batchSize") or 500),
        "min_batch_size": 500,
        "max_batch_size": int(config.get("maxBatchSize") or 16000),
        "current_depth": 1,
        "target_depth": int(config.get("targetDepth") or 8),
        "games_per_attempt": int(config.get("gamesPerAttempt") or 10),
        "pass_score": float(config.get("passScore") or 0.8),
        "completed": False,
        "patterns": {},
        "recent_batches": [],
        "attempts": [],
    }


def load_state(paths: MasterWisdomPaths | None = None) -> dict:
    paths = paths or default_paths()
    if not paths.state_path.exists():
        return initial_state(paths.root)
    loaded = json.loads(paths.state_path.read_text(encoding="utf-8"))
    state = initial_state(paths.root)
    state.update(loaded)
    state.pop("openings", None)
    return state


def save_state(state: dict, paths: MasterWisdomPaths | None = None) -> None:
    paths = paths or default_paths()
    paths.out_dir.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = utc_stamp()
    paths.state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def game_result_score(result: str, color: bool) -> float:
    if result == "1/2-1/2":
        return 0.5
    if result == "1-0":
        return 1.0 if color == chess.WHITE else 0.0
    if result == "0-1":
        return 1.0 if color == chess.BLACK else 0.0
    return 0.5


AUTHORED_MD_MARKER = "Authored-by: Codex batch synthesis"
AUTHORED_JSON_SCHEMA = "master-wisdom-authored-v1"
AUTHORED_SKILL_MARKER = "Codex-authored"


def text_file_contains(path: Path, needle: str) -> bool:
    if not path.exists():
        return False
    try:
        return needle in path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def is_authored_wisdom_md(path: Path) -> bool:
    return text_file_contains(path, AUTHORED_MD_MARKER)


def is_authored_wisdom_json(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return False
    return payload.get("schema") == AUTHORED_JSON_SCHEMA or payload.get("authored_by") == "Codex batch synthesis"


def is_authored_skill(path: Path) -> bool:
    return text_file_contains(path, AUTHORED_SKILL_MARKER)


PATTERN_LABELS = {
    "castle_by_12": "Castle before move 12 when legal pressure allows it",
    "no_castle_by_12": "Staying uncastled past move 12 needs concrete compensation",
    "two_minors_by_10": "Develop at least two minor pieces by move 10",
    "slow_minor_development": "Slow minor-piece development often gives up initiative",
    "center_pawn_by_6": "Contest the center with a d/e pawn by move 6",
    "early_queen_move": "Early queen moves need concrete tactical justification",
    "three_checks": "Checks are strongest when they improve piece activity or win time",
    "six_captures": "Capture-heavy games favor the side that keeps king safety and coordination",
    "promotion": "Passed-pawn conversion matters when the endgame arrives",
}
CAUTION_PATTERN_KEYS = {"no_castle_by_12", "slow_minor_development", "early_queen_move"}


HOME_MINOR_SQUARES = {
    chess.WHITE: {chess.B1, chess.G1, chess.C1, chess.F1},
    chess.BLACK: {chess.B8, chess.G8, chess.C8, chess.F8},
}
HOME_CENTER_PAWNS = {
    chess.WHITE: {chess.D2, chess.E2},
    chess.BLACK: {chess.D7, chess.E7},
}
CASTLE_MOVES = {
    chess.WHITE: {chess.Move.from_uci("e1g1"), chess.Move.from_uci("e1c1")},
    chess.BLACK: {chess.Move.from_uci("e8g8"), chess.Move.from_uci("e8c8")},
}


def add_pattern(state: dict, key: str, score: float) -> None:
    patterns = state.setdefault("patterns", {})
    item = patterns.setdefault(
        key,
        {"label": PATTERN_LABELS.get(key, key.replace("_", " ")), "count": 0, "score_sum": 0.0, "wins": 0, "draws": 0, "losses": 0},
    )
    item["count"] += 1
    item["score_sum"] += score
    if score >= 1.0:
        item["wins"] += 1
    elif score <= 0.0:
        item["losses"] += 1
    else:
        item["draws"] += 1


def analyze_game_into_state(state: dict, game: chess.pgn.Game) -> None:
    result = game.headers.get("Result", "*")
    if result not in {"1-0", "0-1", "1/2-1/2"}:
        return
    board = game.board()
    developed: dict[bool, set[int]] = {chess.WHITE: set(), chess.BLACK: set()}
    feature_counts: dict[bool, dict[str, int]] = {
        chess.WHITE: {"castle_by_12": 0, "center_pawn_by_6": 0, "early_queen_move": 0, "checks": 0, "captures": 0, "promotion": 0},
        chess.BLACK: {"castle_by_12": 0, "center_pawn_by_6": 0, "early_queen_move": 0, "checks": 0, "captures": 0, "promotion": 0},
    }
    for move in game.mainline_moves():
        piece = board.piece_at(move.from_square)
        if piece is None:
            break
        color = piece.color
        move_number = board.fullmove_number
        if move in CASTLE_MOVES[color] and move_number <= 12:
            feature_counts[color]["castle_by_12"] = 1
        if piece.piece_type in {chess.KNIGHT, chess.BISHOP} and move.from_square in HOME_MINOR_SQUARES[color] and move_number <= 10:
            developed[color].add(move.from_square)
        if piece.piece_type == chess.PAWN and move.from_square in HOME_CENTER_PAWNS[color] and move_number <= 6:
            feature_counts[color]["center_pawn_by_6"] = 1
        if piece.piece_type == chess.QUEEN and move_number <= 8:
            feature_counts[color]["early_queen_move"] = 1
        if board.is_capture(move):
            feature_counts[color]["captures"] += 1
        if board.gives_check(move):
            feature_counts[color]["checks"] += 1
        if move.promotion:
            feature_counts[color]["promotion"] += 1
        board.push(move)

    for color in (chess.WHITE, chess.BLACK):
        score = game_result_score(result, color)
        if feature_counts[color]["castle_by_12"]:
            add_pattern(state, "castle_by_12", score)
        else:
            add_pattern(state, "no_castle_by_12", score)
        if len(developed[color]) >= 2:
            add_pattern(state, "two_minors_by_10", score)
        else:
            add_pattern(state, "slow_minor_development", score)
        if feature_counts[color]["center_pawn_by_6"]:
            add_pattern(state, "center_pawn_by_6", score)
        if feature_counts[color]["early_queen_move"]:
            add_pattern(state, "early_queen_move", score)
        if feature_counts[color]["checks"] >= 3:
            add_pattern(state, "three_checks", score)
        if feature_counts[color]["captures"] >= 6:
            add_pattern(state, "six_captures", score)
        if feature_counts[color]["promotion"]:
            add_pattern(state, "promotion", score)


def text_offset(handle) -> int | None:
    try:
        return int(handle.tell())
    except (OSError, ValueError):
        return None


def seek_text_offset(handle, offset: int) -> bool:
    if offset <= 0:
        return True
    try:
        handle.seek(offset)
        return True
    except (OSError, ValueError):
        return False


def iter_games_with_offsets(path: Path, start_offset: int = 0):
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            names = [name for name in archive.namelist() if name.lower().endswith(".pgn")]
            if not names:
                return
            with archive.open(names[0], "r") as raw:
                text = io.TextIOWrapper(raw, encoding="utf-8", errors="replace")
                seek_text_offset(text, int(start_offset or 0))
                while True:
                    before = text_offset(text)
                    game = chess.pgn.read_game(text)
                    if game is None:
                        break
                    yield game, before, text_offset(text)
        return
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        seek_text_offset(handle, int(start_offset or 0))
        while True:
            before = text_offset(handle)
            game = chess.pgn.read_game(handle)
            if game is None:
                break
            yield game, before, text_offset(handle)


def iter_games_from_path(path: Path):
    for game, _before, _after in iter_games_with_offsets(path):
        yield game


def entry_download_path(entry: dict, paths: MasterWisdomPaths) -> Path:
    if entry.get("path"):
        return Path(str(entry["path"]))
    return paths.downloads_dir / str(entry["filename"])


def epoch_ms() -> int:
    return int(time.time() * 1000)


def format_clock_comment(ms: int) -> str:
    total_seconds = max(0, int(round(int(ms) / 1000)))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def set_game_clock_headers(game: chess.pgn.Game, white_ms: int, black_ms: int, running_side: str, updated_at_ms: int | None = None) -> None:
    game.headers["WhiteClockMs"] = str(max(0, int(white_ms)))
    game.headers["BlackClockMs"] = str(max(0, int(black_ms)))
    game.headers["ClockUpdatedAtEpochMs"] = str(int(updated_at_ms if updated_at_ms is not None else epoch_ms()))
    game.headers["ClockRunningSide"] = running_side if running_side in {"White", "Black"} else ""


def configured_move_evidence(paths: MasterWisdomPaths) -> dict:
    config = master_wisdom_config(paths.root)
    return {
        "max_ply_per_game": int(config.get("moveEvidenceMaxPlyPerGame") or 160),
        "prompt_trace_limit": int(config.get("moveEvidencePromptTraceLimit") or 24),
        "top_pattern_limit": int(config.get("moveEvidenceTopPatternLimit") or 40),
    }


def latest_failed_attempt_for_learning(state: dict) -> dict | None:
    attempts = list(state.get("attempts") or [])
    if not attempts:
        return None
    latest = attempts[-1]
    if latest.get("passed") or state.get("completed"):
        return None
    return {
        "generated_at": latest.get("generated_at", ""),
        "depth": int(latest.get("depth", 0) or 0),
        "batch_size": int(latest.get("batch_size", 0) or 0),
        "score": round(float(latest.get("score", 0.0) or 0.0), 3),
        "games": int(latest.get("games", 0) or 0),
        "total_games": int(latest.get("total_games", latest.get("games", 0)) or 0),
        "wins": int(latest.get("wins", 0) or 0),
        "draws": int(latest.get("draws", 0) or 0),
        "losses": int(latest.get("losses", 0) or 0),
        "early_stopped": bool(latest.get("early_stopped")),
        "stop_reason": str(latest.get("stop_reason") or ""),
    }


def phase_for_move(move_number: int) -> str:
    if move_number <= 10:
        return "opening"
    if move_number <= 30:
        return "middlegame"
    return "endgame"


def result_label(result: str) -> str:
    if result == "1-0":
        return "white_win"
    if result == "0-1":
        return "black_win"
    if result == "1/2-1/2":
        return "draw"
    return "unknown"


def side_outcome_label(result: str, color: bool) -> str:
    score = game_result_score(result, color)
    if score >= 1.0:
        return "win"
    if score <= 0.0:
        return "loss"
    return "draw"


def abstract_move_events(board: chess.Board, move: chess.Move, piece: chess.Piece, last_capture_square: int | None) -> list[str]:
    color = piece.color
    move_number = board.fullmove_number
    events: list[str] = []
    is_capture = board.is_capture(move)
    gives_check = board.gives_check(move)
    is_castle = board.is_castling(move) or move in CASTLE_MOVES[color]
    if is_castle:
        events.append("castle_king_to_safety")
    elif piece.piece_type == chess.KING and move_number <= 12:
        events.append("early_king_move_without_castling")
    if piece.piece_type in {chess.KNIGHT, chess.BISHOP} and move.from_square in HOME_MINOR_SQUARES[color] and move_number <= 10:
        events.append("develop_home_minor")
    if piece.piece_type == chess.PAWN and move.from_square in HOME_CENTER_PAWNS[color] and move_number <= 6:
        events.append("contest_center_with_home_pawn")
    if piece.piece_type == chess.PAWN and abs(chess.square_rank(move.to_square) - chess.square_rank(move.from_square)) == 2:
        events.append("two_square_pawn_break")
    if piece.piece_type == chess.QUEEN and move_number <= 8:
        events.append("early_queen_move")
    if piece.piece_type == chess.ROOK and move_number <= 12:
        events.append("early_rook_move")
    if is_capture:
        events.append("capture")
    if is_capture and last_capture_square == move.to_square:
        events.append("recapture")
    if gives_check:
        events.append("check")
    if move.promotion:
        events.append("promotion")
    return events


def abstract_game_move_evidence(game: chess.pgn.Game, source_file: str, game_index: int, max_ply_per_game: int) -> dict:
    result = game.headers.get("Result", "*")
    board = game.board()
    trace: list[dict] = []
    event_rows: list[dict] = []
    last_capture_square: int | None = None
    plies = 0
    for move in game.mainline_moves():
        piece = board.piece_at(move.from_square)
        if piece is None:
            break
        plies += 1
        phase = phase_for_move(board.fullmove_number)
        piece_name = PIECE_NAMES.get(piece.piece_type, "piece")
        side = "white" if piece.color == chess.WHITE else "black"
        score = game_result_score(result, piece.color) if result in {"1-0", "0-1", "1/2-1/2"} else 0.5
        outcome = side_outcome_label(result, piece.color) if result in {"1-0", "0-1", "1/2-1/2"} else "unknown"
        events = abstract_move_events(board, move, piece, last_capture_square)
        was_capture = board.is_capture(move)
        capture_square = move.to_square if was_capture else None
        board.push(move)
        if piece.piece_type in {chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN} and board.is_attacked_by(not piece.color, move.to_square):
            events.append("valuable_piece_lands_on_contested_square")
        if board.is_checkmate():
            events.append("checkmate")
        if not events:
            events.append("quiet_piece_improvement")
        move_row = {
            "ply": plies,
            "phase": phase,
            "side": side,
            "piece": piece_name,
            "events": events[:8],
            "side_outcome": outcome,
        }
        if plies <= max_ply_per_game:
            trace.append(move_row)
        for event in events:
            event_rows.append({"phase": phase, "piece": piece_name, "event": event, "score": score, "side_outcome": outcome})
        last_capture_square = capture_square
    return {
        "source_file": source_file,
        "game_index": int(game_index),
        "result": result_label(result),
        "plies_scanned": plies,
        "plies_recorded": len(trace),
        "trace_limited": plies > len(trace),
        "move_trace": trace,
        "_event_rows": event_rows,
    }


def new_move_evidence_batch(state: dict, requested: int, artifact_path: Path, summary_path: Path) -> dict:
    failed_attempt = latest_failed_attempt_for_learning(state)
    return {
        "schema": "master-wisdom-move-evidence-v1",
        "generated_at": utc_stamp(),
        "trigger": "failed_gate" if failed_attempt else "scheduled_batch",
        "after_failed_attempt": failed_attempt,
        "requested_games": int(requested),
        "games_scanned": 0,
        "plies_scanned": 0,
        "plies_recorded": 0,
        "trace_limited_games": 0,
        "artifact": str(artifact_path),
        "summary_artifact": str(summary_path),
        "_patterns": {},
        "_prompt_traces": [],
    }


def update_move_evidence_batch(batch: dict, game_evidence: dict, prompt_trace_limit: int) -> None:
    batch["games_scanned"] = int(batch.get("games_scanned", 0) or 0) + 1
    batch["plies_scanned"] = int(batch.get("plies_scanned", 0) or 0) + int(game_evidence.get("plies_scanned", 0) or 0)
    batch["plies_recorded"] = int(batch.get("plies_recorded", 0) or 0) + int(game_evidence.get("plies_recorded", 0) or 0)
    if game_evidence.get("trace_limited"):
        batch["trace_limited_games"] = int(batch.get("trace_limited_games", 0) or 0) + 1
    if len(batch.setdefault("_prompt_traces", [])) < prompt_trace_limit:
        compact = {key: value for key, value in game_evidence.items() if not key.startswith("_")}
        batch["_prompt_traces"].append(compact)
    patterns = batch.setdefault("_patterns", {})
    for row in game_evidence.get("_event_rows", []):
        key = f"{row.get('phase')}|{row.get('piece')}|{row.get('event')}"
        item = patterns.setdefault(
            key,
            {
                "phase": row.get("phase", ""),
                "piece": row.get("piece", ""),
                "event": row.get("event", ""),
                "occurrences": 0,
                "score_sum": 0.0,
                "wins": 0,
                "draws": 0,
                "losses": 0,
            },
        )
        item["occurrences"] += 1
        item["score_sum"] += float(row.get("score", 0.5) or 0.5)
        outcome = row.get("side_outcome")
        if outcome == "win":
            item["wins"] += 1
        elif outcome == "loss":
            item["losses"] += 1
        elif outcome == "draw":
            item["draws"] += 1


def finalize_move_evidence_batch(batch: dict, top_pattern_limit: int, include_traces: bool) -> dict:
    rows = []
    for item in batch.get("_patterns", {}).values():
        occurrences = max(1, int(item.get("occurrences", 0) or 0))
        average = float(item.get("score_sum", 0.0) or 0.0) / occurrences
        rows.append(
            {
                "phase": item.get("phase", ""),
                "piece": item.get("piece", ""),
                "event": item.get("event", ""),
                "occurrences": occurrences,
                "average_side_score": round(average, 3),
                "wins": int(item.get("wins", 0) or 0),
                "draws": int(item.get("draws", 0) or 0),
                "losses": int(item.get("losses", 0) or 0),
            }
        )
    rows.sort(key=lambda row: (row["occurrences"], abs(float(row["average_side_score"]) - 0.5)), reverse=True)
    compact = {key: value for key, value in batch.items() if not key.startswith("_")}
    compact["top_move_patterns"] = rows[:top_pattern_limit]
    if include_traces:
        compact["representative_move_traces"] = list(batch.get("_prompt_traces", []))
    return compact


def move_evidence_for_prompt(state: dict) -> dict:
    evidence = state.get("last_move_evidence")
    return evidence if isinstance(evidence, dict) else {}


def infer_exhausted_prefix_totals(state: dict, manifest: dict) -> None:
    processed_files = state.setdefault("processed_files", {})
    processed_totals = state.setdefault("processed_file_totals", {})
    contiguous: list[tuple[str, int]] = []
    for entry in selected_manifest_files(manifest, latest=False):
        filename = str(entry.get("filename", ""))
        processed = int(processed_files.get(filename, 0) or 0)
        if processed <= 0:
            break
        contiguous.append((filename, processed))
    for filename, processed in contiguous[:-1]:
        processed_totals.setdefault(filename, processed)


def learn_batch(
    paths: MasterWisdomPaths | None = None,
    batch_size: int | None = None,
    model_synthesis: bool | None = None,
    synthesis_model: str | None = None,
    synthesis_effort: str | None = None,
    synthesis_timeout: int | None = None,
) -> dict:
    paths = paths or default_paths()
    manifest = load_manifest(paths, refresh=False)
    state = load_state(paths)
    infer_exhausted_prefix_totals(state, manifest)
    target = int(batch_size or state.get("batch_size") or 500)
    processed = 0
    touched_files: list[str] = []
    skipped_files: list[str] = []
    downloaded_files: list[dict] = []
    download_errors: list[dict] = []
    processed_files = state.setdefault("processed_files", {})
    processed_offsets = state.setdefault("processed_file_offsets", {})
    processed_offset_counts = state.setdefault("processed_file_offset_counts", {})
    processed_totals = state.setdefault("processed_file_totals", {})
    move_config = configured_move_evidence(paths)
    evidence_dir = paths.out_dir / "move-evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    evidence_stamp = time.strftime("%Y%m%d-%H%M%S")
    evidence_path = evidence_dir / f"master-wisdom-move-evidence-{evidence_stamp}.jsonl"
    evidence_summary_path = evidence_dir / f"master-wisdom-move-evidence-{evidence_stamp}.summary.json"
    move_batch = new_move_evidence_batch(state, target, evidence_path, evidence_summary_path)
    evidence_handle = None
    for entry in selected_manifest_files(manifest, latest=False):
        pgn_path = entry_download_path(entry, paths)
        if not pgn_path.exists():
            try:
                downloaded = download_file(entry, paths)
                downloaded_files.append(downloaded)
            except Exception as exc:
                download_errors.append({"filename": str(entry.get("filename", "")), "error": str(exc)})
                continue
            pgn_path = entry_download_path(entry, paths)
            if not pgn_path.exists():
                continue
        filename = str(entry["filename"])
        already_processed = int(processed_files.get(filename, 0) or 0)
        known_total = int(processed_totals.get(filename, 0) or 0)
        if known_total and already_processed >= known_total:
            skipped_files.append(filename)
            continue
        start_offset = int(processed_offsets.get(filename, 0) or 0) if already_processed else 0
        offset_count = int(processed_offset_counts.get(filename, 0) or 0) if start_offset else 0
        use_offset = start_offset > 0 and 0 < offset_count <= already_processed
        if not use_offset:
            start_offset = 0
            offset_count = 0
        seen_in_file = offset_count if use_offset else 0
        newly_processed_in_file = 0
        last_offset = start_offset
        exhausted_file = True
        for game, _before_offset, after_offset in iter_games_with_offsets(pgn_path, start_offset=start_offset):
            seen_in_file += 1
            if not use_offset and seen_in_file <= already_processed:
                if after_offset is not None:
                    last_offset = int(after_offset)
                    processed_offsets[filename] = last_offset
                    processed_offset_counts[filename] = seen_in_file
                    if seen_in_file % OFFSET_CHECKPOINT_INTERVAL == 0 or seen_in_file == already_processed:
                        save_state(state, paths)
                continue
            if use_offset and seen_in_file <= already_processed:
                if after_offset is not None:
                    last_offset = int(after_offset)
                    processed_offsets[filename] = last_offset
                    processed_offset_counts[filename] = seen_in_file
                    if seen_in_file % OFFSET_CHECKPOINT_INTERVAL == 0 or seen_in_file == already_processed:
                        save_state(state, paths)
                continue
            analyze_game_into_state(state, game)
            game_evidence = abstract_game_move_evidence(game, filename, seen_in_file, int(move_config["max_ply_per_game"]))
            update_move_evidence_batch(move_batch, game_evidence, int(move_config["prompt_trace_limit"]))
            if evidence_handle is None:
                evidence_handle = evidence_path.open("w", encoding="utf-8")
            public_game_evidence = {key: value for key, value in game_evidence.items() if not key.startswith("_")}
            evidence_handle.write(json.dumps(public_game_evidence, sort_keys=True) + "\n")
            processed += 1
            newly_processed_in_file += 1
            if after_offset is not None:
                last_offset = int(after_offset)
                processed_offsets[filename] = last_offset
                processed_offset_counts[filename] = seen_in_file
            if processed >= target:
                exhausted_file = False
                break
        if newly_processed_in_file:
            processed_files[filename] = already_processed + newly_processed_in_file
            if last_offset > 0:
                processed_offsets[filename] = last_offset
                processed_offset_counts[filename] = processed_files[filename]
            touched_files.append(filename)
        if exhausted_file:
            processed_count = int(processed_files.get(filename, already_processed) or 0)
            if seen_in_file >= processed_count:
                processed_totals[filename] = seen_in_file
        if processed >= target:
            break

    if evidence_handle is not None:
        evidence_handle.close()
    if processed:
        move_summary = finalize_move_evidence_batch(move_batch, int(move_config["top_pattern_limit"]), include_traces=False)
        prompt_move_evidence = finalize_move_evidence_batch(move_batch, int(move_config["top_pattern_limit"]), include_traces=True)
        evidence_summary_path.write_text(json.dumps(move_summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        state["last_move_evidence"] = prompt_move_evidence
        state.setdefault("recent_move_batches", []).append(move_summary)
        state["recent_move_batches"] = state["recent_move_batches"][-12:]
    else:
        for artifact in (evidence_path, evidence_summary_path):
            try:
                if artifact.exists():
                    artifact.unlink()
            except OSError:
                pass

    state["processed_games"] = int(state.get("processed_games", 0) or 0) + processed
    batch_row = {
        "generated_at": utc_stamp(),
        "requested": target,
        "processed": processed,
        "files": touched_files,
        "downloaded": downloaded_files,
        "download_errors": download_errors,
        "skipped_files": skipped_files,
        "batch_size": int(state.get("batch_size") or target),
        "trigger": move_batch.get("trigger"),
        "after_failed_attempt": move_batch.get("after_failed_attempt"),
        "move_evidence": finalize_move_evidence_batch(move_batch, int(move_config["top_pattern_limit"]), include_traces=False) if processed else {},
    }
    state.setdefault("recent_batches", []).append(batch_row)
    state["recent_batches"] = state["recent_batches"][-20:]
    save_state(state, paths)
    write_wisdom_outputs(state, paths)
    synthesis = synthesize_master_wisdom_outputs(
        state,
        paths,
        model=synthesis_model,
        effort=synthesis_effort,
        timeout=synthesis_timeout,
        enabled=model_synthesis,
    )
    state["wisdom_synthesis"] = synthesis
    save_state(state, paths)
    return {
        "ok": True,
        "processed": processed,
        "downloaded": downloaded_files,
        "download_errors": download_errors,
        "skipped_files": skipped_files,
        "synthesis": synthesis,
        "state_path": str(paths.state_path),
        "wisdom": str(paths.wisdom_md_path),
        "skill": str(paths.skill_path),
    }


def pattern_rows(state: dict, good: bool, limit: int = 8) -> list[dict]:
    rows = []
    for key, value in (state.get("patterns") or {}).items():
        count = int(value.get("count", 0) or 0)
        if count <= 0:
            continue
        average = float(value.get("score_sum", 0.0)) / count
        is_forced_caution = key in CAUTION_PATTERN_KEYS
        if good and is_forced_caution:
            continue
        if not good and not is_forced_caution and average > 0.48:
            continue
        if good and average < 0.52:
            continue
        rows.append(
            {
                "key": key,
                "label": PATTERN_LABELS.get(key) or value.get("label") or key,
                "count": count,
                "score": average,
                "wins": value.get("wins", 0),
                "draws": value.get("draws", 0),
                "losses": value.get("losses", 0),
                "confidence": abs(average - 0.5) * math.sqrt(count),
            }
        )
    rows.sort(key=lambda item: (item["confidence"], item["count"]), reverse=True)
    return rows[:limit]


def configured_model_synthesis(paths: MasterWisdomPaths) -> dict:
    config = master_wisdom_config(paths.root)
    return {
        "enabled": bool(config.get("modelSynthesis", True)),
        "model": str(config.get("synthesisModel") or "gpt-5.5"),
        "effort": str(config.get("synthesisEffort") or "xhigh"),
        "timeout": int(config.get("synthesisTimeoutSeconds") or 420),
    }


def parse_json_object(text: str) -> dict:
    text = (text or "").strip()
    if not text:
        raise ValueError("empty model response")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S | re.I)
    if fenced:
        return json.loads(fenced.group(1))
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return json.loads(text[start : end + 1])
    raise ValueError("no JSON object found in model response")


def read_existing_authored_payload(paths: MasterWisdomPaths) -> dict:
    if not paths.wisdom_json_path.exists():
        return {}
    try:
        payload = json.loads(paths.wisdom_json_path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def compact_pattern_rows(rows: list[dict]) -> list[dict]:
    compact = []
    for row in rows:
        compact.append(
            {
                "label": row.get("label", ""),
                "average_score": round(float(row.get("score", 0.0)), 3),
                "sample_size": int(row.get("count", 0) or 0),
            }
        )
    return compact


def recent_attempt_rows(state: dict, limit: int = 8) -> list[dict]:
    attempts = list(state.get("attempts") or [])[-limit:]
    rows = []
    for attempt in attempts:
        rows.append(
            {
                "depth": int(attempt.get("depth", 0) or 0),
                "batch_size": int(attempt.get("batch_size", 0) or 0),
                "score": round(float(attempt.get("score", 0.0) or 0.0), 3),
                "wins": int(attempt.get("wins", 0) or 0),
                "draws": int(attempt.get("draws", 0) or 0),
                "losses": int(attempt.get("losses", 0) or 0),
                "games": int(attempt.get("games", 0) or 0),
                "early_stopped": bool(attempt.get("early_stopped")),
                "stop_reason": str(attempt.get("stop_reason") or ""),
            }
        )
    return rows


def master_wisdom_synthesis_prompt(state: dict, paths: MasterWisdomPaths) -> str:
    existing = read_existing_authored_payload(paths)
    payload = {
        "task": (
            "Update Codex-chess-learner master-game wisdom from Lichess Elite batch evidence "
            "and recent Stockfish ladder results. The latest batch was scanned game-by-game and move-by-move "
            "into abstract phase, piece, and tactical events. Produce compact learner-facing chess principles."
        ),
        "constraints": [
            "Return JSON only.",
            "Principles must be general and reusable across positions.",
            "Do not include opening-family win rates, side scores, sample counts, or evidence counts in the output text.",
            "Do not include exact moves, SAN/PGN fragments, UCI coordinates, FEN-to-move rules, opening-book move lists, Stockfish lines, tablebase facts, or Zero data.",
            "Do not reproduce individual game traces or move numbers in the learner-facing output.",
            "Do not tell the learner to imitate human games; convert evidence into first-principles chess advice.",
            "Keep the strongest anti-blunder rules near the top so capped prompts still see them.",
        ],
        "existing_priority_principles": existing.get("priority_principles", [])[:16],
        "existing_principles": existing.get("principles", [])[:48],
        "batch_pattern_evidence": {
            "useful": compact_pattern_rows(pattern_rows(state, good=True, limit=10)),
            "caution": compact_pattern_rows(pattern_rows(state, good=False, limit=10)),
        },
        "recent_ladder_attempts": recent_attempt_rows(state),
        "latest_batch_move_by_move_evidence": move_evidence_for_prompt(state),
        "current_gate": {
            "depth": int(state.get("current_depth", 1) or 1),
            "target_depth": int(state.get("target_depth", 8) or 8),
            "batch_size": int(state.get("batch_size", 500) or 500),
            "pass_score": float(state.get("pass_score", 0.8) or 0.8),
        },
        "output_schema": {
            "priority_principles": ["short highest-priority learner rules"],
            "principles": ["general reusable chess principles"],
            "skill_principles": ["short subset suitable for an Agent Skill"],
        },
    }
    return json.dumps(payload, indent=2)


def clean_principle(value: object) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip(" -\t\r\n")
    text = text.rstrip(".")
    return text[:240]


def is_forbidden_learner_text(text: str) -> bool:
    lowered = text.lower()
    forbidden_terms = ["stockfish line", "tablebase", "win rate", "evidence count", "sample count", "zero training"]
    if any(term in lowered for term in forbidden_terms):
        return True
    return bool(re.search(r"\b[a-h][1-8][a-h][1-8][qrbn]?\b", lowered))


def normalized_principles(values: object, limit: int) -> list[str]:
    if not isinstance(values, list):
        return []
    result = []
    seen = set()
    for value in values:
        text = clean_principle(value)
        key = text.lower()
        if not text or key in seen or is_forbidden_learner_text(text):
            continue
        result.append(text)
        seen.add(key)
        if len(result) >= limit:
            break
    return result


def write_authored_synthesis_outputs(state: dict, paths: MasterWisdomPaths, synthesis: dict, metadata: dict) -> None:
    priority = normalized_principles(synthesis.get("priority_principles"), 10)
    principles = normalized_principles(synthesis.get("principles"), 48)
    skill_principles = normalized_principles(synthesis.get("skill_principles"), 18) or priority[:8] + principles[:10]
    if not priority and principles:
        priority = principles[:8]
    if not principles:
        raise ValueError("model returned no usable principles")

    updated_at = utc_stamp()
    paths.learner_knowledgebase_dir.mkdir(parents=True, exist_ok=True)
    paths.skill_dir.mkdir(parents=True, exist_ok=True)
    json_payload = {
        "schema": AUTHORED_JSON_SCHEMA,
        "authored_by": "Codex batch synthesis",
        "source": "Lichess Elite master-game batches plus the current Stockfish depth ladder",
        "updated_at": updated_at,
        "use": "Generalized learner-only chess principles; not an opening book, FEN map, statistics table, Stockfish line, or Zero signal.",
        "synthesis": metadata,
        "priority_principles": priority,
        "principles": principles,
    }
    paths.wisdom_json_path.write_text(json.dumps(json_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    md_lines = [
        "# Master Game Wisdom",
        "",
        "Authored-by: Codex batch synthesis",
        f"Synthesis model: {metadata.get('model', '')}",
        f"Synthesis effort: {metadata.get('effort', '')}",
        f"Updated: {updated_at}",
        "Source: Lichess Elite master-game batches plus the current Stockfish depth ladder",
        f"Current Stockfish depth gate: {state.get('current_depth', 1)} / {state.get('target_depth', 8)}",
        "",
        "Use this as generalized guidance for Codex-chess-learner only. This is not an opening book, a statistics table, a Stockfish line, a tablebase, a FEN-to-move map, or a Zero training signal.",
        "",
        "## Live Ladder Corrections",
        "",
    ]
    md_lines.extend(f"- {text}." for text in priority)
    md_lines.extend(["", "## How To Think", ""])
    md_lines.extend(f"- {text}." for text in principles)
    md_lines.extend(
        [
            "",
            "## Move-Selection Checklist",
            "",
            "For every candidate legal move:",
            "",
            "- Is my king safe after the opponent's best check?",
            "- Does the move hang my queen, rook, a minor piece, or a critical defender?",
            "- What forcing reply does the opponent have?",
            "- Does the move improve safety, activity, coordination, or conversion?",
            "",
            "Choose the final UCI move only from legal_moves after eliminating moves that hang the king, queen, rook, or a tactically loose piece.",
            "",
        ]
    )
    paths.wisdom_md_path.write_text("\n".join(md_lines), encoding="utf-8")

    skill_lines = [
        "---",
        "name: master-game-wisdom",
        "description: Learner-local Codex-authored chess principles synthesized from Lichess Elite master-game batches and Stockfish ladder attempts.",
        "---",
        "",
        "# Master Game Wisdom",
        "",
        "Use this skill only for Codex-chess-learner prompt move selection.",
        "",
        "Priority anti-blunder rules:",
    ]
    skill_lines.extend(f"- {text}." for text in skill_principles[:8])
    skill_lines.extend(
        [
            "",
            "Rules:",
            "- Use authored principles in the current position; do not treat this as an opening book.",
            "- Do not use opening-family win rates, exact FEN-to-move rules, tablebase facts, Stockfish/Lc0/Maia PVs, or Zero training labels.",
            "- If this skill conflicts with legal_moves, material_safety, king safety, or clock pressure, obey the current position.",
            "",
            "Current principles:",
        ]
    )
    skill_lines.extend(f"- {text}." for text in skill_principles)
    skill_lines.extend(["", "Move-selection reminder:", "- Choose only from legal_moves.", "- Eliminate moves that hang the king, queen, rook, or a tactically loose piece.", "- Then choose the move that best improves safety, activity, and conversion.", "", f"Detailed source: {paths.wisdom_md_path}", ""])
    paths.skill_path.write_text("\n".join(skill_lines), encoding="utf-8")


def synthesize_master_wisdom_outputs(
    state: dict,
    paths: MasterWisdomPaths | None = None,
    model: str | None = None,
    effort: str | None = None,
    timeout: int | None = None,
    enabled: bool | None = None,
) -> dict:
    paths = paths or default_paths()
    settings = configured_model_synthesis(paths)
    enabled = settings["enabled"] if enabled is None else bool(enabled)
    model = model or settings["model"]
    effort = effort or settings["effort"]
    timeout = int(timeout or settings["timeout"])
    if not enabled:
        return {"status": "disabled", "message": "model synthesis disabled"}

    query_script = Path.home() / ".codex" / "skills" / "codex-app-server-query" / "scripts" / "query_app_server.py"
    if not query_script.exists():
        return {"status": "unavailable", "message": f"Codex app-server query script not found: {query_script}", "model": model, "effort": effort}

    temp_path = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as handle:
            handle.write(master_wisdom_synthesis_prompt(state, paths))
            temp_path = Path(handle.name)
        completed = subprocess.run(
            [
                sys.executable,
                str(query_script),
                "--model",
                model,
                "--effort",
                effort,
                "--cwd",
                str(paths.root),
                "--timeout",
                str(timeout),
                "--prompt-file",
                str(temp_path),
            ],
            cwd=str(paths.root),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout + 30,
            check=False,
        )
        if completed.returncode != 0:
            return {"status": "failed", "message": completed.stderr.strip() or completed.stdout.strip()[:500], "model": model, "effort": effort}
        outer = json.loads(completed.stdout)
        synthesis = parse_json_object(str(outer.get("text") or ""))
        metadata = {
            "status": "ok",
            "model": model,
            "effort": effort,
            "thread_id": outer.get("thread_id", ""),
            "turn_id": outer.get("turn_id", ""),
            "generated_at": utc_stamp(),
        }
        write_authored_synthesis_outputs(state, paths, synthesis, metadata)
        metadata["message"] = "updated learner master wisdom from model synthesis"
        return metadata
    except Exception as exc:
        return {"status": "failed", "message": f"{type(exc).__name__}: {exc}", "model": model, "effort": effort}
    finally:
        if temp_path:
            try:
                temp_path.unlink()
            except OSError:
                pass


def write_wisdom_outputs(state: dict, paths: MasterWisdomPaths | None = None) -> None:
    paths = paths or default_paths()
    paths.learner_knowledgebase_dir.mkdir(parents=True, exist_ok=True)
    paths.skill_dir.mkdir(parents=True, exist_ok=True)
    good = pattern_rows(state, good=True, limit=10)
    avoid = pattern_rows(state, good=False, limit=8)
    summary = {
        "schema": "master-wisdom-summary-v1",
        "updated_at": utc_stamp(),
        "source": state.get("source"),
        "source_url": state.get("source_url"),
        "processed_games": state.get("processed_games", 0),
        "batch_size": state.get("batch_size"),
        "current_depth": state.get("current_depth"),
        "target_depth": state.get("target_depth"),
        "pass_score": state.get("pass_score"),
        "useful_principles": [row["label"] for row in good],
        "caution_principles": [row["label"] for row in avoid],
    }
    if not is_authored_wisdom_json(paths.wisdom_json_path):
        paths.wisdom_json_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    lines = [
        "# Master Game Wisdom",
        "",
        f"Updated: {summary['updated_at']}",
        f"Source: {SOURCE_NAME} ({state.get('source_url', SOURCE_INDEX_URL)})",
        f"Processed games: {state.get('processed_games', 0)}",
        f"Current Stockfish depth gate: {state.get('current_depth', 1)} / {state.get('target_depth', 8)}",
        f"Batch size: {state.get('batch_size', 500)} games",
        "",
        "Use this as generalized guidance for Codex-chess-learner only. This file stores principles, not opening statistics, exact move rules, Stockfish lines, tablebase facts, or Zero training signals.",
        "",
        "## Useful Principles",
    ]
    if good:
        for row in good:
            lines.append(f"- {row['label']}.")
    else:
        lines.append("- No useful principle has stabilized yet.")
    lines.extend(["", "## Caution Principles"])
    if avoid:
        for row in avoid:
            lines.append(f"- {row['label']}.")
    else:
        lines.append("- No caution principle has stabilized yet.")
    lines.extend(
        [
            "",
            "## Move-Selection Reminder",
            "",
            "First check legality, king safety, immediate material swings, and opponent forcing replies. Then apply the strongest relevant master-game concept above. Keep the final UCI copied exactly from legal_moves.",
            "",
        ]
    )
    if not is_authored_wisdom_md(paths.wisdom_md_path):
        paths.wisdom_md_path.write_text("\n".join(lines), encoding="utf-8")

    skill_lines = [
        "---",
        "name: master-game-wisdom",
        "description: Learner-local generalized chess wisdom extracted from Lichess Elite master games without exact move memorization.",
        "---",
        "",
        "# Master Game Wisdom",
        "",
        "Use this skill only for Codex-chess-learner prompt move selection.",
        "",
        "Rules:",
        "- Use generalized concepts only: king safety, development, center control, conversion, tactics, and coordination.",
        "- Do not use opening-family win rates, exact FEN-to-move rules, opening-book lines, tablebase facts, Stockfish/Lc0/Maia PVs, or Zero training labels.",
        "- If this skill conflicts with current legal moves, material_safety, or clock pressure, obey the current position.",
        "",
        "Current strongest concepts:",
    ]
    for row in good[:6]:
        skill_lines.append(f"- {row['label']}.")
    if not good:
        skill_lines.append("- No extracted concept has stabilized yet; fall back to normal current-position reasoning.")
    skill_lines.extend(["", f"Detailed source: {paths.wisdom_md_path}", ""])
    if not is_authored_skill(paths.skill_path):
        paths.skill_path.write_text("\n".join(skill_lines), encoding="utf-8")


def load_stockfish_path(config_path: Path = ENGINE_CONFIG_PATH) -> Path:
    engines = json.loads(config_path.read_text(encoding="utf-8"))
    for engine in engines if isinstance(engines, list) else []:
        name = str(engine.get("name", "")).lower()
        path = Path(str(engine.get("path", "")))
        if "stockfish" in name and path.exists():
            return path
    raise RuntimeError(f"No Stockfish executable found in {config_path}")


def configured_play_model(paths: MasterWisdomPaths) -> dict:
    master_config = master_wisdom_config(paths.root)
    return {
        "model": str(master_config.get("playModel") or repo_config_value(paths.root, "codex.model", "gpt-5.3-codex")),
        "effort": str(master_config.get("playEffort") or repo_config_value(paths.root, "codex.effort", "high")),
        "preflight": bool(master_config.get("preflightPlayModel", True)),
        "timeout": int(master_config.get("playPreflightTimeoutSeconds") or repo_config_value(paths.root, "codex.preflightTimeoutSeconds", 45)),
    }


def preflight_learner_play_model(paths: MasterWisdomPaths, model: str, effort: str, timeout_seconds: int) -> dict:
    script = paths.root / "tools" / "check_codex_model_available.py"
    if not script.exists():
        return {"ok": False, "message": f"missing model preflight script: {script}", "stdout": "", "stderr": ""}
    command = [
        sys.executable,
        str(script),
        "--model",
        str(model),
        "--effort",
        str(effort),
        "--timeout",
        str(max(1, int(timeout_seconds))),
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=str(paths.root),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=max(5, int(timeout_seconds) + 20),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "message": f"model preflight timed out after {exc.timeout}s",
            "stdout": str(exc.stdout or ""),
            "stderr": str(exc.stderr or ""),
        }
    stdout = completed.stdout.strip()
    stderr = completed.stderr.strip()
    return {
        "ok": completed.returncode == 0,
        "message": stdout if completed.returncode == 0 else (stderr or stdout or f"preflight exited {completed.returncode}"),
        "stdout": stdout,
        "stderr": stderr,
        "returncode": completed.returncode,
    }


def record_training_blocker(state: dict, reason: str, message: str, **details) -> dict:
    blocker = {
        "generated_at": utc_stamp(),
        "reason": reason,
        "message": message,
        **details,
    }
    state["last_training_blocker"] = blocker
    return blocker


class UciPlayer:
    def __init__(self, name: str, command: Path, options: dict[str, str] | None = None, env: dict[str, str] | None = None):
        self.name = name
        self.command_path = command
        proc_env = os.environ.copy()
        if env:
            proc_env.update({str(key): str(value) for key, value in env.items()})
        self.lines: queue.Queue[str] = queue.Queue()
        self.proc = subprocess.Popen(
            [str(command)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=proc_env,
        )
        self.reader = threading.Thread(target=self._reader, daemon=True)
        self.reader.start()
        self.send("uci")
        self.read_until("uciok", timeout_seconds=30)
        for key, value in (options or {}).items():
            self.send(f"setoption name {key} value {value}")
        self.send("isready")
        self.read_until("readyok", timeout_seconds=30)

    def _reader(self) -> None:
        if self.proc.stdout is None:
            return
        for line in self.proc.stdout:
            self.lines.put(line.strip())

    def send(self, line: str) -> None:
        if self.proc.stdin is None:
            raise RuntimeError(f"{self.name} stdin is closed")
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()

    def read_until(self, marker: str, timeout_seconds: int = 120) -> list[str]:
        deadline = time.time() + max(1, int(timeout_seconds))
        lines = []
        while True:
            if self.proc.poll() is not None and self.lines.empty():
                raise RuntimeError(f"{self.name} exited unexpectedly")
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError(f"{self.name} timed out waiting for {marker}; saw {lines[-8:]}")
            try:
                line = self.lines.get(timeout=min(0.25, remaining))
            except queue.Empty:
                continue
            line = line.strip()
            lines.append(line)
            if line == marker or line.startswith(marker + " ") or marker in line:
                return lines

    def new_game(self) -> None:
        self.send("ucinewgame")
        self.send("isready")
        self.read_until("readyok", timeout_seconds=30)

    def bestmove(self, board: chess.Board, go_command: str, timeout_seconds: int = 120) -> chess.Move | None:
        self.send("position fen " + board.fen())
        self.send(go_command)
        lines = self.read_until("bestmove", timeout_seconds=timeout_seconds)
        for line in reversed(lines):
            if not line.startswith("bestmove "):
                continue
            value = line.split()[1]
            if value == "0000":
                return None
            try:
                move = chess.Move.from_uci(value)
            except ValueError:
                return None
            if move in board.legal_moves:
                return move
            return None
        return None

    def close(self) -> None:
        if self.proc.poll() is None:
            try:
                self.send("quit")
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()


def record_depth_attempt(
    state: dict,
    depth: int,
    games: int,
    learner_points: float,
    wins: int,
    draws: int,
    losses: int,
    pgn_path: str = "",
    simulated: bool = False,
    total_games: int | None = None,
    early_stopped: bool = False,
    stop_reason: str = "",
) -> dict:
    games = max(1, int(games))
    total_games = max(games, int(total_games or games))
    pass_score = float(state.get("pass_score", 0.8))
    required_points = pass_score * total_games
    score = learner_points / total_games
    passed = learner_points >= required_points
    row = {
        "generated_at": utc_stamp(),
        "depth": int(depth),
        "games": games,
        "total_games": total_games,
        "learner_points": learner_points,
        "score": score,
        "wins": wins,
        "draws": draws,
        "losses": losses,
        "passed": passed,
        "pgn_path": pgn_path,
        "batch_size": int(state.get("batch_size", 500) or 500),
        "simulated": simulated,
        "early_stopped": bool(early_stopped),
        "stop_reason": stop_reason,
    }
    state.setdefault("attempts", []).append(row)
    state["attempts"] = state["attempts"][-200:]
    if passed:
        if int(depth) >= int(state.get("target_depth", 8)):
            state["completed"] = True
        else:
            state["current_depth"] = int(depth) + 1
    else:
        current_batch = int(state.get("batch_size", 500) or 500)
        max_batch = int(state.get("max_batch_size", 16000) or 16000)
        state["batch_size"] = min(max_batch, max(current_batch + int(state.get("min_batch_size", 500) or 500), current_batch * 2))
    return row


def attempt_can_still_pass(learner_points: float, completed_games: int, total_games: int, pass_score: float) -> bool:
    required_points = float(pass_score) * max(1, int(total_games))
    remaining_games = max(0, int(total_games) - int(completed_games))
    return float(learner_points) + remaining_games >= required_points


def save_leaderboard(state: dict, paths: MasterWisdomPaths | None = None) -> list[dict]:
    paths = paths or default_paths()
    rows = list(state.get("attempts") or [])
    rows.sort(key=lambda row: (int(row.get("depth", 0)), float(row.get("score", 0)), str(row.get("generated_at", ""))), reverse=True)
    payload = {"schema": "master-wisdom-leaderboard-v1", "updated_at": utc_stamp(), "rows": rows[:100]}
    paths.out_dir.mkdir(parents=True, exist_ok=True)
    paths.leaderboard_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload["rows"]


def live_status_path_for(pgn_path: Path) -> Path:
    return pgn_path.with_suffix(".status.json")


def write_live_game_state(
    paths: MasterWisdomPaths,
    game: chess.pgn.Game,
    completed_game_texts: list[str],
    game_number: int,
    total_games: int,
    completed: bool,
) -> None:
    paths.live_pgn_path.parent.mkdir(parents=True, exist_ok=True)
    live_games = [text for text in completed_game_texts if text.strip()] + [str(game)]
    paths.live_pgn_path.write_text("\n\n".join(live_games) + "\n\n", encoding="utf-8")
    status_payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "generated_at_epoch": time.time(),
        "output_pgn": str(paths.live_pgn_path),
        "control_pgn": str(paths.live_pgn_path),
        "locked_game": int(game_number),
        "games": [
            {
                "game": int(game_number),
                "total": int(total_games),
                "white": game.headers.get("White", "White"),
                "black": game.headers.get("Black", "Black"),
                "result": game.headers.get("Result", "*"),
                "reason": game.headers.get("Termination", "") if completed else "",
                "finished": completed,
            }
        ],
    }
    live_status_path_for(paths.live_pgn_path).write_text(json.dumps(status_payload, indent=2), encoding="utf-8")


def evaluate_depth(
    paths: MasterWisdomPaths | None = None,
    depth: int | None = None,
    games: int | None = None,
    max_plies: int = 160,
    dry_run: bool = False,
    simulated_score: float = 0.0,
) -> dict:
    paths = paths or default_paths()
    state = load_state(paths)
    depth = int(depth or state.get("current_depth", 1))
    games = int(games or state.get("games_per_attempt", 10))
    if dry_run:
        learner_points = max(0.0, min(float(games), float(simulated_score) * games))
        wins = int(learner_points)
        draws = 1 if learner_points - wins >= 0.5 else 0
        losses = max(0, games - wins - draws)
        row = record_depth_attempt(state, depth, games, learner_points, wins, draws, losses, simulated=True)
        save_state(state, paths)
        save_leaderboard(state, paths)
        write_wisdom_outputs(state, paths)
        return {"ok": True, "attempt": row, "state_path": str(paths.state_path), "leaderboard": str(paths.leaderboard_path)}

    learner_cmd = paths.root / "engines" / "codex-chess-learner" / "codex-chess-learner.cmd"
    play_model = configured_play_model(paths)
    if play_model["preflight"]:
        preflight = preflight_learner_play_model(paths, play_model["model"], play_model["effort"], play_model["timeout"])
        if not preflight.get("ok"):
            blocker = record_training_blocker(
                state,
                "learner_model_preflight_failed",
                str(preflight.get("message") or "Codex learner play model preflight failed"),
                model=play_model["model"],
                effort=play_model["effort"],
                preflight=preflight,
            )
            save_state(state, paths)
            save_leaderboard(state, paths)
            write_wisdom_outputs(state, paths)
            return {
                "ok": False,
                "reason": blocker["reason"],
                "blocker": blocker,
                "state_path": str(paths.state_path),
                "leaderboard": str(paths.leaderboard_path),
            }
    state.pop("last_training_blocker", None)
    stockfish_cmd = load_stockfish_path()
    paths.matches_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    pgn_path = paths.matches_dir / f"master-wisdom-depth-{depth}-{stamp}.pgn"
    games_text: list[str] = []
    wins = draws = losses = 0
    learner_points = 0.0
    completed_games = 0
    early_stopped = False
    stop_reason = ""
    pass_score = float(state.get("pass_score", 0.8))
    learner_env = {"CODEX_CHESS_MODEL": play_model["model"], "CODEX_CHESS_EFFORT": play_model["effort"]}
    learner = UciPlayer(
        "Codex-chess-learner",
        learner_cmd,
        {"UseMemory": "true", "UseSkills": "true", "LearningMode": "true"},
        env=learner_env,
    )
    stockfish = UciPlayer(f"Stockfish depth {depth}", stockfish_cmd, {"Threads": "1", "Hash": "32"})
    try:
        for index in range(games):
            learner_white = index % 2 == 0
            board = chess.Board()
            game = chess.pgn.Game()
            game.headers["Event"] = f"Master Wisdom depth {depth}"
            game.headers["Site"] = str(paths.root)
            game.headers["Date"] = time.strftime("%Y.%m.%d")
            game.headers["Round"] = str(index + 1)
            game.headers["White"] = "Codex-chess-learner" if learner_white else f"Stockfish depth {depth}"
            game.headers["Black"] = f"Stockfish depth {depth}" if learner_white else "Codex-chess-learner"
            game.headers["BatchSize"] = str(state.get("batch_size", 500))
            game.headers["TotalGames"] = str(games)
            game.headers["LichessMasterWisdom"] = str(paths.wisdom_md_path)
            white_clock_ms = MASTER_WISDOM_CLOCK_MS
            black_clock_ms = MASTER_WISDOM_CLOCK_MS
            set_game_clock_headers(game, white_clock_ms, black_clock_ms, "White")
            node = game
            write_live_game_state(paths, game, games_text, index + 1, games, completed=False)
            learner.new_game()
            stockfish.new_game()
            restart_learner_after_game = False
            for _ in range(max_plies):
                if board.is_game_over(claim_draw=True):
                    break
                moving_side = "White" if board.turn == chess.WHITE else "Black"
                move_start_ms = epoch_ms()
                set_game_clock_headers(game, white_clock_ms, black_clock_ms, moving_side, move_start_ms)
                write_live_game_state(paths, game, games_text, index + 1, games, completed=False)
                is_learner_turn = board.turn == chess.WHITE if learner_white else board.turn == chess.BLACK
                if is_learner_turn:
                    try:
                        move = learner.bestmove(board, f"go wtime {int(white_clock_ms)} btime {int(black_clock_ms)}", timeout_seconds=180)
                    except TimeoutError:
                        elapsed_ms = max(0, epoch_ms() - move_start_ms)
                        if moving_side == "White":
                            white_clock_ms = max(0, white_clock_ms - elapsed_ms)
                        else:
                            black_clock_ms = max(0, black_clock_ms - elapsed_ms)
                        game.headers["Termination"] = "learner timeout waiting for bestmove"
                        result = "0-1" if board.turn == chess.WHITE else "1-0"
                        game.headers["Result"] = result
                        restart_learner_after_game = True
                        break
                    if move is None:
                        game.headers["Termination"] = "learner invalid or forfeit"
                        result = "0-1" if board.turn == chess.WHITE else "1-0"
                        game.headers["Result"] = result
                        break
                else:
                    move = stockfish.bestmove(board, f"go depth {depth}", timeout_seconds=30)
                    if move is None:
                        game.headers["Termination"] = "stockfish invalid"
                        result = "0-1" if board.turn == chess.WHITE else "1-0"
                        game.headers["Result"] = result
                        break
                san = board.san(move)
                board.push(move)
                elapsed_ms = max(0, epoch_ms() - move_start_ms)
                if moving_side == "White":
                    white_clock_ms = max(0, white_clock_ms - elapsed_ms)
                    moved_clock_ms = white_clock_ms
                else:
                    black_clock_ms = max(0, black_clock_ms - elapsed_ms)
                    moved_clock_ms = black_clock_ms
                next_running_side = "White" if board.turn == chess.WHITE else "Black"
                set_game_clock_headers(game, white_clock_ms, black_clock_ms, next_running_side)
                node = node.add_variation(move)
                node.comment = f"{san[:60]} [%clk {format_clock_comment(moved_clock_ms)}]"
                write_live_game_state(paths, game, games_text, index + 1, games, completed=False)
            if game.headers.get("Result", "*") == "*":
                outcome = board.outcome(claim_draw=True)
                game.headers["Result"] = board.result(claim_draw=True) if outcome else "1/2-1/2"
                game.headers["Termination"] = str(outcome.termination.name).lower() if outcome else f"max plies {max_plies}"
            set_game_clock_headers(game, white_clock_ms, black_clock_ms, "")
            result = game.headers["Result"]
            score = game_result_score(result, chess.WHITE if learner_white else chess.BLACK)
            learner_points += score
            if score >= 1.0:
                wins += 1
            elif score <= 0.0:
                losses += 1
            else:
                draws += 1
            completed_games = index + 1
            write_live_game_state(paths, game, games_text, index + 1, games, completed=True)
            games_text.append(str(game))
            if not attempt_can_still_pass(learner_points, completed_games, games, pass_score):
                early_stopped = True
                stop_reason = f"target unreachable after {completed_games}/{games} games"
                break
            if restart_learner_after_game:
                learner.close()
                learner = UciPlayer(
                    "Codex-chess-learner",
                    learner_cmd,
                    {"UseMemory": "true", "UseSkills": "true", "LearningMode": "true"},
                    env=learner_env,
                )
    finally:
        learner.close()
        stockfish.close()
    pgn_path.write_text("\n\n".join(games_text) + "\n\n", encoding="utf-8")
    row = record_depth_attempt(
        state,
        depth,
        completed_games or len(games_text) or games,
        learner_points,
        wins,
        draws,
        losses,
        str(pgn_path),
        total_games=games,
        early_stopped=early_stopped,
        stop_reason=stop_reason,
    )
    save_state(state, paths)
    save_leaderboard(state, paths)
    write_wisdom_outputs(state, paths)
    return {"ok": True, "attempt": row, "pgn_path": str(pgn_path), "state_path": str(paths.state_path), "leaderboard": str(paths.leaderboard_path)}


def run_cycles(
    paths: MasterWisdomPaths | None = None,
    cycles: int = 1,
    dry_run_evaluation: bool = False,
    simulated_score: float = 0.0,
    model_synthesis: bool | None = None,
    synthesis_model: str | None = None,
    synthesis_effort: str | None = None,
    synthesis_timeout: int | None = None,
) -> dict:
    paths = paths or default_paths()
    results = []
    for _ in range(max(1, cycles)):
        state = load_state(paths)
        if state.get("completed"):
            break
        learned = learn_batch(
            paths,
            int(state.get("batch_size", 500) or 500),
            model_synthesis=model_synthesis,
            synthesis_model=synthesis_model,
            synthesis_effort=synthesis_effort,
            synthesis_timeout=synthesis_timeout,
        )
        evaluated = evaluate_depth(paths, dry_run=dry_run_evaluation, simulated_score=simulated_score)
        results.append({"learned": learned, "evaluated": evaluated})
        if not evaluated.get("ok", False):
            break
    return {"ok": True, "cycles": results, "state": str(paths.state_path)}


def read_text(path: Path, max_chars: int = 24000) -> dict:
    item = {"exists": path.exists(), "path": str(path), "text": "", "size": 0, "updated_at": ""}
    if not path.exists():
        return item
    stat = path.stat()
    item.update(
        {
            "text": path.read_text(encoding="utf-8", errors="replace")[:max_chars],
            "size": stat.st_size,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
        }
    )
    return item


def read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return default


def header_int(game: chess.pgn.Game, key: str, default: int = 0) -> int:
    try:
        return int(str(game.headers.get(key, "")).strip())
    except (TypeError, ValueError):
        return default


def parse_depth_from_text(text: str) -> int:
    match = re.search(r"depth\s+(\d+)", text or "", re.I)
    return int(match.group(1)) if match else 0


def read_live_games(path: Path) -> list[chess.pgn.Game]:
    if not path.exists():
        return []
    games = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            while True:
                game = chess.pgn.read_game(handle)
                if game is None:
                    break
                games.append(game)
    except OSError:
        return games
    return games


def learner_color_for(game: chess.pgn.Game) -> bool | None:
    white = str(game.headers.get("White", ""))
    black = str(game.headers.get("Black", ""))
    if white == "Codex-chess-learner":
        return chess.WHITE
    if black == "Codex-chess-learner":
        return chess.BLACK
    return None


def collect_current_attempt(state: dict, paths: MasterWisdomPaths | None = None) -> dict:
    paths = paths or default_paths()
    status_path = live_status_path_for(paths.live_pgn_path)
    status = read_json(status_path, {})
    games = read_live_games(paths.live_pgn_path)
    if not games and not status:
        return {
            "exists": False,
            "pgn_path": str(paths.live_pgn_path),
            "status_path": str(status_path),
            "status": "No current master-wisdom match",
        }

    status_game = (status.get("games") or [{}])[0] if isinstance(status, dict) else {}
    latest_game = games[-1] if games else chess.pgn.Game()
    depth = (
        parse_depth_from_text(latest_game.headers.get("Event", ""))
        or parse_depth_from_text(latest_game.headers.get("White", ""))
        or parse_depth_from_text(latest_game.headers.get("Black", ""))
        or int(state.get("current_depth", 1) or 1)
    )
    total_games = (
        header_int(latest_game, "TotalGames", 0)
        or int(status_game.get("total", 0) or 0)
        or int(state.get("games_per_attempt", 10) or 10)
    )
    current_game = int(status.get("locked_game", 0) or 0) if isinstance(status, dict) else 0
    if not current_game:
        current_game = header_int(latest_game, "Round", len(games) or 1)
    batch_size = header_int(latest_game, "BatchSize", int(state.get("batch_size", 500) or 500))

    wins = draws = losses = 0
    learner_points = 0.0
    completed_games = 0
    game_rows = []
    for index, game in enumerate(games, start=1):
        result = game.headers.get("Result", "*")
        round_number = header_int(game, "Round", index)
        color = learner_color_for(game)
        learner_score = None
        if result in {"1-0", "0-1", "1/2-1/2"} and color is not None:
            learner_score = game_result_score(result, color)
            learner_points += learner_score
            completed_games += 1
            if learner_score >= 1.0:
                wins += 1
            elif learner_score <= 0.0:
                losses += 1
            else:
                draws += 1
        game_rows.append(
            {
                "game": round_number,
                "white": game.headers.get("White", ""),
                "black": game.headers.get("Black", ""),
                "result": result,
                "learner_score": learner_score,
            }
        )

    remaining_games = max(0, total_games - completed_games)
    pass_score = float(state.get("pass_score", 0.8) or 0.8)
    required_points = pass_score * max(1, total_games)
    needed_points = max(0.0, required_points - learner_points)
    can_still_pass = learner_points + remaining_games >= required_points
    finished_current = bool(status_game.get("finished")) if isinstance(status_game, dict) else False
    if completed_games >= total_games and learner_points >= required_points:
        status_label = "passed"
    elif completed_games >= total_games or not can_still_pass:
        status_label = "needs more learning"
    elif not finished_current or latest_game.headers.get("Result", "*") == "*":
        status_label = "in progress"
    else:
        status_label = "between games"

    updated_at = ""
    if paths.live_pgn_path.exists():
        updated_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(paths.live_pgn_path.stat().st_mtime))
    return {
        "exists": True,
        "pgn_path": str(paths.live_pgn_path),
        "status_path": str(status_path),
        "updated_at": updated_at,
        "depth": depth,
        "batch_size": batch_size,
        "current_game": current_game,
        "total_games": total_games,
        "completed_games": completed_games,
        "remaining_games": remaining_games,
        "learner_points": learner_points,
        "score": learner_points / max(1, completed_games),
        "match_score": learner_points / max(1, total_games),
        "pass_score": pass_score,
        "required_points": required_points,
        "needed_points": needed_points,
        "wins": wins,
        "draws": draws,
        "losses": losses,
        "can_still_pass": can_still_pass,
        "status": status_label,
        "current_white": status_game.get("white", latest_game.headers.get("White", "")) if isinstance(status_game, dict) else latest_game.headers.get("White", ""),
        "current_black": status_game.get("black", latest_game.headers.get("Black", "")) if isinstance(status_game, dict) else latest_game.headers.get("Black", ""),
        "current_result": status_game.get("result", latest_game.headers.get("Result", "*")) if isinstance(status_game, dict) else latest_game.headers.get("Result", "*"),
        "games": game_rows,
    }


def finalize_unreachable_live_attempt(paths: MasterWisdomPaths | None = None) -> dict:
    paths = paths or default_paths()
    state = load_state(paths)
    attempt = collect_current_attempt(state, paths)
    if not attempt.get("exists"):
        return {"ok": True, "recorded": False, "reason": "no live attempt", "state_path": str(paths.state_path)}
    if int(attempt.get("batch_size") or 0) != int(state.get("batch_size", 500) or 500):
        return {"ok": True, "recorded": False, "reason": "live attempt already superseded", "state_path": str(paths.state_path)}
    if attempt.get("can_still_pass"):
        return {"ok": True, "recorded": False, "reason": "target still reachable", "state_path": str(paths.state_path)}

    completed_games = int(attempt.get("completed_games") or 0)
    if completed_games <= 0:
        return {"ok": True, "recorded": False, "reason": "no completed games to record", "state_path": str(paths.state_path)}

    live_games = read_live_games(paths.live_pgn_path)
    completed = [game for game in live_games if game.headers.get("Result", "*") != "*"]
    if not completed:
        return {"ok": True, "recorded": False, "reason": "live PGN has no completed games", "state_path": str(paths.state_path)}

    completed_games = min(completed_games, len(completed))
    archived_games = completed[:completed_games]
    paths.matches_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    depth = int(attempt.get("depth") or state.get("current_depth", 1) or 1)
    pgn_path = paths.matches_dir / f"master-wisdom-depth-{depth}-{stamp}-early-stop.pgn"
    pgn_text = "\n\n".join(str(game) for game in archived_games) + "\n\n"
    pgn_path.write_text(pgn_text, encoding="utf-8")
    paths.live_pgn_path.write_text(pgn_text, encoding="utf-8")

    total_games = int(attempt.get("total_games") or state.get("games_per_attempt", 10) or 10)
    stop_reason = f"target unreachable after {completed_games}/{total_games} games"
    row = record_depth_attempt(
        state,
        depth,
        completed_games,
        float(attempt.get("learner_points") or 0.0),
        int(attempt.get("wins") or 0),
        int(attempt.get("draws") or 0),
        int(attempt.get("losses") or 0),
        str(pgn_path),
        total_games=total_games,
        early_stopped=True,
        stop_reason=stop_reason,
    )
    save_state(state, paths)
    save_leaderboard(state, paths)
    write_wisdom_outputs(state, paths)

    last_game = archived_games[-1]
    live_status_path_for(paths.live_pgn_path).write_text(
        json.dumps(
            {
                "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "generated_at_epoch": time.time(),
                "output_pgn": str(paths.live_pgn_path),
                "control_pgn": str(paths.live_pgn_path),
                "locked_game": completed_games,
                "games": [
                    {
                        "game": completed_games,
                        "total": total_games,
                        "white": last_game.headers.get("White", "White"),
                        "black": last_game.headers.get("Black", "Black"),
                        "result": last_game.headers.get("Result", "*"),
                        "reason": stop_reason,
                        "finished": True,
                    }
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return {"ok": True, "recorded": True, "attempt": row, "pgn_path": str(pgn_path), "state_path": str(paths.state_path), "leaderboard": str(paths.leaderboard_path)}


def collect_view_data(paths: MasterWisdomPaths | None = None) -> dict:
    paths = paths or default_paths()
    state = load_state(paths)
    manifest = load_manifest(paths, refresh=False) if paths.manifest_path.exists() else {"files": []}
    leaderboard = json.loads(paths.leaderboard_path.read_text(encoding="utf-8")) if paths.leaderboard_path.exists() else {"rows": []}
    downloads = []
    for entry in selected_manifest_files(manifest, latest=True, limit=8):
        path = entry_download_path(entry, paths)
        downloads.append(
            {
                "month": entry.get("month"),
                "filename": entry.get("filename"),
                "exists": path.exists(),
                "size": path.stat().st_size if path.exists() else 0,
                "path": str(path),
            }
        )
    attempts = list(state.get("attempts") or [])
    latest_attempt = attempts[-1] if attempts else {}
    return {
        "root": str(paths.out_dir),
        "updated_at": utc_stamp(),
        "source": {
            "name": SOURCE_NAME,
            "index_url": SOURCE_INDEX_URL,
            "note": SOURCE_NOTE,
            "manifest": str(paths.manifest_path),
            "months": len(manifest.get("files") or []),
            "latest_month": (manifest.get("files") or [{}])[-1].get("month", ""),
            "downloads": downloads,
        },
        "summary": {
            "processed_games": state.get("processed_games", 0),
            "batch_size": state.get("batch_size", 500),
            "current_depth": state.get("current_depth", 1),
            "target_depth": state.get("target_depth", 8),
            "games_per_attempt": state.get("games_per_attempt", 10),
            "pass_score": state.get("pass_score", 0.8),
            "completed": state.get("completed", False),
        },
        "wisdom": read_text(paths.wisdom_md_path),
        "wisdom_json": read_text(paths.wisdom_json_path, max_chars=12000),
        "skill": read_text(paths.skill_path, max_chars=12000),
        "principles": {
            "useful": [{"label": row["label"]} for row in pattern_rows(state, good=True, limit=8)],
            "caution": [{"label": row["label"]} for row in pattern_rows(state, good=False, limit=8)],
        },
        "ladder": {"latest_attempt": latest_attempt, "attempts": attempts[-20:]},
        "leaderboard": leaderboard.get("rows", []),
        "current_attempt": collect_current_attempt(state, paths),
    }


def print_json(data: dict) -> None:
    print(json.dumps(data, indent=2, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser(description="Download Lichess Elite games and extract learner-only master wisdom.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    manifest_cmd = sub.add_parser("manifest")
    manifest_cmd.add_argument("--refresh", action="store_true")

    download_cmd = sub.add_parser("download")
    download_cmd.add_argument("--refresh", action="store_true")
    download_cmd.add_argument("--all", action="store_true")
    download_cmd.add_argument("--limit", type=int, default=1)
    download_cmd.add_argument("--month", action="append", default=[])

    learn_cmd = sub.add_parser("learn")
    learn_cmd.add_argument("--batch-size", type=int)
    learn_cmd.add_argument("--no-model-synthesis", action="store_true")
    learn_cmd.add_argument("--synthesis-model")
    learn_cmd.add_argument("--synthesis-effort")
    learn_cmd.add_argument("--synthesis-timeout", type=int)

    evaluate_cmd = sub.add_parser("evaluate")
    evaluate_cmd.add_argument("--depth", type=int)
    evaluate_cmd.add_argument("--games", type=int)
    evaluate_cmd.add_argument("--max-plies", type=int, default=160)
    evaluate_cmd.add_argument("--dry-run", action="store_true")
    evaluate_cmd.add_argument("--simulated-score", type=float, default=0.0)

    cycle_cmd = sub.add_parser("cycle")
    cycle_cmd.add_argument("--cycles", type=int, default=1)
    cycle_cmd.add_argument("--dry-run-evaluation", action="store_true")
    cycle_cmd.add_argument("--simulated-score", type=float, default=0.0)
    cycle_cmd.add_argument("--no-model-synthesis", action="store_true")
    cycle_cmd.add_argument("--synthesis-model")
    cycle_cmd.add_argument("--synthesis-effort")
    cycle_cmd.add_argument("--synthesis-timeout", type=int)

    sub.add_parser("stop-if-failed")

    sub.add_parser("summary")

    args = parser.parse_args()
    paths = default_paths()
    if args.cmd == "manifest":
        print_json(load_manifest(paths, refresh=args.refresh))
    elif args.cmd == "download":
        print_json(download_manifest_files(paths, latest=True, limit=None if args.all else args.limit, months=args.month or None, refresh=args.refresh))
    elif args.cmd == "learn":
        print_json(
            learn_batch(
                paths,
                args.batch_size,
                model_synthesis=False if args.no_model_synthesis else None,
                synthesis_model=args.synthesis_model,
                synthesis_effort=args.synthesis_effort,
                synthesis_timeout=args.synthesis_timeout,
            )
        )
    elif args.cmd == "evaluate":
        print_json(evaluate_depth(paths, depth=args.depth, games=args.games, max_plies=args.max_plies, dry_run=args.dry_run, simulated_score=args.simulated_score))
    elif args.cmd == "cycle":
        print_json(
            run_cycles(
                paths,
                cycles=args.cycles,
                dry_run_evaluation=args.dry_run_evaluation,
                simulated_score=args.simulated_score,
                model_synthesis=False if args.no_model_synthesis else None,
                synthesis_model=args.synthesis_model,
                synthesis_effort=args.synthesis_effort,
                synthesis_timeout=args.synthesis_timeout,
            )
        )
    elif args.cmd == "stop-if-failed":
        print_json(finalize_unreachable_live_attempt(paths))
    elif args.cmd == "summary":
        print_json(collect_view_data(paths))


if __name__ == "__main__":
    main()
