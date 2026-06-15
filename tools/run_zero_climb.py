from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import random
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import chess


ROOT = Path(__file__).resolve().parents[1]
ZERO_RESEARCH_PATH = ROOT / "engines" / "codex-chess-zero" / "zero_research.py"
CLIMB_DIR = ROOT / "engines" / "codex-chess-zero" / "research" / "climb"
CLIMB_STATE_PATH = CLIMB_DIR / "climb-state.json"
CLIMB_LOG_PATH = CLIMB_DIR / "climb-log.jsonl"
CLIMB_METRICS_PATH = CLIMB_DIR / "climb-metrics.jsonl"
ENGINE_CONFIG_PATH = Path.home() / "AppData/Roaming/org.encroissant.app/engines/engines.json"
PROFILE_DEFAULTS = {
    "quick": {
        "cycles": 1,
        "zero_visits": 8,
        "self_play_games": 1,
        "self_play_visits": 8,
        "self_play_max_plies": 80,
        "train_epochs": 1,
        "promotion_games": 2,
        "promotion_visits": 8,
    },
    "gm-sprint": {
        "cycles": 2,
        "zero_visits": 16,
        "self_play_games": 6,
        "self_play_visits": 16,
        "self_play_max_plies": 120,
        "train_epochs": 2,
        "promotion_games": 8,
        "promotion_visits": 16,
    },
}
INTERNAL_NEUTRAL_SCORE = 0.5


