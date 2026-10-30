"""Name the moves that would lose material, including defended captures.

Reads {"fen", "moves", "side_to_move", "clocks"} on stdin and prints sentences.
Pure python-chess: no engine, no tablebase, no network.

Why this tool exists: counting attackers against defenders is not enough. A move
can land on a square that is "defended" and still lose material, because the
opponent may take with a cheaper piece and win the exchange. This tool plays the
capture sequence out (cheapest attacker first, both sides) and reports the final
material swing, so the player never has to do that sum in its head.

Output is summary first, because only the start of it is shown: a piece that is
already lost (hanging, or attacked by a cheaper piece), then the moves that keep
the material, then a short grouped list of losing moves. Each losing group ends
with an `AVOID: <uci moves> - <reason>` line for the harness gate.
"""
import json
import sys

import chess

WORTH = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 0}
NAME = {chess.PAWN: "pawn", chess.KNIGHT: "knight", chess.BISHOP: "bishop",
        chess.ROOK: "rook", chess.QUEEN: "queen", chess.KING: "king"}
SHOW_SAFE = 12      # moves listed by name in the "keep the material" line
SHOW_GROUPS = 5     # losing groups listed in words


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


def pawns(n):
    return f"about {n:.0f} pawn" + ("" if n == 1 else "s")


def takers(board, square, mine):
    return ", ".join(sorted(f"their {NAME[board.piece_at(s).piece_type]} on {chess.square_name(s)}"
                            for s in board.attackers(not mine, square)))


def worst_loss(board, mine):
    """(pawns lost, the piece words, who takes it) for the worst exchange against `mine`; opponent to move."""
    worst, piece_words, who = 0, "", ""
    for square in chess.SQUARES:
        piece = board.piece_at(square)
        if piece is None or piece.color != mine or not board.attackers(not mine, square):
            continue
        loss = exchange_gain(board, square, not mine)
        if loss > worst:
            worst = loss
            piece_words = f"your {NAME[piece.piece_type]} on {chess.square_name(square)}"
            who = takers(board, square, mine)
    return worst, piece_words, who


def move_loss(board, move, mine):
    """(net pawns lost, reason words) for one move, played out to a quiet capture state."""
    board = board.copy()
    taken_first = board.piece_at(move.to_square)          # read before the push
    board.push(move)
    worst, piece_words, who = worst_loss(board, mine)
    # Our move may have captured something first, so report the net swing.
    net = worst - (WORTH[taken_first.piece_type] if taken_first else 0)
    if worst <= 0 or net <= 0:
        return 0, ""
    return net, f"{piece_words} would be captured by {who}"


def offender(board, move, mine):
    """What this move costs in our own material, as one sentence ("" when nothing is lost)."""
    net, detail = move_loss(board, move, mine)
    if net <= 0:
        return ""
    return (f"you would be {pawns(net)} down there, because {detail} "
            "and the captures do not win the material back")


def threats_now(board, mine):
    """Own pieces already lost if we do nothing: hanging, or attacked by a cheaper piece."""
    if board.is_check():
        return []                     # the check must be answered first; the move list covers it
    after = board.copy()
    after.push(chess.Move.null())
    out = []
    for square in chess.SQUARES:
        piece = after.piece_at(square)
        if piece is None or piece.color != mine or piece.piece_type == chess.KING:
            continue
        if not after.attackers(not mine, square):
            continue
        loss = exchange_gain(after, square, not mine)
        if loss > 0:
            how = "nothing defends it" if not after.attackers(mine, square) else "the taker is worth less"
            out.append(f"your {NAME[piece.piece_type]} on {chess.square_name(square)} can be taken by "
                       f"{takers(after, square, mine)} ({how}), {pawns(loss)} lost if you do nothing")
    return out


def main():
    payload = json.load(sys.stdin)
    board = chess.Board(payload["fen"])
    mine = board.turn
    safe, groups = [], {}
    for move in board.legal_moves:
        san = board.san(move)
        net, detail = move_loss(board, move, mine)
        if net <= 0:
            safe.append(san)
        else:
            groups.setdefault((net, detail), []).append((san, move.uci()))
    threats = threats_now(board, mine)
    if threats:
        print("Right now: " + "; ".join(threats) + ".")
        if safe:
            more = f" and {len(safe) - SHOW_SAFE} more" if len(safe) > SHOW_SAFE else ""
            print("Moves that keep the material: " + ", ".join(safe[:SHOW_SAFE]) + more + ".")
        else:
            print("No move keeps all the material.")
    if not groups:
        print("No move here loses material once the captures are played out.")
        return
    ordered = sorted(groups.items(), key=lambda item: (-item[0][0], item[0][1]))
    count = sum(len(moves) for moves in groups.values())
    print(f"{count} moves lose material once the captures are played out, so avoid them:")
    for (net, detail), moves in ordered[:SHOW_GROUPS]:
        sans = [san for san, _uci in moves]
        named = ", ".join(sans[:6]) + (f" and {len(sans) - 6} more" if len(sans) > 6 else "")
        print(f"  {named}: you would be {pawns(net)} down there, because {detail}.")
    if len(ordered) > SHOW_GROUPS:
        print(f"  plus {len(ordered) - SHOW_GROUPS} smaller groups of losing moves.")
    # Machine-readable verdict for the harness gate: one line per group, with the reason
    # that group loses. The engine refuses a reply whose move is listed here, once.
    for (net, detail), moves in ordered:
        print(f"AVOID: {' '.join(uci for _san, uci in moves)} - {pawns(net)} down, {detail}")


if __name__ == "__main__":
    main()
