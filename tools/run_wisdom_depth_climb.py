"""Climb ladder: pure Wisdom-chess vs Stockfish depth 1..8 (win required to advance)."""

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
from run_wisdom_depth_gate import WISDOM_CMD, play_gate

STATE_PATH = ROOT / "out" / "wisdom-depth-matches" / "depth-climb-state.json"
LOG_PATH = ROOT / "engines" / "wisdom-chess" / "wisdom-climb-log.md"
MAX_DEPTH = 8


def default_state() -> dict:
    return {
        "current_depth": 1,
        "max_passed_depth": 0,
        "attempts_total": 0,
        "wins": 0,
        "losses": 0,
        "draws": 0,
        "history": [],
        "goal_met": False,
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


def append_climb_log(depth: int, result: str, won: bool, archive_name: str) -> None:
    outcome = "PASS" if won else ("DRAW" if result == "1/2-1/2" else "FAIL")
    stamp = time.strftime("%Y-%m-%d")
    block = f"""
### {stamp} — Depth {depth} ({outcome})

**Result:** {result} — `{archive_name}`

**Next:** {"Advance to depth " + str(depth + 1) if won and depth < MAX_DEPTH else ("Goal met at depth 8." if won and depth >= MAX_DEPTH else "Retry same depth; tune engine or movetime.")}
"""
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not LOG_PATH.exists():
        LOG_PATH.write_text(
            "# Wisdom-chess depth climb\n\nPure TWIC wisdom engine vs fixed-depth Stockfish. Win to advance.\n\n## Log\n",
            encoding="utf-8",
        )
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(block)


def wisdom_won(result: str, wisdom_white: bool) -> bool:
    return (result == "1-0" and wisdom_white) or (result == "0-1" and not wisdom_white)


def movetime_for_depth(base_ms: int, depth: int, attempt_at_depth: int) -> int:
    bump = (attempt_at_depth // 2) * 3000
    depth_bonus = max(0, depth - 3) * 2000
    return min(90000, base_ms + bump + depth_bonus)


def main() -> None:
    parser = argparse.ArgumentParser(description="Pure wisdom engine vs SF depth 1→8 climb")
    parser.add_argument("--start-depth", type=int, default=1)
    parser.add_argument("--target-depth", type=int, default=MAX_DEPTH)
    parser.add_argument("--wisdom-ms", type=int, default=15000)
    parser.add_argument("--max-attempts", type=int, default=0, help="0 = unlimited until pass or Ctrl+C")
    parser.add_argument("--max-plies", type=int, default=200)
    parser.add_argument("--live-pgn", type=Path)
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()

    state = default_state() if args.reset else load_state()
    if args.reset:
        state["current_depth"] = max(1, args.start_depth)
    target = min(MAX_DEPTH, max(1, args.target_depth))
    depth = max(1, min(target, int(state.get("current_depth", args.start_depth))))
    state["current_depth"] = depth

    attempts_run = 0
    print(
        f"Wisdom climb: pure engine vs Stockfish depth {depth}..{target} "
        f"(passed through depth {state.get('max_passed_depth', 0)})",
        flush=True,
    )

    while depth <= target:
        attempt_at_depth = sum(
            1 for row in state["history"] if row.get("depth") == depth and not row.get("won")
        )
        wisdom_white = attempt_at_depth % 2 == 0
        ms = movetime_for_depth(args.wisdom_ms, depth, attempt_at_depth)
        attempts_run += 1
        if args.max_attempts and attempts_run > args.max_attempts:
            break

        print(
            f"\n=== Depth {depth} attempt {attempt_at_depth + 1}: "
            f"Wisdom as {'White' if wisdom_white else 'Black'}, {ms}ms ===",
            flush=True,
        )
        try:
            _, result, archive = play_gate(
                depth,
                ms,
                wisdom_white,
                args.live_pgn.resolve() if args.live_pgn else None,
                args.max_plies,
                wisdom_cmd=WISDOM_CMD,
            )
        except Exception as exc:
            print(f"Game error (retrying): {exc}", flush=True)
            time.sleep(3)
            continue
        won = wisdom_won(result, wisdom_white)
        state["attempts_total"] = int(state.get("attempts_total", 0)) + 1
        if won:
            state["wins"] += 1
        elif result == "1/2-1/2":
            state["draws"] += 1
        else:
            state["losses"] += 1

        row = {
            "depth": depth,
            "attempt": attempt_at_depth + 1,
            "result": result,
            "won": won,
            "wisdom_white": wisdom_white,
            "wisdom_ms": ms,
            "pgn": archive.name,
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        state["history"].append(row)
        append_climb_log(depth, result, won, archive.name)
        save_state(state)
        print(f"Result: {result} (won={won})", flush=True)

        if won:
            state["max_passed_depth"] = max(int(state.get("max_passed_depth", 0)), depth)
            if depth >= target:
                state["goal_met"] = True
                save_state(state)
                print(
                    json.dumps(
                        {
                            "goal_met": True,
                            "max_passed_depth": depth,
                            "pgn": str(archive),
                        },
                        indent=2,
                    )
                )
                sys.exit(0)
            depth += 1
            state["current_depth"] = depth
            save_state(state)
            print(f"Advanced to depth {depth}", flush=True)
        else:
            save_state(state)

    summary = {
        "goal_met": bool(state.get("goal_met")),
        "current_depth": state.get("current_depth"),
        "max_passed_depth": state.get("max_passed_depth"),
        "attempts_total": state.get("attempts_total"),
    }
    print(json.dumps(summary, indent=2))
    sys.exit(0 if summary["goal_met"] else 1)


if __name__ == "__main__":
    main()
