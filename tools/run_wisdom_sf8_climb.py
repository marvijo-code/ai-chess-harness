"""Loop Wisdom-chess vs Stockfish depth 8 until a win."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import chess

ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))
from run_wisdom_depth_gate import play_gate

STATE_PATH = ROOT / "out" / "wisdom-depth-matches" / "sf8-climb-state.json"


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"attempts": 0, "wins": 0, "losses": 0, "draws": 0, "history": []}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--depth", type=int, default=8)
    parser.add_argument("--wisdom-ms", type=int, default=12000)
    parser.add_argument("--max-attempts", type=int, default=50)
    parser.add_argument("--max-plies", type=int, default=200)
    args = parser.parse_args()

    state = load_state()
    movetimes = [args.wisdom_ms, args.wisdom_ms + 4000, args.wisdom_ms + 8000, 20000]

    for attempt in range(state["attempts"], args.max_attempts):
        wisdom_white = attempt % 2 == 0
        ms = movetimes[attempt % len(movetimes)]
        print(f"\n=== Attempt {attempt + 1}: Wisdom as {'White' if wisdom_white else 'Black'}, {ms}ms ===", flush=True)
        _, result, archive = play_gate(args.depth, ms, wisdom_white, None, args.max_plies)
        won = (result == "1-0" and wisdom_white) or (result == "0-1" and not wisdom_white)
        state["attempts"] = attempt + 1
        if won:
            state["wins"] += 1
        elif result == "1/2-1/2":
            state["draws"] += 1
        else:
            state["losses"] += 1
        state["history"].append(
            {
                "attempt": attempt + 1,
                "result": result,
                "won": won,
                "wisdom_white": wisdom_white,
                "wisdom_ms": ms,
                "pgn": archive.name,
                "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        )
        save_state(state)
        print(f"Result: {result} (won={won})", flush=True)
        if won and args.depth >= 8:
            print(json.dumps({"goal_met": True, "attempt": attempt + 1, "pgn": str(archive)}, indent=2))
            sys.exit(0)

    print(json.dumps({"goal_met": False, "attempts": state["attempts"]}, indent=2))
    sys.exit(1)


if __name__ == "__main__":
    main()
