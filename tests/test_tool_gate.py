"""The tool gate: a reply the player's own tool flags is refused once, with the tool's reason.

Context: 2026-10-26, the round 2 trap. The shared tool printed that Ng7 loses the
queen and the model played it anyway. The gate is the harness refusing to accept a
reply that contradicts the player's own check.
"""
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


class FakeClient(sp.SubscriptionChessClient):
    """Answers with a scripted list of moves, so the gate can be tested without a model."""

    def __init__(self, moves):
        super().__init__("opencode-go", lambda _m: None)
        self.scripted = list(moves)
        self.prompts = []

    def _ask(self, prompt, timeout):
        self.prompts.append(prompt)
        move = self.scripted.pop(0) if self.scripted else "0000"
        return f'{{"move": "{move}", "comment": "scripted"}}'


class ToolGateTests(unittest.TestCase):
    def setUp(self):
        self.board = chess.Board(FEN)
        self.flagged = {"f5g7": "Ng7: you would be about 9 pawns down there, because your queen on d7 "
                                "would be captured by their queen on g4"}

    def make(self, moves):
        client = FakeClient(moves)
        client._flagged = dict(self.flagged)
        return client

    def test_flagged_reply_is_refused_and_the_retry_is_asked_again(self):
        client = self.make(["f5g7", "g8h8"])
        move, _comment = client.choose_move(self.board, {"wtime": 600000, "btime": 600000}, [])
        self.assertEqual(move, "g8h8", "the gate must make the model answer again")
        self.assertEqual(client.last_report["gated"], 1)
        self.assertEqual(len(client.prompts), 2)
        self.assertIn("loses material", client.prompts[1])

    def test_safe_reply_is_accepted_without_a_retry(self):
        client = self.make(["g8h8"])
        move, _ = client.choose_move(self.board, {"wtime": 600000, "btime": 600000}, [])
        self.assertEqual(move, "g8h8")
        self.assertEqual(client.last_report.get("gated", 0), 0)
        self.assertEqual(len(client.prompts), 1)

    def test_gate_fires_once_so_a_model_can_still_insist(self):
        # Refuse the first attempt; if it insists on the same move, the move stands.
        # The harness never picks a move for the player.
        client = self.make(["f5g7", "f5g7"])
        move, _ = client.choose_move(self.board, {"wtime": 600000, "btime": 600000}, [])
        self.assertEqual(move, "f5g7")
        self.assertEqual(client.last_report["gated"], 1)

    def test_no_flag_no_gate(self):
        client = self.make(["f5g7"])
        client._flagged = {}
        client._tools_text = lambda *a, **k: ""      # no tools this time, so nothing is flagged
        move, _ = client.choose_move(self.board, {"wtime": 600000, "btime": 600000}, [])
        self.assertEqual(move, "f5g7")
        self.assertEqual(client.last_report.get("gated", 0), 0)

    def test_new_game_clears_the_gate_memory(self):
        client = self.make(["f5g7", "f5g7", "f5g7"])
        client.choose_move(self.board, {"wtime": 600000, "btime": 600000}, [])
        client.new_game()
        self.assertEqual(client._flagged, {})
        self.assertEqual(client._gated, set())

    def test_shared_tool_emits_the_machine_readable_line(self):
        block = pt.run_tools(None, self.board, [])
        flagged = pt.flagged_moves(block.results)
        self.assertIn("f5g7", flagged)
        self.assertIn("queen on d7", flagged["f5g7"])
        self.assertNotIn("g8h8", flagged)


if __name__ == "__main__":
    unittest.main()
