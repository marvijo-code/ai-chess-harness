"""Build the fixed set of the hardest Lichess puzzles that have exactly one solution.

    python tools/lichess_puzzles.py build --stockfish <path> [--count 100] [--max-rows 3000000]

Source: the Lichess puzzle database (https://database.lichess.org/#puzzles, CC0). It is streamed
and decoded on the fly, so the 1 GB CSV never lands on disk. Columns:
PuzzleId,FEN,Moves,Rating,RatingDeviation,Popularity,NbPlays,Themes,GameUrl,OpeningTags.
FEN is the position BEFORE the opponent's first move; Moves[0] is that move, then the solution
alternates solver / opponent.

"Exactly one solution" is checked with Stockfish (curation only; it never sees a model): at every
solver move the engine's best move must be the puzzle move and the second-best move must be clearly
worse (not winning, or at least UNIQUE_GAP_CP below the best). Puzzles with two mating moves anywhere
are dropped, so the grader can compare moves exactly.

The result is tests/fixtures/lichess-hardest-100.json, sorted easiest first by Lichess rating.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import urllib.request
from pathlib import Path

import chess
import chess.engine

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "lichess-hardest-100.json"
DB_URL = "https://database.lichess.org/lichess_db_puzzle.csv.zst"
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) chess-harness puzzle fixture builder"

MIN_RATING = 2700
MAX_DEVIATION = 90       # a settled rating, not a new puzzle that is still moving
MIN_PLAYS = 300
MIN_POPULARITY = 60      # players did not vote it broken
UNIQUE_GAP_CP = 300
NOT_WINNING_CP = 150
VERIFY_DEPTH = 16


def stream_rows(url: str = DB_URL, max_rows: int | None = None):
    import zstandard

    request = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
    with urllib.request.urlopen(request, timeout=120) as response:
        reader = zstandard.ZstdDecompressor().stream_reader(response)
        text = io.TextIOWrapper(reader, encoding="utf-8", newline="")
        for index, row in enumerate(csv.DictReader(text)):
            if max_rows is not None and index >= max_rows:
                return
            yield row


def candidate(row: dict) -> dict | None:
    try:
        rating, dev = int(row["Rating"]), int(row["RatingDeviation"])
        plays, pop = int(row["NbPlays"]), int(row["Popularity"])
    except (KeyError, ValueError):
        return None
    if rating < MIN_RATING or dev > MAX_DEVIATION or plays < MIN_PLAYS or pop < MIN_POPULARITY:
        return None
    moves = row["Moves"].split()
    if len(moves) < 2 or len(moves) % 2:
        return None
    return {"id": row["PuzzleId"], "fen": row["FEN"], "moves": moves, "rating": rating, "rating_deviation": dev,
            "plays": plays, "popularity": pop, "themes": row["Themes"].split(), "url": f"https://lichess.org/training/{row['PuzzleId']}"}


def solver_positions(puzzle: dict):
    """(board before each solver move, the solution move) pairs."""
    board = chess.Board(puzzle["fen"])
    moves = puzzle["moves"]
    board.push_uci(moves[0])
    for index in range(1, len(moves), 2):
        yield board.copy(), moves[index]
        board.push_uci(moves[index])
        if index + 1 < len(moves):
            board.push_uci(moves[index + 1])


def is_unique(engine: chess.engine.SimpleEngine, puzzle: dict, depth: int = VERIFY_DEPTH) -> tuple[bool, str]:
    for board, solution in solver_positions(puzzle):
        mating = [m for m in board.legal_moves if board.gives_check(m) and _mates(board, m)]
        if len(mating) > 1:
            return False, f"{len(mating)} mating moves at ply {board.ply()}"
        if board.legal_moves.count() == 1:
            continue
        lines = engine.analyse(board, chess.engine.Limit(depth=depth), multipv=2)
        top = lines[0]["pv"][0].uci()
        if top != solution:
            return False, f"engine prefers {top} over {solution} at ply {board.ply()}"
        best = lines[0]["score"].pov(board.turn).score(mate_score=100000)
        second = lines[1]["score"].pov(board.turn).score(mate_score=100000) if len(lines) > 1 else None
        if second is not None and second > NOT_WINNING_CP and best - second < UNIQUE_GAP_CP:
            return False, f"second move {lines[1]['pv'][0].uci()} also wins ({second} vs {best}) at ply {board.ply()}"
    return True, ""


def _mates(board: chess.Board, move: chess.Move) -> bool:
    board.push(move)
    try:
        return board.is_checkmate()
    finally:
        board.pop()


def build(stockfish: str, count: int, max_rows: int, out: Path, pool: int, log=print,
          cache: Path | None = None, depth: int = VERIFY_DEPTH) -> list[dict]:
    found: list[dict] = []
    if cache is not None and cache.is_file():
        found = json.loads(cache.read_text(encoding="utf-8"))
        log(f"{len(found)} candidates from {cache}")
    else:
        for index, row in enumerate(stream_rows(max_rows=max_rows)):
            item = candidate(row)
            if item:
                found.append(item)
            if index and index % 250000 == 0:
                log(f"scanned {index} rows, {len(found)} candidates")
        if cache is not None:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(found), encoding="utf-8")
    found.sort(key=lambda p: (-p["rating"], p["id"]))
    log(f"{len(found)} candidates rated {MIN_RATING}+; verifying the hardest {pool} for a unique solution")
    accepted: list[dict] = []
    engine = chess.engine.SimpleEngine.popen_uci(stockfish)
    engine.configure({"Threads": 1, "Hash": 64})
    try:
        for item in found[:pool]:
            ok, why = is_unique(engine, item, depth)
            if ok:
                accepted.append(item)
                log(f"accepted {item['id']} ({item['rating']}), {len(accepted)}/{count}")
                if len(accepted) == count:
                    break
            else:
                log(f"dropped {item['id']} ({item['rating']}): {why}")
    finally:
        engine.quit()
    if len(accepted) < count:
        raise SystemExit(f"only {len(accepted)} unique puzzles found; raise --max-rows or --pool")
    accepted.sort(key=lambda p: (p["rating"], p["id"]))
    for number, item in enumerate(accepted, 1):
        item["number"] = number
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "source": DB_URL, "license": "CC0 (Lichess puzzle database)",
        "selection": {"min_rating": MIN_RATING, "max_rating_deviation": MAX_DEVIATION, "min_plays": MIN_PLAYS,
                      "min_popularity": MIN_POPULARITY, "rows_scanned": max_rows,
                      "unique_check": f"Stockfish depth {depth} multipv 2: best = puzzle move, second not "
                                      f"winning (<= {NOT_WINNING_CP} cp) or {UNIQUE_GAP_CP}+ cp worse; one mating move at most"},
        "puzzles": accepted}, indent=1), encoding="utf-8")
    log(f"wrote {out} ({len(accepted)} puzzles, ratings {accepted[0]['rating']}-{accepted[-1]['rating']})")
    return accepted


def load(path: Path = FIXTURE) -> list[dict]:
    return json.loads(Path(path).read_text(encoding="utf-8"))["puzzles"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--stockfish", required=True)
    b.add_argument("--count", type=int, default=100)
    b.add_argument("--max-rows", type=int, default=3000000)
    b.add_argument("--pool", type=int, default=600, help="how many of the hardest candidates to verify")
    b.add_argument("--out", type=Path, default=FIXTURE)
    b.add_argument("--depth", type=int, default=VERIFY_DEPTH)
    b.add_argument("--cache", type=Path, default=ROOT / "out" / "puzzles" / "candidates.json",
                   help="candidate rows from an earlier scan (skips the download)")
    args = parser.parse_args(argv)
    if args.cmd == "build":
        build(args.stockfish, args.count, args.max_rows, args.out, args.pool,
              log=lambda m: print(m, flush=True), cache=args.cache, depth=args.depth)
    return 0


if __name__ == "__main__":
    sys.exit(main())
