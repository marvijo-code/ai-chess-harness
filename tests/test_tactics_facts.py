"""Focused tests for the harness-computed tactics facts block (PRD 184, Validation 85).

The headline case replays the exact pre-blunder position from the 2026-10-26
DeepSeek-vs-SF4 diagnostic game: 25.Nxf7?? hung the knight (+11 -> -511).
The facts block must flag that landing square while staying silent on safe moves.
"""
import os
import sys
import unittest
from pathlib import Path

import chess

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engines" / "llm-chess-engine"))
import subscription_providers as sp  # noqa: E402

# Diagnostic game up to 24...h6 (the position DeepSeek faced before 25.Nxf7??).
BLUNDER_GAME = ["e2e4", "e7e5", "g1f3", "b8c6", "f1b5", "g8f6", "e1g1", "f6e4",
                "d2d4", "e4d6", "b5c6", "d7c6", "d4e5", "d6f5", "d1d8", "e8d8",
                "b1c3", "f8e7", "f1d1", "d8e8", "c1g5", "e7g5", "f3g5", "h7h6"]


def play(ucis):
    board = chess.Board()
    for uci in ucis:
        board.push_uci(uci)
    return board


class TacticsFactsTests(unittest.TestCase):
    def test_blunder_landing_square_is_flagged(self):
        board = play(BLUNDER_GAME)
        facts = sp.tactics_facts(board)
        nxf7 = [line for line in facts.splitlines() if line.strip().startswith("Nxf7 ")]
        self.assertEqual(len(nxf7), 1)
        self.assertIn("takes bP", nxf7[0])
        self.assertIn("HANGS", nxf7[0])

    def test_safe_alternative_is_not_flagged(self):
        board = play(BLUNDER_GAME)
        facts = sp.tactics_facts(board)
        nge4 = [line for line in facts.splitlines() if line.strip().startswith("Nge4 ")]
        self.assertEqual(len(nge4), 1)
        self.assertNotIn("HANGS", nge4[0])

    def test_hanging_knight_named_in_pieces_section(self):
        board = play(BLUNDER_GAME)
        facts = sp.tactics_facts(board)
        self.assertIn("Hanging pieces", facts)
        hanging = [line for line in facts.splitlines() if "wNg5" in line]
        self.assertTrue(any("HANGING" in line for line in hanging),
                        f"wNg5 lines: {hanging}")

    def test_startpos_has_no_hanging_section(self):
        facts = sp.tactics_facts(chess.Board())
        self.assertNotIn("Hanging pieces", facts)
        self.assertNotIn("HANGS", facts)
        self.assertIn("Legal-move landing safety (20 moves):", facts)

    def test_check_and_capture_flags(self):
        board = play(["e2e4", "e7e5", "d1h5", "b8c6", "f1c4", "g8f6"])
        facts = sp.tactics_facts(board)
        mate = [line for line in facts.splitlines() if line.strip().startswith("Qxf7# ")]
        self.assertEqual(len(mate), 1)
        self.assertIn("gives check", mate[0])

    def test_prompt_carries_facts_and_verify_line(self):
        board = play(BLUNDER_GAME)
        prompt = sp.build_prompt(board, {"wtime": 600000, "btime": 600000}, BLUNDER_GAME, [])
        self.assertIn("Computed tactics facts", prompt)
        self.assertIn("HANGS", prompt)
        self.assertIn("never leave a piece hanging", prompt)

    def test_facts_use_no_engine_only_rules(self):
        # The facts block must never cheat: no engine process, no search, no
        # scores, no best-move hints - only python-chess rules (attackers,
        # check detection) plus the static material table. Static values like
        # "takes P" name piece types, never centipawn evaluations.
        import re
        source = Path(sp.__file__).read_text(encoding="utf-8")
        for banned in ("chess.engine", "SimpleEngine", "popen", "stockfish",
                       "evaluate", "analyse", "analyze("):
            self.assertNotIn(banned, source,
                             f"engine/analysis hook {banned!r} has no place in the move prompt path")
        board = play(BLUNDER_GAME)
        facts = sp.tactics_facts(board)
        self.assertNotRegex(facts, r"[+-]?\d{3,}",
                            "no centipawn-style numbers allowed in facts")
        self.assertNotIn("best", facts.lower())
        self.assertNotIn("score", facts.lower())
        self.assertNotIn("??", facts)
        self.assertNotIn("!!", facts)

    def test_kill_switch_removes_facts(self):
        board = play(BLUNDER_GAME)
        old = os.environ.get("LLM_TACTICS_FACTS")
        os.environ["LLM_TACTICS_FACTS"] = "0"
        try:
            prompt = sp.build_prompt(board, {"wtime": 600000, "btime": 600000}, BLUNDER_GAME, [])
        finally:
            if old is None:
                del os.environ["LLM_TACTICS_FACTS"]
            else:
                os.environ["LLM_TACTICS_FACTS"] = old
        self.assertNotIn("Computed tactics facts", prompt)
        self.assertNotIn("never leave a piece hanging", prompt)


if __name__ == "__main__":
    unittest.main()
