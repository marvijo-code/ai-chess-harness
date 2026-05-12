import asyncio
import importlib.util
import unittest
from pathlib import Path

import chess

from tools.mirror_fastchess_live_pgn import build_game


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class TimeLossHandlingTests(unittest.TestCase):
    def test_codex_engine_does_not_start_model_turn_when_clock_is_expired(self):
        module = load_module("codex_chess_uci_timeout_test", ROOT / "engines" / "codex-chess" / "codex_chess_uci.py")
        client = module.CodexAppServer("gpt-test", "low")

        async def fail_start():
            raise AssertionError("start should not be called when the clock is expired")

        client.start = fail_start

        move = asyncio.run(client.choose_move(chess.Board(), {"wtime": 0, "btime": 300000}, []))

        self.assertEqual(move, "0000")

    def test_openrouter_engine_does_not_call_api_when_clock_is_expired(self):
        module = load_module("llm_chess_uci_timeout_test", ROOT / "engines" / "llm-chess-engine" / "llm_chess_uci.py")
        client = module.OpenRouterChessClient()

        def fail_post(*args, **kwargs):
            raise AssertionError("_post should not be called when the clock is expired")

        client._post = fail_post

        move, reason = client.choose_move(chess.Board(), {"wtime": 0, "btime": 300000}, [])

        self.assertEqual(move, "0000")
        self.assertIn("clock expired", reason)

    def test_live_mirror_writes_timeout_winner_and_reason(self):
        current = {
            "game": 1,
            "white": "Codex-chess",
            "black": "Codex-chess-learner",
            "result": "*",
            "reason": "",
            "finished": False,
        }
        state = {
            "moves": [],
            "wtime": 0,
            "btime": 180000,
            "updated_at": 123456789,
            "running_side": "White",
        }

        game = build_game(current, state)

        self.assertEqual(game.headers["Result"], "0-1")
        self.assertIn("Codex-chess (White) lost on time", game.headers["Termination"])
        self.assertEqual(game.headers["ClockRunningSide"], "")


if __name__ == "__main__":
    unittest.main()
