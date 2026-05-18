from __future__ import annotations

import argparse
import json

import chess

PIECE_VALUES = {
    chess.PAWN: 100,
    chess.KNIGHT: 300,
    chess.BISHOP: 300,
    chess.ROOK: 500,
    chess.QUEEN: 900,
    chess.KING: 0,
}


def material_balance(board: chess.Board, color: bool) -> int:
    total = 0
    for piece in board.piece_map().values():
        value = PIECE_VALUES.get(piece.piece_type, 0)
        total += value if piece.color == color else -value
    return total


def audit(fen: str) -> dict:
    board = chess.Board(fen)
    color = board.turn
    checks = []
    captures = []
    risky = []
    before = material_balance(board, color)
    for move in board.legal_moves:
        san = board.san(move)
        after = board.copy(stack=False)
        is_capture = board.is_capture(move)
        gives_check = board.gives_check(move)
        after.push(move)
        reply_swing = 0
        for reply in after.legal_moves:
            reply_board = after.copy(stack=False)
            reply_board.push(reply)
            reply_swing = max(reply_swing, before - material_balance(reply_board, color))
        row = {"uci": move.uci(), "san": san, "reply_material_swing_cp": reply_swing}
        if gives_check:
            checks.append(row)
        if is_capture:
            captures.append(row)
        if reply_swing >= 300:
            risky.append(row)
    return {
        "fen": fen,
        "side_to_move": "white" if color == chess.WHITE else "black",
        "legal_moves": board.legal_moves.count(),
        "checks": checks[:12],
        "captures": captures[:12],
        "risky_moves": risky[:12],
        "policy": "Current-position feature audit only. This tool does not choose a move and contains no opening book, tablebase, or engine labels.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit current-position concept features without choosing a move.")
    parser.add_argument("--fen", required=True)
    args = parser.parse_args()
    print(json.dumps(audit(args.fen), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
