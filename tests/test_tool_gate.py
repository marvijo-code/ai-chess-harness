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
        flagged = pt.flagged_moves(block.results, self.board)
        self.assertIn("f5g7", flagged)
        self.assertIn("queen on d7", flagged["f5g7"])
        self.assertNotIn("g8h8", flagged)

    def test_refusal_is_recorded_as_gated_not_invalid(self):
        client = self.make(["f5g7", "g8h8"])
        client.choose_move(self.board, {"wtime": 600000, "btime": 600000}, [])
        self.assertEqual(client.last_report["illegal"], ["gated"])

    def test_broken_reply_is_still_invalid(self):
        client = self.make(["nonsense", "g8h8"])
        client.choose_move(self.board, {"wtime": 600000, "btime": 600000}, [])
        self.assertNotIn("gated", client.last_report["illegal"])
        self.assertEqual(len(client.last_report["illegal"]), 1)


def tool(output, name="t"):
    return pt.ToolResult(name, True, output=output)


class ExactGateTests(unittest.TestCase):
    """flagged_moves flags a move only for a reason that is about THAT move (2026-10-30)."""

    def setUp(self):
        self.board = chess.Board()      # 1.Nf3 is g1f3, 1.e4 is e2e4, 1.d4 is d2d4

    def test_list_form_with_a_reason_about_another_move_flags_only_that_move(self):
        # The real failure: one long AVOID list under one example sentence. The old code gave
        # that sentence to every listed move ("refused Re3 because of Qb3").
        out = tool("Nf3: reply e5 wins the knight on f3 (pin/fork, no fix)\nAVOID: g1f3 e2e4 d2d4")
        flagged = pt.flagged_moves([out], self.board)
        self.assertEqual(list(flagged), ["g1f3"])
        self.assertIn("Nf3: reply e5", flagged["g1f3"])

    def test_list_form_under_a_count_line_flags_nothing(self):
        out = tool("Legal move count: 20\nAVOID: g1f3 e2e4")
        self.assertEqual(pt.flagged_moves([out], self.board), {})

    def test_per_move_form_flags_with_its_own_reason(self):
        out = tool("Two checks below.\nAVOID: e2e4 - loses the bishop on the long diagonal\n"
                   "AVOID: d2d4: drops the c4 pawn")
        flagged = pt.flagged_moves([out], self.board)
        self.assertEqual(flagged, {"e2e4": "loses the bishop on the long diagonal", "d2d4": "drops the c4 pawn"})

    def test_per_move_form_may_share_one_reason(self):
        out = tool("AVOID: e2e4 d2d4 - about 1 pawn down, your pawn would be captured")
        flagged = pt.flagged_moves([out], self.board)
        self.assertEqual(set(flagged), {"e2e4", "d2d4"})
        self.assertEqual(flagged["e2e4"], "about 1 pawn down, your pawn would be captured")

    def test_san_led_reason_line_flags_with_that_line(self):
        out = tool("Checked 20 moves.\n  - e4: the pawn on e4 hangs after the knight jump\n"
                   "* Nc3+ drops the knight to b4\nAVOID: e2e4 b1c3 d2d4")
        flagged = pt.flagged_moves([out], self.board)
        self.assertEqual(set(flagged), {"e2e4", "b1c3"})
        self.assertEqual(flagged["e2e4"], "e4: the pawn on e4 hangs after the knight jump")
        self.assertEqual(flagged["b1c3"], "Nc3+ drops the knight to b4")

    def test_line_naming_several_moves_flags_each(self):
        out = tool("Nf3, Nc3 and e4: each walks into a fork that wins a piece\nAVOID: g1f3 b1c3 e2e4 d2d4")
        flagged = pt.flagged_moves([out], self.board)
        self.assertEqual(set(flagged), {"g1f3", "b1c3", "e2e4"})

    def test_reason_must_start_with_the_move_not_mention_it(self):
        out = tool("After e4 the knight is fine\nAVOID: g1f3")
        self.assertEqual(pt.flagged_moves([out], self.board), {})

    def test_without_a_board_only_uci_led_lines_and_per_move_form_count(self):
        out = tool("Nf3: loses a tempo\ne2e4 drops the pawn\nAVOID: g1f3 e2e4\nAVOID: d2d4 - drops the c4 pawn")
        flagged = pt.flagged_moves([out])
        self.assertEqual(set(flagged), {"e2e4", "d2d4"})
        self.assertEqual(flagged["e2e4"], "e2e4 drops the pawn")

    def test_reason_lines_come_from_the_same_tool_only(self):
        first = tool("Nf3: loses the knight (another tool)", name="a")
        second = tool("AVOID: g1f3", name="b")
        self.assertEqual(pt.flagged_moves([first, second], self.board), {})

    def test_reason_without_lost_material_flags_nothing(self):
        # 2026-10-30, Sonnet v SF4 on v8: pawn-recapture-wreck refused 5.Nc3 and 23.Ng4, both the
        # best move, because "a pawn recapture doubles my pawns". Advice is not a loss.
        out = tool("Nc3 leaves my N capturable by d5c3 with only a pawn recapture that doubles my pawns\n"
                   "Nf3: loses a tempo\nAVOID: b1c3 g1f3\nAVOID: e2e4 - weakens d4\nAVOID: d2d4: leaves c4 weak")
        self.assertEqual(pt.flagged_moves([out], self.board), {})

    def test_mate_reason_flags(self):
        out = tool("AVOID: f2f3 - Qh4# follows\nAVOID: g2g4: walks into mate")
        self.assertEqual(set(pt.flagged_moves([out], self.board)), {"f2f3", "g2g4"})

    def test_says_material_loss(self):
        for text in ("about 3 pawns down, your knight on f3 would be captured", "reply Qxh3+ wins a piece on b3",
                     "hangs the queen", "gives up the exchange", "Qh4# next"):
            self.assertTrue(pt.says_material_loss(text), text)
        for text in ("doubles my pawns", "loses a tempo", "weakens the king", "Legal move count: 20",
                     "Nc3 leaves my N capturable by d5c3 with only a pawn recapture that doubles my pawns"):
            self.assertFalse(pt.says_material_loss(text), text)

    def test_failed_tool_flags_nothing(self):
        broken = pt.ToolResult("t", False, output="AVOID: e2e4 - loses a pawn", error="exit 1")
        self.assertEqual(pt.flagged_moves([broken], self.board), {})


if __name__ == "__main__":
    unittest.main()
