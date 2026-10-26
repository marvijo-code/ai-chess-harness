"""Mirror a running or finished LLM match PGN into tournament-viewer state.

The single-game viewer (`live_pgn_viewer.py`) serves one PGN. The tournament
viewer (`llm_tournament_viewer.py`) serves a tournament state JSON with games,
moves, clocks, standings and round pairings. This bridge reads a match PGN plus
its archive JSON (times, tries, comments) and writes a tournament-shaped state
file, so any match can be watched in the tournament UI.

Read-only on the match: it only ever writes the state file it is given.

    python tools/tourney_state_bridge.py --live-pgn out/live/<slug>-live.pgn \
        --state out/live/<slug>-tournament.json [--once] [--interval 5]

Then: python tools/llm_tournament_viewer.py --state out/live/<slug>-tournament.json
"""

from __future__ import annotations

import argparse
import io
import json
import time
from pathlib import Path

import chess
import chess.pgn

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE_DIR = ROOT / "out" / "llm-matches"
GAME_ID = "r1b1"


def _archive(slug: str, live_pgn: Path) -> dict:
    """The match archive for this PGN (next to it in out/llm-matches when present)."""
    for path in (ARCHIVE_DIR / f"{slug}.json", live_pgn.parent / f"{slug}.json", live_pgn.with_suffix(".json")):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return {}


def _move_extras(archive: dict) -> tuple[dict[int, dict], dict[str, int]]:
    games = archive.get("games") or []
    moves = (games[0].get("moves") if games else []) or []
    extras = {m["ply"]: m for m in moves if m.get("ply")}
    invalid = (games[0].get("invalid_attempts") if games else {}) or {}
    return extras, {"white": int(invalid.get("white", 0)), "black": int(invalid.get("black", 0))}


def _points(result: str, finished: bool) -> tuple[float, float]:
    if not finished:
        return 0.0, 0.0
    if result == "1-0":
        return 1.0, 0.0
    if result == "0-1":
        return 0.0, 1.0
    return 0.5, 0.5


def _record(points: float, finished: bool) -> tuple[int, int, int]:
    if not finished:
        return 0, 0, 0
    if points == 1.0:
        return 1, 0, 0
    if points == 0.5:
        return 0, 1, 0
    return 0, 0, 1


def build_state(live_pgn: Path, slug: str) -> dict | None:
    """Tournament-shaped state for the match in `live_pgn`, or None when unreadable."""
    try:
        text = live_pgn.read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.strip():
        return None
    try:
        game = chess.pgn.read_game(io.StringIO(text))
    except (ValueError, KeyError):
        return None
    if game is None:
        return None

    headers = game.headers
    archive = _archive(slug, live_pgn)
    extras, invalid = _move_extras(archive)

    board = game.board()
    moves = []
    for ply, move in enumerate(game.mainline_moves(), start=1):
        san = board.san(move)
        extra = extras.get(ply) or {}
        moves.append({
            "ply": ply,
            "side": "white" if board.turn == chess.WHITE else "black",
            "san": san,
            "uci": move.uci(),
            "tries": int(extra.get("tries", 1)),
            "elapsed_ms": int(extra.get("elapsed_ms", 0)),
            "comment": str(extra.get("comment") or ""),
        })
        board.push(move)

    result = headers.get("Result", "*")
    finished = result != "*"
    white = headers.get("White", "White")
    black = headers.get("Black", "Black")
    try:
        clocks = {"white": int(headers.get("WhiteClockMs", 600000)),
                  "black": int(headers.get("BlackClockMs", 600000))}
    except ValueError:
        clocks = {"white": 600000, "black": 600000}

    wpts, bpts = _points(result, finished)
    ww, wd, wl = _record(wpts, finished)
    bw, bd, bl = _record(bpts, finished)

    return {
        "id": slug,
        "title": f"{white} vs {black}",
        "games": {GAME_ID: {
            "id": GAME_ID, "board": 1, "round": 1, "white": white, "black": black,
            "status": "live" if not finished else "finished",
            "result": result, "termination": headers.get("Termination", ""),
            "fen": board.fen(), "moves": moves, "clocks": clocks,
        }},
        "standings": [
            {"name": white, "rank": 1 if wpts >= bpts else 2, "points": wpts,
             "played": 1 if finished else 0, "elo": 1500, "elo_delta": 0,
             "wins": ww, "draws": wd, "losses": wl,
             "forfeits": 0, "flags": 0, "invalid_attempts": invalid["white"]},
            {"name": black, "rank": 1 if bpts > wpts else 2, "points": bpts,
             "played": 1 if finished else 0, "elo": 1500, "elo_delta": 0,
             "wins": bw, "draws": bd, "losses": bl,
             "forfeits": 0, "flags": 0, "invalid_attempts": invalid["black"]},
        ],
        "players": [{"name": white, "provider": headers.get("WhiteProvider", "")},
                    {"name": black, "provider": headers.get("BlackProvider", "")}],
        "config": {"timeControlMs": 600000, "incrementMs": 10000, "maxAttempts": 3,
                   "startElo": 1500, "eloK": 32, "rounds": 1},
        "current_round": 1,
        "rounds": [{"round": 1, "status": "finished" if finished else "live",
                    "pairings": [{"game_id": GAME_ID, "white": white, "black": black}]}],
        "finished": finished,
        "winner": (white if result == "1-0" else black if result == "0-1" else None) if finished else None,
        "updated_epoch_ms": int(time.time() * 1000),
    }


def write_state(state: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    for _ in range(20):
        try:
            tmp.replace(path)
            return
        except PermissionError:      # the viewer may hold the file for a moment on Windows
            time.sleep(0.05)
    path.write_text(json.dumps(state), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--live-pgn", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--once", action="store_true", help="write once and exit")
    parser.add_argument("--interval", type=float, default=5.0)
    args = parser.parse_args(argv)

    slug = args.state.name[: -len("-tournament.json")] if args.state.name.endswith("-tournament.json") else args.state.stem
    while True:
        state = build_state(args.live_pgn, slug)
        if state is not None:
            write_state(state, args.state)
        if args.once or (state is not None and state["finished"]):
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
