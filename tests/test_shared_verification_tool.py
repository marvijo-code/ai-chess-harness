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
sys.path.insert(0, str(ROOT / "tools"))
import player_tools  # noqa: E402

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


ROOK_G5 = "6r1/ppp1k3/2p2p2/3b2r1/7P/3P1B2/PPP1K1P1/4R2R b - - 0 26"


def run_tool(fen):
    payload = json.dumps({"fen": fen, "moves": [], "side_to_move": "black", "clocks": {}})
    proc = subprocess.run([sys.executable, str(TOOL)], input=payload, capture_output=True, text=True,
                          encoding="utf-8", timeout=60)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


class SummaryFirstTests(unittest.TestCase):
    """Only the start of a tool's output is shown, so the threat and the fix come first (2026-10-30)."""

    def setUp(self):
        self.out = run_tool(ROOK_G5)
        self.lines = self.out.splitlines()

    def test_threat_is_the_first_line(self):
        self.assertTrue(self.lines[0].startswith("Right now:"), self.lines[0])
        self.assertIn("your rook on g5", self.lines[0])
        self.assertIn("their pawn on h4", self.lines[0])

    def test_saving_moves_come_next(self):
        self.assertTrue(self.lines[1].startswith("Moves that keep the material:"), self.lines[1])
        for san in ("R5g7", "R5g6", "Rf5", "Rg3"):
            self.assertIn(san, self.lines[1])
        self.assertNotIn("Rg4", self.lines[1])

    def test_losing_moves_are_grouped_not_one_line_each(self):
        words = [line for line in self.lines if not line.startswith(("AVOID:", "Right now:", "Moves that keep"))]
        self.assertLessEqual(len(words), 8, words)     # 31 losing moves, a handful of lines
        rook_lost = [line for line in words if "rook on g5 would be captured by their pawn on h4" in line]
        self.assertTrue(rook_lost and "more" in rook_lost[0], words)

    def test_summary_fits_inside_the_prompt_cut(self):
        shown = player_tools.cut_output(self.out.strip())
        self.assertIn("Moves that keep the material:", shown)
        self.assertIn("Rg4: you would be about 2 pawns down", shown)

    def test_avoid_lines_carry_a_reason_for_each_move(self):
        board = chess.Board(ROOK_G5)
        result = player_tools.ToolResult("loose-piece-check", True, output=self.out)
        flagged = player_tools.flagged_moves([result], board)
        self.assertIn("rook on g4 would be captured by their bishop on f3", flagged["g5g4"])
        self.assertIn("rook on g5 would be captured by their pawn on h4", flagged["g8h8"])
        for safe in ("g5g7", "g5g6", "g5f5", "g5g3", "d5f3"):
            self.assertNotIn(safe, flagged)

    def test_no_threat_line_when_nothing_is_attacked(self):
        out = run_tool(chess.STARTING_FEN)
        self.assertEqual(out.strip(), "No move here loses material once the captures are played out.")

    def test_fits_the_tool_size_cap(self):
        self.assertLessEqual(len(TOOL.read_bytes()), player_tools.TOOL_MAX_BYTES)


if __name__ == "__main__":
    unittest.main()
