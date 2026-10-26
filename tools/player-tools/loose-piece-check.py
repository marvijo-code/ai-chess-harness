"""Name the moves that would lose material, including defended captures.

Reads {"fen", "moves", "side_to_move", "clocks"} on stdin and prints sentences.
Pure python-chess: no engine, no tablebase, no network.

Why this tool exists: counting attackers against defenders is not enough. A move
can land on a square that is "defended" and still lose material, because the
opponent may take with a cheaper piece and win the exchange. This tool plays the
capture sequence out (cheapest attacker first, both sides) and reports the final
material swing, so the player never has to do that sum in its head.
"""
import json
import sys

import chess

WORTH = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 0}
NAME = {chess.PAWN: "pawn", chess.KNIGHT: "knight", chess.BISHOP: "bishop",
        chess.ROOK: "rook", chess.QUEEN: "queen", chess.KING: "king"}


def exchange_gain(board, square, side):
    """Best net material gain for `side` if it starts capturing on `square`.

    Both sides may decline: a side keeps capturing only when it gains, so this
    is the standard static exchange value, not a forced sequence.
    """
    captures = [m for m in board.legal_moves if m.to_square == square and board.turn == side]
    if not captures:
        return 0
    best = 0
    for move in captures:
        taken = board.piece_at(square)
        gain = WORTH[taken.piece_type] if taken else 0
        board.push(move)
        reply = exchange_gain(board, square, not side)
        board.pop()
        best = max(best, gain - reply)
    return best


def offender(board, move, mine):
    """What this move costs in our own material, played out to a quiet capture state."""
    board = board.copy()
    taken_first = board.piece_at(move.to_square)          # read before the push
    board.push(move)
    worst = 0
    detail = ""
    for square in chess.SQUARES:
        piece = board.piece_at(square)
        if piece is None or piece.color != mine:
            continue
        if not board.attackers(not mine, square):
            continue
        loss = exchange_gain(board, square, not mine)
        if loss > worst:
            worst = loss
            attackers = ", ".join(sorted(f"their {NAME[board.piece_at(s).piece_type]} on {chess.square_name(s)}"
                                         for s in board.attackers(not mine, square)))
            detail = (f"your {NAME[piece.piece_type]} on {chess.square_name(square)} "
                      f"would be captured by {attackers}")
    if worst <= 0:
        return ""
    # Our move may have captured something first, so report the net swing.
    net = worst - (WORTH[taken_first.piece_type] if taken_first else 0)
    if net <= 0:
        return ""
    return (f"you would be about {net:.0f} pawns down there, because {detail} "
            "and the captures do not win the material back")


def main():
    payload = json.load(sys.stdin)
    board = chess.Board(payload["fen"])
    mine = board.turn
    risky = []
    for move in board.legal_moves:
        san = board.san(move)
        note = offender(board, move, mine)
        if note:
            risky.append((san, note))
    if risky:
        print("These moves lose material once the captures are played out, so avoid them:")
        for san, note in risky:
            print(f"  {san}: {note}.")
    else:
        print("No move here loses material once the captures are played out.")


if __name__ == "__main__":
    main()