def load_zero_research():
    spec = importlib.util.spec_from_file_location("codex_chess_zero_research_climb", ZERO_RESEARCH_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


zero = load_zero_research()


@dataclass(frozen=True)
class LadderStage:
    name: str
    opponent: str
    games: int
    pass_score: float
    max_plies: int
    stockfish_depth: int = 0
    training_allowed: bool = False
    evaluation_only: bool = True


def now_stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def profile_defaults(profile: str) -> dict[str, int]:
    if profile not in PROFILE_DEFAULTS:
        raise ValueError(f"unknown climb profile: {profile}")
    return dict(PROFILE_DEFAULTS[profile])


def resolve_profile_settings(args: argparse.Namespace) -> dict[str, int]:
    defaults = profile_defaults(args.profile)
    settings = {}
    for name, default in defaults.items():
        value = getattr(args, name)
        settings[name] = default if value is None else value
    return settings


def failed_gate_seed_base(seed: int, attempt_index: int, self_play_games: int) -> int:
    stride = max(1, int(self_play_games))
    return int(seed) + (max(0, int(attempt_index)) * stride)


def material_balance(board: chess.Board, color: bool) -> int:
    return zero.material_balance(board, color)


def locate_stockfish(config_path: Path = ENGINE_CONFIG_PATH) -> Path | None:
    if not config_path.exists():
        return None
    try:
        engines = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    for engine in engines if isinstance(engines, list) else []:
        name = str(engine.get("name", "")).lower()
        path = Path(str(engine.get("path", "")))
        if "stockfish" in name and path.exists():
            return path
    return None


def stage_catalog(stockfish_available: bool | None = None) -> list[LadderStage]:
    if stockfish_available is None:
        stockfish_available = locate_stockfish() is not None
    stages = [
        LadderStage("random-legal", "random", games=8, pass_score=0.70, max_plies=100),
        LadderStage("capture-greedy", "capture_greedy", games=10, pass_score=0.65, max_plies=120),
        LadderStage("one-ply-material", "one_ply_material", games=12, pass_score=0.60, max_plies=140),
    ]
    if stockfish_available:
        stages.extend(
            [
                LadderStage("stockfish-depth-1", "stockfish", games=6, pass_score=0.50, max_plies=120, stockfish_depth=1),
                LadderStage("stockfish-depth-2", "stockfish", games=6, pass_score=0.50, max_plies=120, stockfish_depth=2),
                LadderStage("stockfish-depth-4", "stockfish", games=8, pass_score=0.50, max_plies=140, stockfish_depth=4),
                LadderStage("stockfish-depth-8", "stockfish", games=10, pass_score=0.50, max_plies=160, stockfish_depth=8),
                LadderStage("installed-stockfish-full", "stockfish", games=20, pass_score=0.50, max_plies=200, stockfish_depth=12),
            ]
        )
    return stages


def load_state(path: Path = CLIMB_STATE_PATH) -> dict:
    if not path.exists():
        return {
            "schema": "zero-climb-state-v1",
            "created_at": now_stamp(),
            "updated_at": now_stamp(),
            "current_stage_index": 0,
            "beaten_stages": [],
            "attempts": [],
        }
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(state: dict, path: Path = CLIMB_STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = now_stamp()
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def append_log(row: dict, path: Path = CLIMB_LOG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")


def append_metrics(row: dict, path: Path = CLIMB_METRICS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")


def game_score(result: str, zero_white: bool) -> float:
    if result == "1/2-1/2":
        return 0.5
    if result == "1-0":
        return 1.0 if zero_white else 0.0
    if result == "0-1":
        return 0.0 if zero_white else 1.0
    return 0.5


def choose_random(board: chess.Board, rng: random.Random) -> chess.Move:
    return rng.choice(list(board.legal_moves))


def choose_capture_greedy(board: chess.Board, rng: random.Random) -> chess.Move:
    moves = list(board.legal_moves)
    captures = []
    for move in moves:
        captured = board.piece_at(move.to_square)
        value = zero.PIECE_VALUES.get(captured.piece_type, 0) if captured else 0
        captures.append((value, board.gives_check(move), move.uci(), move))
    best_value = max(row[0] for row in captures)
    best = [row for row in captures if row[0] == best_value]
    return max(best, key=lambda row: (row[1], row[2]))[3] if best_value > 0 else choose_random(board, rng)


def choose_one_ply_material(board: chess.Board, rng: random.Random) -> chess.Move:
    color = board.turn
    best: tuple[int, bool, str, chess.Move] | None = None
    for move in board.legal_moves:
        after = board.copy(stack=False)
        after.push(move)
        score = material_balance(after, color)
        candidate = (score, board.gives_check(move), move.uci(), move)
        if best is None or candidate > best:
            best = candidate
    return best[3] if best else choose_random(board, rng)


class StockfishDepthPlayer:
    def __init__(self, path: Path, depth: int):
        self.path = path
        self.depth = depth
        self.proc: subprocess.Popen[str] | None = None

    def __enter__(self) -> "StockfishDepthPlayer":
        self.proc = subprocess.Popen(
            [str(self.path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(self.path.parent),
        )
        self._send("uci")
        self._read_until("uciok")
        self._send("isready")
        self._read_until("readyok")
        return self

    def __exit__(self, *args) -> None:
        if self.proc and self.proc.poll() is None:
            self._send("quit")
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def _send(self, text: str) -> None:
        assert self.proc and self.proc.stdin
        self.proc.stdin.write(text + "\n")
        self.proc.stdin.flush()

    def _read_until(self, marker: str) -> list[str]:
        assert self.proc and self.proc.stdout
        lines = []
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("Stockfish exited while waiting for " + marker)
            line = line.strip()
            lines.append(line)
            if line == marker or line.startswith(marker + " "):
                return lines

    def choose(self, board: chess.Board) -> chess.Move:
        self._send("position fen " + board.fen())
        self._send(f"go depth {self.depth}")
        for line in self._read_until("bestmove"):
            if line.startswith("bestmove "):
                move = chess.Move.from_uci(line.split()[1])
                if move in board.legal_moves:
                    return move
        raise RuntimeError("Stockfish did not return a legal move")


def play_game(
    stage: LadderStage,
    zero_network,
    zero_visits: int,
    zero_white: bool,
    rng: random.Random,
    stockfish_player: StockfishDepthPlayer | None = None,
) -> dict:
    board = chess.Board()
    plies = 0
    for _ in range(stage.max_plies):
        if board.is_game_over(claim_draw=True):
            break
        is_zero_turn = board.turn == chess.WHITE if zero_white else board.turn == chess.BLACK
        if is_zero_turn:
            move = zero.run_mcts(board, zero_network, visits=zero_visits).move
        elif stage.opponent == "random":
            move = choose_random(board, rng)
        elif stage.opponent == "capture_greedy":
            move = choose_capture_greedy(board, rng)
        elif stage.opponent == "one_ply_material":
            move = choose_one_ply_material(board, rng)
        elif stage.opponent == "stockfish":
            if stockfish_player is None:
                raise RuntimeError("Stockfish stage requested without Stockfish player")
            move = stockfish_player.choose(board)
        else:
            raise ValueError(f"unknown opponent: {stage.opponent}")
        board.push(move)
        plies += 1
    result = board.result(claim_draw=True) if board.outcome(claim_draw=True) else "1/2-1/2"
    return {
        "result": result,
        "zero_white": zero_white,
        "zero_score": game_score(result, zero_white),
        "plies": plies,
        "final_fen": board.fen(),
    }


def evaluate_stage(stage: LadderStage, zero_visits: int, seed: int, network: object | None = None) -> dict:
    rng = random.Random(seed)
    network = network or zero.PolicyValueNetwork.load()
    games = []
    stockfish_path = locate_stockfish() if stage.opponent == "stockfish" else None
    if stage.opponent == "stockfish" and stockfish_path is None:
        return {
            "stage": stage.name,
            "available": False,
            "passed": False,
            "reason": "Stockfish executable not found",
            "training_sources": {"opponent_labels_used": False, "stockfish_labels_used": False},
        }
    player_factory: Callable[[], object]
    player_factory = (
        (lambda: StockfishDepthPlayer(stockfish_path, stage.stockfish_depth))
        if stockfish_path
        else (lambda: contextlib.nullcontext(None))
    )
    with player_factory() as stockfish_player:
        for index in range(stage.games):
            games.append(
                play_game(
                    stage,
                    network,
                    zero_visits=zero_visits,
                    zero_white=index % 2 == 0,
                    rng=rng,
                    stockfish_player=stockfish_player,
                )
            )
    points = sum(game["zero_score"] for game in games)
    score = points / max(1, len(games))
    return {
        "stage": stage.name,
        "opponent": stage.opponent,
        "available": True,
        "generated_at": now_stamp(),
        "network_id": network.network_id,
        "generation": network.generation,
        "games": len(games),
        "zero_points": points,
        "score": round(score, 6),
        "pass_score": stage.pass_score,
        "passed": score >= stage.pass_score,
        "rows": games,
        "training_allowed": stage.training_allowed,
        "evaluation_only": stage.evaluation_only,
        "training_sources": {"opponent_labels_used": False, "stockfish_labels_used": False},
    }


def read_self_play_feedback(path: Path) -> dict:
    try:
        game = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"path": str(path), "readable": False}
    records = list(game.get("records", []))
    moves = [str(record.get("chosen_move", "")) for record in records]
    replay_append = game.get("replay_append", {}) if isinstance(game.get("replay_append", {}), dict) else {}
    outcome_signal = sum(1 for record in records if abs(float(record.get("outcome", 0.0) or 0.0)) > 0)
    terminal_counts: dict[str, int] = {}
    opening_signatures = set()
    for record in records:
        terminal = str(record.get("terminal_kind") or game.get("terminal_kind") or "")
        if terminal:
            terminal_counts[terminal] = terminal_counts.get(terminal, 0) + 1
        if record.get("opening_signature"):
            opening_signatures.add(str(record["opening_signature"]))
    return {
        "path": str(path),
        "readable": True,
        "result": game.get("result"),
        "outcome_source": game.get("outcome_source", ""),
        "terminal_kind": game.get("terminal_kind", ""),
        "plies": game.get("plies", len(records)),
        "position_count": len(records),
        "unique_positions": len({str(record.get("position_key", "")) for record in records if record.get("position_key")}),
        "unique_opening_signatures": len(opening_signatures),
        "terminal_counts": terminal_counts,
        "outcome_signal_positions": outcome_signal,
        "exploratory_moves": sum(1 for record in records if record.get("selection") == "exploratory"),
        "trajectory_signature": zero.stable_id(moves),
        "replay_added": int(replay_append.get("added", 0) or 0),
        "replay_updated_duplicates": int(replay_append.get("updated_duplicates", 0) or 0),
        "replay_skipped_duplicates": int(replay_append.get("skipped_duplicates", 0) or 0),
    }


def summarize_training_feedback(paths: list[Path], candidate: object, promotion: dict) -> dict:
    self_play = [read_self_play_feedback(path) for path in paths]
    readable = [row for row in self_play if row.get("readable")]
    replay_added = sum(int(row.get("replay_added", 0) or 0) for row in readable)
    replay_updated = sum(int(row.get("replay_updated_duplicates", 0) or 0) for row in readable)
    replay_skipped = sum(int(row.get("replay_skipped_duplicates", 0) or 0) for row in readable)
    outcome_signal = sum(int(row.get("outcome_signal_positions", 0) or 0) for row in readable)
    unique_opening_signatures = len(
        {
            str(row.get("trajectory_signature", ""))
            for row in readable
            if row.get("trajectory_signature")
        }
    )
    terminal_counts: dict[str, int] = {}
    novelty_records = 0
    novelty_keys: set[str] = set()
    safe_novelty = 0
    risky_novelty_non_wins = 0
    for row in readable:
        for key, value in dict(row.get("terminal_counts", {})).items():
            terminal_counts[key] = terminal_counts.get(key, 0) + int(value or 0)
        try:
            game = json.loads(Path(str(row.get("path", ""))).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            game = {}
        for record in list(game.get("records", [])) if isinstance(game, dict) else []:
            novelty = record.get("novelty", {})
            if not isinstance(novelty, dict) or not novelty.get("key"):
                continue
            novelty_records += 1
            novelty_keys.add(str(novelty.get("key")))
            tags = set(str(tag) for tag in novelty.get("tags", []))
            if "archive_new" in tags and "safe_refutation" in tags:
                safe_novelty += 1
            if float(record.get("outcome", 0.0) or 0.0) <= 0.0 and novelty.get("refutation_status") in {"watch", "unsafe"}:
                risky_novelty_non_wins += 1
    signatures = [str(row.get("trajectory_signature", "")) for row in readable if row.get("trajectory_signature")]
    duplicate_trajectories = len(signatures) > 1 and len(set(signatures)) < len(signatures)
    promoted = bool(promotion.get("promoted"))
    reasons = []
    if readable and replay_added == 0 and replay_updated == 0:
        reasons.append("self-play produced no new replay-buffer positions")
    if readable and outcome_signal == 0:
        reasons.append("self-play produced no decisive or adjudicated outcome signal")
    if duplicate_trajectories:
        reasons.append("self-play repeated at least one full trajectory")
    guard = promotion.get("external_regression_guard") or {}
    if guard and guard.get("passed") is False:
        reasons.append("candidate regressed on external evaluation guard")
    if not promoted:
        reasons.append("candidate failed promotion gate")
    status = "promoted"
    if not promoted and guard and guard.get("passed") is False:
        status = "external_regression_blocked"
    elif not promoted and readable and replay_added == 0 and replay_updated == 0:
        status = "stalled_no_new_replay_signal"
    elif not promoted:
        status = "candidate_not_stronger_yet"
    return {
        "status": status,
        "reasons": reasons,
        "self_play": self_play,
        "replay_added": replay_added,
        "replay_updated_duplicates": replay_updated,
        "replay_skipped_duplicates": replay_skipped,
        "outcome_signal_positions": outcome_signal,
        "terminal_counts": terminal_counts,
        "true_draw_positions": sum(value for key, value in terminal_counts.items() if key != "capped_draw" and key.endswith("_draw")),
        "capped_draw_positions": int(terminal_counts.get("capped_draw", 0) or 0),
        "repetition_draw_positions": int(terminal_counts.get("repetition_draw", 0) or 0),
        "unique_opening_signatures": unique_opening_signatures,
        "novelty": {
            "records": novelty_records,
            "distinct_keys": len(novelty_keys),
            "safe_archive_new": safe_novelty,
            "risky_non_wins": risky_novelty_non_wins,
        },
        "training_metrics": getattr(candidate, "training_metrics", {}),
        "duplicate_trajectories": duplicate_trajectories,
        "candidate": getattr(candidate, "network_id", ""),
        "promoted": promoted,
        "training_sources": {source: False for source in zero.FORBIDDEN_TRAINING_SOURCES},
    }


def external_regression_guard(
    stage: LadderStage | None,
    baseline_evaluation: dict | None,
    candidate: object,
    zero_visits: int,
    seed: int,
) -> dict:
    guard = {
        "available": False,
        "passed": None,
        "reason": "current failed stage or baseline evaluation unavailable",
        "training_sources": {"opponent_labels_used": False, "stockfish_labels_used": False},
    }
    if stage is None or not baseline_evaluation or "score" not in baseline_evaluation:
        return guard
    baseline_score = float(baseline_evaluation.get("score") or 0.0)
    candidate_evaluation = evaluate_stage(stage, zero_visits=zero_visits, seed=seed, network=candidate)
    guard.update(
        {
            "available": bool(candidate_evaluation.get("available", True)),
            "stage": stage.name,
            "baseline_network_id": baseline_evaluation.get("network_id"),
            "candidate_network_id": getattr(candidate, "network_id", ""),
            "baseline_score": baseline_score,
            "candidate_score": candidate_evaluation.get("score"),
            "evaluation": candidate_evaluation,
            "reason": "candidate evaluation completed",
            "training_sources": candidate_evaluation.get(
                "training_sources",
                {"opponent_labels_used": False, "stockfish_labels_used": False},
            ),
        }
    )
    if not guard["available"] or candidate_evaluation.get("score") is None:
        guard["passed"] = None
        guard["reason"] = candidate_evaluation.get("reason", "candidate evaluation unavailable")
        return guard
    candidate_score = float(candidate_evaluation.get("score") or 0.0)
    guard["passed"] = candidate_score + 1e-9 >= baseline_score
    guard["reason"] = "candidate did not regress" if guard["passed"] else "candidate regressed against current failed stage"
    return guard


def finalize_candidate_promotion(
    candidate: object,
    promotion: dict,
    stage: LadderStage | None = None,
    baseline_evaluation: dict | None = None,
    zero_visits: int = 8,
    seed: int = 1,
) -> dict:
    promotion = dict(promotion)
    internal_promoted = bool(promotion.get("promoted"))
    internal_score = float((promotion.get("match") or {}).get("score") or 0.0)
    internal_neutral = internal_score + 1e-9 >= INTERNAL_NEUTRAL_SCORE
    promotion["internal_promoted"] = internal_promoted
    promotion["internal_neutral"] = internal_neutral
    promotion.setdefault("committed", False)
    if not internal_promoted and not internal_neutral:
        return promotion
    guard = external_regression_guard(stage, baseline_evaluation, candidate, zero_visits=zero_visits, seed=seed)
    promotion["external_regression_guard"] = guard
    if guard.get("passed") is False:
        promotion["promoted"] = False
        promotion["committed"] = False
        promotion["reason"] = (
            "candidate passed internal gate but failed external regression guard"
            if internal_promoted
            else "candidate drew internal gate but failed external regression guard"
        )
        return promotion
    if not internal_promoted and guard.get("passed") is not True:
        promotion["promoted"] = False
        promotion["committed"] = False
        promotion["reason"] = "candidate drew internal gate but external regression guard was unavailable"
        return promotion
    candidate.save(zero.CURRENT_NETWORK_PATH)
    promotion["promoted"] = True
    promotion["committed"] = True
    promotion["reason"] = (
        "candidate met internal gate and external regression guard"
        if internal_promoted and guard.get("passed") is True
        else "candidate drew internal gate and passed external regression guard"
        if guard.get("passed") is True
        else "candidate met internal gate; external regression guard unavailable"
    )
    return promotion


def append_promotion_log(promotion: dict) -> None:
    with zero.PROMOTION_LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(promotion, sort_keys=True, separators=(",", ":")) + "\n")


def train_after_failed_gate(
    self_play_games: int,
    self_play_visits: int,
    max_plies: int,
    epochs: int,
    promotion_games: int,
    promotion_visits: int,
    seed: int | None = None,
    stage: LadderStage | None = None,
    baseline_evaluation: dict | None = None,
    external_zero_visits: int = 8,
    external_seed: int | None = None,
) -> dict:
    paths = (
        zero.run_self_play(games=self_play_games, visits=self_play_visits, max_plies=max_plies, seed=seed)
        if self_play_games > 0
        else []
    )
    reanalysis = zero.reanalyze_replay_records(limit=max(1, self_play_games * 4), visits=max(1, min(self_play_visits, 16)))
    candidate = zero.train_from_replay(epochs=epochs)
    promotion = zero.promotion_gate(games=promotion_games, visits=promotion_visits, threshold=0.55, commit=False, log=False)
    promotion["gate_order"] = "internal_champion_gate_before_external_ladder_retry"
    promotion = finalize_candidate_promotion(
        candidate,
        promotion,
        stage=stage,
        baseline_evaluation=baseline_evaluation,
        zero_visits=external_zero_visits,
        seed=external_seed if external_seed is not None else (seed or 1),
    )
    append_promotion_log(promotion)
    diagnosis = summarize_training_feedback(paths, candidate, promotion)
    wisdom = zero.write_wisdom_delta(paths, diagnosis, candidate.to_dict(), promotion) if paths else {}
    zero.research_summary()
    seed_window = (
        {
            "first": seed,
            "last": seed + max(0, len(paths) - 1),
            "games_requested": self_play_games,
            "games_written": len(paths),
        }
        if seed is not None
        else None
    )
    return {
        "self_play_games": len(paths),
        "self_play_paths": [str(path) for path in paths],
        "self_play_seed_window": seed_window,
        "candidate": candidate.to_dict(),
        "promotion": promotion,
        "reanalysis": reanalysis,
        "diagnosis": diagnosis,
        "wisdom": wisdom,
        "training_sources": {source: False for source in zero.FORBIDDEN_TRAINING_SOURCES},
    }


def run_climb_cycle(
    state_path: Path = CLIMB_STATE_PATH,
    log_path: Path = CLIMB_LOG_PATH,
    stages: list[LadderStage] | None = None,
    zero_visits: int = 8,
    seed: int = 1,
    self_play_games: int = 1,
    self_play_visits: int = 8,
    self_play_max_plies: int = 80,
    train_epochs: int = 1,
    promotion_games: int = 2,
    promotion_visits: int = 8,
    metrics_path: Path = CLIMB_METRICS_PATH,
) -> dict:
    stages = stages or stage_catalog()
    state = load_state(state_path)
    index = int(state.get("current_stage_index", 0))
    if index >= len(stages):
        result = {
            "generated_at": now_stamp(),
            "complete": True,
            "message": "all configured stages passed",
            "current_stage_index": index,
            "beaten_stages": state.get("beaten_stages", []),
        }
        append_log(result, log_path)
        save_state(state, state_path)
        return result

    stage = stages[index]
    attempt_index = len(state.get("attempts", []))
    evaluation = evaluate_stage(stage, zero_visits=zero_visits, seed=seed + attempt_index)
    row = {
        "generated_at": now_stamp(),
        "complete": False,
        "stage_index": index,
        "stage": stage.__dict__,
        "evaluation": evaluation,
        "training": None,
    }
    if evaluation.get("passed"):
        state.setdefault("beaten_stages", []).append(
            {
                "stage": stage.name,
                "score": evaluation["score"],
                "games": evaluation["games"],
                "passed_at": now_stamp(),
            }
        )
        state["current_stage_index"] = index + 1
        row["action"] = "advanced"
    else:
        row["action"] = "trained_self_play_and_retried_later"
        row["training"] = train_after_failed_gate(
            self_play_games=self_play_games,
            self_play_visits=self_play_visits,
            max_plies=self_play_max_plies,
            epochs=train_epochs,
            promotion_games=promotion_games,
            promotion_visits=promotion_visits,
            seed=failed_gate_seed_base(seed, attempt_index, self_play_games),
            stage=stage,
            baseline_evaluation=evaluation,
            external_zero_visits=zero_visits,
            external_seed=seed + attempt_index + 100_000,
        )
    state.setdefault("attempts", []).append(row)
    state["last_result"] = row
    save_state(state, state_path)
    append_log(row, log_path)
    append_metrics(build_round_metrics(row), metrics_path)
    return row


def build_round_metrics(row: dict) -> dict:
    evaluation = dict(row.get("evaluation") or {})
    training = dict(row.get("training") or {})
    diagnosis = dict(training.get("diagnosis") or {})
    promotion = dict(training.get("promotion") or {})
    match = dict(promotion.get("match") or {})
    result_rows = list(evaluation.get("rows") or [])
    wdl = {"wins": 0, "draws": 0, "losses": 0}
    for game in result_rows:
        score = float(game.get("zero_score", 0.5))
        if score >= 1.0:
            wdl["wins"] += 1
        elif score <= 0.0:
            wdl["losses"] += 1
        else:
            wdl["draws"] += 1
    training_metrics = dict(diagnosis.get("training_metrics") or training.get("candidate", {}).get("training_metrics", {}) or {})
    return {
        "generated_at": row.get("generated_at"),
        "stage": (row.get("stage") or {}).get("name"),
        "action": row.get("action"),
        "external_ladder_score": evaluation.get("score"),
        "external_ladder_games": evaluation.get("games"),
        "external_wins": wdl["wins"],
        "external_draws": wdl["draws"],
        "external_losses": wdl["losses"],
        "internal_gate_score": match.get("score"),
        "internal_gate_games": match.get("games"),
        "internal_gate_promoted": promotion.get("promoted"),
        "internal_gate_passed": promotion.get("internal_promoted"),
        "internal_gate_neutral": promotion.get("internal_neutral"),
        "internal_gate_committed": promotion.get("committed"),
        "external_regression_guard_passed": (promotion.get("external_regression_guard") or {}).get("passed"),
        "external_regression_guard_baseline_score": (promotion.get("external_regression_guard") or {}).get("baseline_score"),
        "external_regression_guard_candidate_score": (promotion.get("external_regression_guard") or {}).get("candidate_score"),
        "true_draw_positions": diagnosis.get("true_draw_positions", 0),
        "capped_draw_positions": diagnosis.get("capped_draw_positions", 0),
        "repetition_draw_positions": diagnosis.get("repetition_draw_positions", 0),
        "unique_opening_signatures": diagnosis.get("unique_opening_signatures", 0),
        "replay_added": diagnosis.get("replay_added", 0),
        "replay_updated_duplicates": diagnosis.get("replay_updated_duplicates", 0),
        "replay_skipped_duplicates": diagnosis.get("replay_skipped_duplicates", 0),
        "policy_loss": training_metrics.get("policy_loss"),
        "value_loss": training_metrics.get("value_loss"),
        "training_sources": training.get("training_sources") or diagnosis.get("training_sources") or {},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Codex-chess-zero climb ladder toward installed Stockfish gates.")
    parser.add_argument("--profile", choices=sorted(PROFILE_DEFAULTS), default="quick")
    parser.add_argument("--cycles", type=int)
    parser.add_argument("--zero-visits", type=int)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--self-play-games", type=int)
    parser.add_argument("--self-play-visits", type=int)
    parser.add_argument("--self-play-max-plies", type=int)
    parser.add_argument("--train-epochs", type=int)
    parser.add_argument("--promotion-games", type=int)
    parser.add_argument("--promotion-visits", type=int)
    parser.add_argument("--state", type=Path, default=CLIMB_STATE_PATH)
    parser.add_argument("--log", type=Path, default=CLIMB_LOG_PATH)
    parser.add_argument("--metrics", type=Path, default=CLIMB_METRICS_PATH)
    args = parser.parse_args()
    settings = resolve_profile_settings(args)

    results = []
    for cycle in range(max(1, settings["cycles"])):
        results.append(
            run_climb_cycle(
                state_path=args.state,
                log_path=args.log,
                zero_visits=settings["zero_visits"],
                seed=args.seed + cycle,
                self_play_games=settings["self_play_games"],
                self_play_visits=settings["self_play_visits"],
                self_play_max_plies=settings["self_play_max_plies"],
                train_epochs=settings["train_epochs"],
                promotion_games=settings["promotion_games"],
                promotion_visits=settings["promotion_visits"],
                metrics_path=args.metrics,
            )
        )
        if results[-1].get("complete"):
            break
    print(
        json.dumps(
            {
                "ok": True,
                "profile": args.profile,
                "settings": settings,
                "results": results,
                "state": str(args.state),
                "log": str(args.log),
                "metrics": str(args.metrics),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
