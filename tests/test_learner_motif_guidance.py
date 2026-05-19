import importlib.util
import sys
import unittest
from pathlib import Path

import chess


ROOT = Path(__file__).resolve().parents[1]


def load_uci_module():
    path = ROOT / "engines" / "codex-chess" / "codex_chess_uci.py"
    spec = importlib.util.spec_from_file_location("codex_chess_uci_motif_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class LearnerMotifGuidanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.uci = load_uci_module()

    def test_learner_motif_warnings_are_advisory_and_legal_move_bounded(self):
        board = chess.Board()
        legal = [move.uci() for move in board.legal_moves]

        warnings = self.uci.learner_advisory_motif_warnings(board, legal)

        by_move = {warning["uci"]: warning for warning in warnings}
        self.assertIn("b1a3", by_move)
        self.assertEqual(by_move["b1a3"]["policy"], "learner_advisory_only")
        self.assertTrue({warning["uci"] for warning in warnings}.issubset(set(legal)))


if __name__ == "__main__":
    unittest.main()
