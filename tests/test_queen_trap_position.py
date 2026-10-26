"""The round 2 queen trap (2026-10-26): 38...Ng7?? walks into Qxd7.

Position after 37.Qg4+ (DeepSeek as Black, game 2 of the 3-game series):
the knight on f5 is the only piece between the white queen on g4 and the black
queen on d7. Moving it loses the queen to Qxd7; Kh8 is safe.

The unit tests pin the facts and the shared tool. The integration test runs a
real tournament model on this exact position and is skipped unless
RUN_LLM_LIVE=1 (it spends subscription time, so it never runs by accident).
"""
import json
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
# The position alone is enough to test the trap; GAME_HISTORY reproduces the context
# it actually arrived in (round 2, after 37 moves, Black's clock at 12:15).
HISTORY = []
GAME_HISTORY = ["d2d4", "g8f6", "c2c4", "e7e6", "g1f3", "b7b6", "g2g3", "c8b7", "f1g2", "f8e7",
                "e1g1", "e8g8", "b1c3", "f6e4", "c1d2", "e7f6", "h2h4", "d7d5", "c4d5", "e4c3",
                "d2c3", "e6d5", "b2b4", "b8c6", "e2e3", "c6e7", "f3e5", "e7c6", "b4b5", "c6e7",
                "e5g4", "e7f5", "d1f3", "d8d7", "g4f6", "g7f6", "f3g4"]
GAME_BLACK_CLOCK_MS = 735017
LIVE_RUNS = 3
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


def _queen_loss(board_after: chess.Board) -> int:
    return loses_the_queen(board_after)


def _verdict(move_uci: str) -> tuple[str, str]:
    board = chess.Board(FEN)
    if not move_uci or move_uci == "0000":
        return "no reply", "provider failed or forfeited"
    try:
        move = chess.Move.from_uci(move_uci)
    except ValueError:
        return "bad reply", "not a move"
    if move not in board.legal_moves:
        return "bad reply", "illegal"
    san = board.san(move)
    board.push(move)
    loss = _queen_loss(board)
    if loss >= 5:
        return "LOST QUEEN", f"{san} (-{loss})"
    if loss >= 2:
        return "lost material", f"{san} (-{loss})"
    return "safe", san


@unittest.skipUnless(os.environ.get("RUN_LLM_LIVE") == "1",
                     "live model test: set RUN_LLM_LIVE=1 (spends subscription or API credit)")
class QueenTrapLiveTests(unittest.TestCase):
    """The trap position, in its real context, RUNS times on one real model.

    Skipped by default so the suite never bills anyone. Run at will:

        RUN_LLM_LIVE=1 TRAP_PROVIDER=claude TRAP_MODEL=claude-haiku-5-5 python -m pytest tests/test_queen_trap_position.py -q

    It asserts the harness (a legal reply, the shared tool ran). The model's choice
    is recorded, not asserted: the point is to measure which models walk into it.
    """

    provider = os.environ.get("TRAP_PROVIDER", "opencode-go")
    model = os.environ.get("TRAP_MODEL", "deepseek-v4.1-flash")

    def test_three_contextual_runs(self):
        sys.path.insert(0, str(ROOT / "tools"))
        sys.path.insert(0, str(ROOT / "engines" / "llm-chess-engine"))
        import subscription_providers as providers

        results = []
        for run in range(1, LIVE_RUNS + 1):
            client = providers.SubscriptionChessClient(self.provider, lambda _m: None)
            client.tools_dir = None                      # shared tools only, as in a game
            client.model = self.model
            client.effort = "high"
            move, comment = client.choose_move(
                chess.Board(FEN),
                {"wtime": 600000, "btime": GAME_BLACK_CLOCK_MS, "winc": 10000, "binc": 10000},
                GAME_HISTORY)
            verdict, detail = _verdict(move)
            tools_run = [t["name"] for t in client.last_report.get("tools", [])]
            results.append({"run": run, "move": move, "verdict": verdict, "detail": detail,
                            "tools": tools_run, "comment": (comment or "")[:200]})
            # A dead route is not a chess result: skip with the provider's own words.
            if move == "0000" and providers.provider_unavailable(comment or ""):
                self.skipTest(f"{self.provider} {self.model} is unavailable: {(comment or '')[:140]}")
            self.assertNotEqual(move, "0000", f"{self.model} run {run} forfeited instead of answering: {comment[:160]}")
            self.assertIn("loose-piece-check", tools_run, f"tools did not run on run {run}")

        out = ROOT / "out" / "validation" / f"queen-trap-{self.model.replace('/', '-')}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"provider": self.provider, "model": self.model,
                                   "history_plies": len(GAME_HISTORY), "clock_ms": GAME_BLACK_CLOCK_MS,
                                   "results": results}, indent=2), encoding="utf-8")

        lost = [r for r in results if r["verdict"] == "LOST QUEEN"]
        print(f"\n{self.model}: {len(lost)}/{len(results)} lost the queen -> {out}")
        for r in results:
            print(f"  run {r['run']}: {r['verdict']:12s} {r['detail']}")


if __name__ == "__main__":
    unittest.main()
