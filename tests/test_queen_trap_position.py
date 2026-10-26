"""The round 2 queen trap (2026-10-26): 38...Ng7?? walks into Qxd7.

Position after 37.Qg4+ (DeepSeek as Black, game 2 of the 3-game series):
the knight on f5 is the only piece between the white queen on g4 and the black
queen on d7. Moving it loses the queen to Qxd7; Kh8 is safe.

The unit tests pin the facts and the shared tool. The integration test runs a
real tournament model on this exact position and is skipped unless
RUN_LLM_LIVE=1 (it spends subscription time, so it never runs by accident).
"""
import os
import sys
import unittest
from pathlib import Path

import chess

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "engines" / "llm-chess-engine"))

import player_tools as pt  # noqa: E402
import subscription_providers as sp  # noqa: E402

FEN = "r4rk1/pbpq1p1p/1p3p2/1P1p1n2/3P2QP/2B1P1P1/P4PB1/R4RK1 b - - 1 19"
HISTORY = []          # the position alone is enough: it is the trap, not the line
TRAP_MOVES = {"f5g7", "b7b5"}   # Ng7 (the blunder) and any knight hop off f5


def loses_the_queen(board_after: chess.Board) -> int:
    """Pawns of black material White wins, starting with the queen if it is en prise."""
    spec = __import__("importlib.util", fromlist=["util"]).spec_from_file_location(
        "lpc", ROOT / "tools" / "player-tools" / "loose-piece-check.py")
    lpc = __import__("importlib.util", fromlist=["util"]).module_from_spec(spec)
    spec.loader.exec_module(lpc)
    worst = 0
    for square in chess.SQUARES:
        piece = board_after.piece_at(square)
        if piece is None or piece.color != chess.BLACK:
            continue
        if not board_after.attackers(chess.WHITE, square):
            continue
        worst = max(worst, lpc.exchange_gain(board_after, square, chess.WHITE))
    return worst


class QueenTrapFactsTests(unittest.TestCase):
    def setUp(self):
        self.board = chess.Board(FEN)

    def test_it_is_black_to_move_and_in_check(self):
        self.assertEqual(self.board.turn, chess.BLACK)
        self.assertTrue(self.board.is_check())

    def test_the_knight_is_the_only_shield(self):
        after = self.board.copy()
        after.push_san("Kh8")
        self.assertEqual(list(after.attackers(chess.WHITE, chess.D7)), [],
                         "with the knight still on f5 the d7 queen is not attacked")

    def test_ng7_loses_the_queen(self):
        after = self.board.copy()
        after.push_san("Ng7")
        self.assertIn(chess.G4, after.attackers(chess.WHITE, chess.D7))
        self.assertEqual(loses_the_queen(after), 9)

    def test_kh8_is_safe(self):
        after = self.board.copy()
        after.push_san("Kh8")
        self.assertEqual(loses_the_queen(after), 0)

    def test_shared_tool_flags_the_trap_and_clears_kh8(self):
        report = pt.run_tools(None, self.board, HISTORY, include_shared=True).results[0]
        self.assertTrue(report.ok, report.error)
        flagged = [line for line in report.output.splitlines() if line.strip().startswith("Ng7")]
        self.assertTrue(flagged, report.output)
        self.assertIn("queen on d7", flagged[0])
        self.assertFalse([line for line in report.output.splitlines() if line.strip().startswith("Kh8")])

    def test_facts_alone_do_not_see_the_queen_danger(self):
        # Documents the known limit: the facts block describes the landing square,
        # so Ng7 reads as "1 attacker, 1 defender" and misses the exposed queen.
        # The material arithmetic lives in the shared tool, which does catch it.
        facts = sp.tactics_facts(self.board)
        line = [l for l in facts.splitlines() if l.strip().startswith("Ng7")]
        self.assertTrue(line)
        self.assertIn("their queen on g4", line[0])
        self.assertNotIn("queen on d7", line[0])
        report = pt.run_tools(None, self.board, HISTORY, include_shared=True).results[0]
        self.assertIn("queen on d7", report.output)


@unittest.skipUnless(os.environ.get("RUN_LLM_LIVE") == "1", "live model test: set RUN_LLM_LIVE=1")
class QueenTrapLiveTests(unittest.TestCase):
    """One real model on the trap position. Asserts the harness, reports the choice."""

    def test_model_reply_is_legal_and_tool_ran(self):
        provider = os.environ.get("TRAP_PROVIDER", "opencode-go")
        model = os.environ.get("TRAP_MODEL", "deepseek-v4.1-flash")
        client = sp.SubscriptionChessClient(provider, lambda _m: None)
        client.tools_dir = None
        move, _comment = client.choose_move(
            chess.Board(FEN), {"wtime": 600000, "btime": 600000, "winc": 10000, "binc": 10000}, HISTORY)
        self.assertNotEqual(move, "0000", "the model forfeited instead of answering")
        self.assertIn(chess.Move.from_uci(move), chess.Board(FEN).legal_moves)
        self.assertIn("loose-piece-check", [r["name"] for r in client.last_report.get("tools", [])])


if __name__ == "__main__":
    unittest.main()
