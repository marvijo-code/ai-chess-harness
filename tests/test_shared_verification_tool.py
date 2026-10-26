"""Tests for the shared verification tool (PRD 197, Validation 93).

The tool's job is to be right about material and readable by a model: it must
catch a piece left en prise, a defended-but-losing capture, and the 2026-10-26
Nxf7 case where the king recapture only becomes legal after the defender leaves.
"""
import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path

import chess

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "player-tools" / "loose-piece-check.py"

spec = importlib.util.spec_from_file_location("loose_piece_check", TOOL)
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)

MOVE_10 = "r1bqr1k1/ppp1bppp/2pn4/4N3/8/5Q2/PPPP1PPP/RNB1R1K1 w - - 5 10"


class SharedToolTests(unittest.TestCase):
    def test_exists_in_the_shared_folder(self):
        self.assertTrue(TOOL.is_file(), f"missing shared tool: {TOOL}")
        self.assertTrue((ROOT / "tools" / "player-tools" / "README.md").is_file())

    def test_catches_the_king_recapture_after_the_defender_leaves(self):
        # 10.Nxf7 is not a free pawn: Black's knight recaptures, and only then is
        # Kxf7 legal. The tool must see the whole sequence.
        board = chess.Board(MOVE_10)
        note = tool.offender(board, board.parse_san("Nxf7"), chess.WHITE)
        self.assertTrue(note)
        self.assertIn("pawns down", note)
        self.assertIn("knight on f7", note)

    def test_catches_a_defended_but_losing_queen_capture(self):
        board = chess.Board(MOVE_10)
        note = tool.offender(board, board.parse_san("Qxf7+"), chess.WHITE)
        self.assertTrue(note)
        self.assertGreaterEqual(int("".join(c for c in note.split("about ")[1].split(" ")[0] if c.isdigit())), 6)

    def test_quiet_move_is_not_flagged(self):
        board = chess.Board(MOVE_10)
        self.assertEqual(tool.offender(board, board.parse_san("Nc3"), chess.WHITE)
                         if "Nc3" in [board.san(m) for m in board.legal_moves] else "", "")

    def test_output_is_plain_sentences(self):
        payload = json.dumps({"fen": MOVE_10, "moves": [], "side_to_move": "white", "clocks": {}})
        proc = subprocess.run([sys.executable, str(TOOL)], input=payload, capture_output=True, text=True,
                              encoding="utf-8", timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = proc.stdout
        self.assertIn("avoid them", out)
        self.assertIn("pawns down", out)
        self.assertIn("would be captured by", out)
        # readable words, not engine notation or centipawns
        self.assertNotIn("cp", out.split())
        self.assertNotIn("{", out)
        self.assertNotIn("??", out)


if __name__ == "__main__":
    unittest.main()
