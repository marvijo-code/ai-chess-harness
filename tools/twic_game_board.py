"""Print FEN and file occupancy at selected plies for Layer A game study."""
from __future__ import annotations

import argparse
import io
import json
from pathlib import Path

import chess
import chess.pgn

ROOT = Path(__file__).resolve().parents[1]


def file_occupancy(board: chess.Board, file_idx: int) -> str:
    parts: list[str] = []
    for rank in range(8):
        sq = chess.square(file_idx, rank)
        piece = board.piece_at(sq)
        if piece:
            parts.append(f"{chess.square_name(sq)}={piece.symbol()}")
    return ", ".join(parts) if parts else "empty"


def board_snapshot(board: chess.Board, label: str) -> dict:
    return {
        "label": label,
        "fen": board.fen(),
        "turn": "white" if board.turn == chess.WHITE else "black",
        "files": {chess.FILE_NAMES[f]: file_occupancy(board, f) for f in range(8)},
    }


def snapshots_for_game(game: chess.pgn.Game, plies: list[int]) -> list[dict]:
    board = game.board()
    moves = list(game.mainline_moves())
    targets = sorted(set(plies))
    out: list[dict] = []
    cursor = 0
    for ply in range(1, len(moves) + 1):
        san = board.san(moves[ply - 1])
        board.push(moves[ply - 1])
        if ply in targets:
            out.append({**board_snapshot(board, f"ply {ply} ({san})"), "ply": ply, "san": san})
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Board-verified snapshots for TWIC game study")
    parser.add_argument("pgn", type=Path, help="PGN file with one game")
    parser.add_argument("--plies", default="", help="Comma-separated ply numbers, e.g. 34,35,36")
    parser.add_argument("--phases", action="store_true", help="Default phase boundaries: 1,20,40,60,80,final")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    text = args.pgn.read_text(encoding="utf-8")
    game = chess.pgn.read_game(io.StringIO(text))
    if game is None:
        raise SystemExit("No game found in PGN")

    if args.plies:
        plies = [int(x.strip()) for x in args.plies.split(",") if x.strip()]
    elif args.phases:
        total = len(list(game.mainline_moves()))
        plies = sorted({1, 20, 40, 60, 80, total} & set(range(1, total + 1)))
    else:
        plies = [1]

    snaps = snapshots_for_game(game, plies)
    headers = {
        "white": game.headers.get("White"),
        "black": game.headers.get("Black"),
        "result": game.headers.get("Result"),
        "event": game.headers.get("Event"),
        "date": game.headers.get("Date"),
    }
    payload = {"headers": headers, "snapshots": snaps}

    if args.json:
        print(json.dumps(payload, indent=2))
        return

    print(f"{headers['white']} vs {headers['black']} — {headers['result']} — {headers['event']}")
    for snap in snaps:
        print(f"\n[{snap['label']}]")
        print(snap["fen"])
        for file_name in "abcdefgh":
            occ = snap["files"][file_name]
            if occ != "empty":
                print(f"  {file_name}-file: {occ}")


if __name__ == "__main__":
    main()
