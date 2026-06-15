"""Train Composer wisdom locally, then test 10-game depth gates 1→8; repeat until depth 8 pass."""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from composer_wisdom_trainer import (
    is_significant_delta,
    snapshot_baseline,
    train_from_pgns,
    wisdom_delta,
)
from run_composer_depth_gate_batch import run_batch

STATE_PATH = ROOT / "out" / "composer-training" / "loop-state.json"
LOG_PATH = ROOT / "out" / "composer-training" / "loop-log.jsonl"
MAX_DEPTH = 8


def log_event(event: str, **fields: object) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    row = {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event, **fields}
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")
    print(json.dumps(row), flush=True)


def default_state() -> dict:
    return {
        "current_depth": 1,
        "max_passed_depth": 0,
        "cycle": 0,
        "goal_met": False,
        "history": [],
    }


def load_state() -> dict:
    if STATE_PATH.exists():
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        for key, value in default_state().items():
            data.setdefault(key, value)
        return data
    return default_state()


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def training_phase(
    baseline: dict,
    min_chars: int,
    max_rounds: int,
) -> dict:
    rounds = 0
    last_lessons = -1
    while rounds < max_rounds:
        rounds += 1
        outcome = train_from_pgns(min_significant_chars=min_chars)
        delta = wisdom_delta(baseline)
        log_event(
            "train_round",
            round=rounds,
            games_scanned=outcome.games_scanned,
            lessons_added=outcome.lessons_added,
            principles_added=outcome.principles_added,
            rules_added=outcome.rules_added,
            chars_added=delta.get("chars_added", 0),
            significant=is_significant_delta(delta, min_chars),
        )
        if is_significant_delta(delta, min_chars):
            return {"rounds": rounds, "delta": delta, "outcome": outcome.__dict__}
        if outcome.lessons_added == 0 and last_lessons == 0:
            log_event("train_plateau", round=rounds)
            break
        last_lessons = outcome.lessons_added
        time.sleep(1)
    delta = wisdom_delta(baseline)
    return {"rounds": rounds, "delta": delta, "plateau": True}


def main() -> None:
    parser = argparse.ArgumentParser(description="Composer train→test loop (local only, no API)")
    parser.add_argument("--target-depth", type=int, default=MAX_DEPTH)
    parser.add_argument("--games", type=int, default=10)
    parser.add_argument("--pass-score", type=float, default=0.8)
    parser.add_argument("--composer-ms", type=int, default=12000)
    parser.add_argument("--min-chars", type=int, default=200, help="Min wisdom delta vs baseline to enter test")
    parser.add_argument("--max-train-rounds", type=int, default=8)
    parser.add_argument("--live-pgn", type=Path)
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()

    state = default_state() if args.reset else load_state()
    if args.reset:
        save_state(state)

    target = min(MAX_DEPTH, max(1, args.target_depth))
    depth = max(1, min(target, int(state.get("current_depth", 1))))

    log_event("loop_start", depth=depth, target=target)

    while depth <= target:
        state["cycle"] = int(state.get("cycle", 0)) + 1
        cycle = state["cycle"]
        log_event("cycle_start", cycle=cycle, depth=depth)

        baseline = snapshot_baseline()
        train_summary = training_phase(baseline, args.min_chars, args.max_train_rounds)
        delta = train_summary.get("delta", wisdom_delta(baseline))

        if not is_significant_delta(delta, args.min_chars):
            log_event("train_forced_test", reason="plateau_or_insufficient_delta", delta=delta)
        else:
            log_event("train_complete", delta=delta)

        try:
            batch = run_batch(
                depth,
                args.games,
                args.composer_ms,
                args.pass_score,
                args.live_pgn.resolve() if args.live_pgn else None,
                200,
            )
        except Exception as exc:
            log_event("batch_crash", error=str(exc), traceback=traceback.format_exc())
            time.sleep(5)
            continue

        row = {
            "cycle": cycle,
            "depth": depth,
            "passed": batch["passed"],
            "points": batch["points"],
            "games_played": batch["games_played"],
            "train_rounds": train_summary.get("rounds"),
            "delta": delta,
        }
        state["history"].append(row)
        log_event("batch_done", **row)

        if batch["passed"]:
            state["max_passed_depth"] = max(int(state.get("max_passed_depth", 0)), depth)
            if depth >= target:
                state["goal_met"] = True
                state["current_depth"] = depth
                save_state(state)
                log_event("goal_met", depth=depth, points=batch["points"])
                sys.exit(0)
            depth += 1
            state["current_depth"] = depth
            save_state(state)
            log_event("depth_advanced", new_depth=depth)
        else:
            save_state(state)
            log_event("batch_failed", depth=depth, points=batch["points"])

        time.sleep(2)


if __name__ == "__main__":
    main()
