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


def _read_games(live_pgn: Path) -> list[chess.pgn.Game]:
    """Every game in the PGN, in order (a series writes them one after another)."""
    try:
        text = live_pgn.read_text(encoding="utf-8")
    except OSError:
        return []
    games = []
    handle = io.StringIO(text)
    while True:
        try:
            game = chess.pgn.read_game(handle)
        except (ValueError, KeyError):
            break
        if game is None:
            break
        if not game.headers.get("White") and not game.headers.get("Black"):
            break
        games.append(game)
    return games


def _moves_of(game: chess.pgn.Game, extras: dict[int, dict]) -> tuple[list[dict], str]:
    board = game.board()
    moves = []
    for ply, move in enumerate(game.mainline_moves(), start=1):
        extra = extras.get(ply) or {}
        moves.append({"ply": ply,
                      "side": "white" if board.turn == chess.WHITE else "black",
                      "san": board.san(move), "uci": move.uci(),
                      "tries": int(extra.get("tries", 1)),
                      "elapsed_ms": int(extra.get("elapsed_ms", 0)),
                      "comment": str(extra.get("comment") or "")})
        board.push(move)
    return moves, board.fen()


def build_state(live_pgn: Path, slug: str) -> dict | None:
    """Tournament-shaped state for every game in `live_pgn`, or None when unreadable."""
    games = _read_games(live_pgn)
    if not games:
        return None

    archive = _archive(slug, live_pgn)
    archived = archive.get("games") or []

    state_games: dict[str, dict] = {}
    rounds: list[dict] = []
    table: dict[str, dict] = {}
    title_white = title_black = ""

    for index, game in enumerate(games, start=1):
        gid = f"r{index}b1"
        headers = game.headers
        white = headers.get("White", "White")
        black = headers.get("Black", "Black")
        title_white, title_black = title_white or white, title_black or black
        extras, invalid = _move_extras({"games": [archived[index - 1]]} if index - 1 < len(archived) else {})
        moves, fen = _moves_of(game, extras)
        result = headers.get("Result", "*")
        finished = result != "*"
        try:
            clocks = {"white": int(headers.get("WhiteClockMs", 600000)),
                      "black": int(headers.get("BlackClockMs", 600000))}
        except ValueError:
            clocks = {"white": 600000, "black": 600000}

        state_games[gid] = {
            "id": gid, "board": 1, "round": index, "white": white, "black": black,
            "status": "finished" if finished else "live", "result": result,
            "termination": headers.get("Termination", ""), "fen": fen, "moves": moves,
            "clocks": clocks,
        }
        rounds.append({"round": index, "status": "finished" if finished else "live",
                       "pairings": [{"game_id": gid, "white": white, "black": black}]})

        wpts, bpts = _points(result, finished)
        for name, pts, bad in ((white, wpts, invalid["white"]), (black, bpts, invalid["black"])):
            row = table.setdefault(name, {"name": name, "points": 0.0, "played": 0,
                                          "wins": 0, "draws": 0, "losses": 0,
                                          "invalid_attempts": 0})
            row["points"] += pts
            if finished:
                row["played"] += 1
                wins, draws, losses = _record(pts, finished)
                row["wins"] += wins
                row["draws"] += draws
                row["losses"] += losses
            row["invalid_attempts"] += bad

    standings = []
    for rank, row in enumerate(sorted(table.values(), key=lambda r: -r["points"]), start=1):
        standings.append({**row, "rank": rank, "elo": 1500, "elo_delta": 0, "forfeits": 0, "flags": 0})

    last = games[-1]
    finished = last.headers.get("Result", "*") != "*"
    winner = None
    if finished and standings:
        winner = standings[0]["name"] if standings[0]["points"] > standings[-1]["points"] else None

    return {
        "id": slug,
        "title": f"{title_white} vs {title_black}",
        "games": state_games,
        "standings": standings,
        "players": [{"name": n, "provider": ""} for n in table],
        "config": {"timeControlMs": 1200000, "incrementMs": 10000, "maxAttempts": 3,
                   "startElo": 1500, "eloK": 32, "rounds": len(games)},
        "current_round": len(games),
        "rounds": rounds,
        "finished": finished and len(state_games) == len(games),
        "winner": winner,
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
