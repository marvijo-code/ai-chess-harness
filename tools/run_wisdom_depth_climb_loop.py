"""Never-stop loop: pure Wisdom-chess vs Stockfish depth 1..8 until goal met."""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

STATE_PATH = ROOT / "out" / "wisdom-depth-matches" / "depth-climb-state.json"
LOCK_PATH = ROOT / "out" / "wisdom-depth-matches" / "climb-loop.lock"
LOG_PATH = ROOT / "out" / "wisdom-depth-matches" / "climb-loop-log.jsonl"


def log_event(event: str, **fields: object) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    row = {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event, **fields}
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")
    print(json.dumps(row), flush=True)


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {}


def write_lock() -> None:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOCK_PATH.write_text(
        json.dumps({"pid": os.getpid(), "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}),
        encoding="utf-8",
    )


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--wisdom-ms", type=int, default=15000)
    parser.add_argument("--target-depth", type=int, default=8)
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--live-pgn", type=Path)
    args = parser.parse_args()

    write_lock()
    log_event("loop_start", pid=os.getpid(), wisdom_ms=args.wisdom_ms)

    if args.reset and STATE_PATH.exists():
        STATE_PATH.unlink()

    from run_wisdom_depth_climb import default_state, load_state, main as climb_main, save_state

    restart_count = 0
    while True:
        state = load_state()
        if state.get("goal_met"):
            log_event("goal_met", max_passed_depth=state.get("max_passed_depth"))
            sys.exit(0)

        argv = [
            "run_wisdom_depth_climb.py",
            "--wisdom-ms",
            str(args.wisdom_ms),
            "--target-depth",
            str(args.target_depth),
        ]
        if args.live_pgn:
            argv.extend(["--live-pgn", str(args.live_pgn.resolve())])

        old_argv = sys.argv
        sys.argv = argv
        try:
            log_event("climb_run_start", restart_count=restart_count, current_depth=state.get("current_depth", 1))
            climb_main()
            state = load_state()
            if state.get("goal_met"):
                log_event("goal_met", max_passed_depth=state.get("max_passed_depth"))
                sys.exit(0)
            log_event("climb_run_exit", reason="returned_without_goal")
        except KeyboardInterrupt:
            log_event("loop_stop", reason="keyboard_interrupt")
            sys.exit(130)
        except Exception as exc:
            log_event("climb_crash", error=str(exc), traceback=traceback.format_exc())
            time.sleep(5)
        finally:
            sys.argv = old_argv

        restart_count += 1
        state = load_state()
        if not state:
            save_state(default_state())
        time.sleep(2)


if __name__ == "__main__":
    main()
