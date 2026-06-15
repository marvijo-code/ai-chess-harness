"""Run N Composer vs Stockfish depth gate games and score the batch."""

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
from run_composer_depth_gate import play_gate

OUT_DIR = ROOT / "out" / "composer-training"


def composer_points(result: str, composer_white: bool) -> float:
    if result == "1/2-1/2":
        return 0.5
    won = (result == "1-0" and composer_white) or (result == "0-1" and not composer_white)
    return 1.0 if won else 0.0


def run_batch(
    depth: int,
    games: int,
    composer_ms: int,
    pass_score: float,
    live_pgn: Path | None,
    max_plies: int,
) -> dict:
    depth = max(1, depth)
    rows: list[dict] = []
    total_points = 0.0

    for index in range(games):
        composer_white = index % 2 == 0
        print(
            f"\n=== Depth {depth} game {index + 1}/{games} "
            f"(Composer {'White' if composer_white else 'Black'}) ===",
            flush=True,
        )
        try:
            _, result, archive = play_gate(depth, composer_ms, composer_white, live_pgn, max_plies)
        except Exception as exc:
            print(f"Game error: {exc}", flush=True)
            result = "0-1" if composer_white else "1-0"
            archive = OUT_DIR / f"batch-error-depth-{depth}-{index}.txt"
            archive.write_text(str(exc), encoding="utf-8")
        points = composer_points(result, composer_white)
        total_points += points
        rows.append(
            {
                "game": index + 1,
                "result": result,
                "points": points,
                "composer_white": composer_white,
                "pgn": archive.name if hasattr(archive, "name") else str(archive),
            }
        )
        print(f"Result: {result} ({points} pt)", flush=True)
        required = pass_score * games
        remaining = games - (index + 1)
        max_future = total_points + remaining
        if max_future < required:
            print("Batch cannot reach pass mark — stopping early.", flush=True)
            break
        if total_points >= required:
            print("Pass mark reached — stopping early.", flush=True)
            break

    passed = total_points >= pass_score * games
    summary = {
        "depth": depth,
        "games_requested": games,
        "games_played": len(rows),
        "points": total_points,
        "pass_score": pass_score,
        "required_points": pass_score * games,
        "passed": passed,
        "rows": rows,
        "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    (OUT_DIR / f"batch-depth-{depth}-{stamp}.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--depth", type=int, required=True)
    parser.add_argument("--games", type=int, default=10)
    parser.add_argument("--composer-ms", type=int, default=12000)
    parser.add_argument("--pass-score", type=float, default=0.8)
    parser.add_argument("--live-pgn", type=Path)
    parser.add_argument("--max-plies", type=int, default=200)
    args = parser.parse_args()

    summary = run_batch(
        args.depth,
        args.games,
        args.composer_ms,
        args.pass_score,
        args.live_pgn.resolve() if args.live_pgn else None,
        args.max_plies,
    )
    print(json.dumps(summary, indent=2))
    sys.exit(0 if summary["passed"] else 1)


if __name__ == "__main__":
    main()
