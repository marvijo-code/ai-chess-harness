"""Focused tests for the match-PGN to tournament-state bridge (PRD 196, Validation 92)."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import tourney_state_bridge as bridge  # noqa: E402

PGN = """[Event "LLM Match: A vs B"]
[Site "chess-harness-codex"]
[White "DeepSeek V4.1 Flash"]
[Black "Stockfish 19 (depth 4)"]
[Result "0-1"]
[Termination "checkmate"]
[TimeControl "600+10"]
[WhiteClockMs "341476"]
[BlackClockMs "169893"]

1. e4 e5 2. Nf3 Nc6 0-1
"""

LIVE = PGN.replace('[Result "0-1"]', '[Result "*"]').replace("0-1\n", "*\n")


def write(tmp: Path, text: str, name: str = "m-live.pgn") -> Path:
    path = tmp / name
    path.write_text(text, encoding="utf-8")
    return path


class BridgeTests(unittest.TestCase):
    def test_finished_game_maps_to_tournament_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = bridge.build_state(write(Path(tmp), PGN), "m")
        game = state["games"]["r1b1"]
        self.assertEqual(game["status"], "finished")
        self.assertEqual(game["result"], "0-1")
        self.assertEqual(len(game["moves"]), 4)
        self.assertEqual(game["moves"][0]["san"], "e4")
        self.assertEqual(game["clocks"], {"white": 341476, "black": 169893})
        self.assertEqual(state["winner"], "Stockfish 19 (depth 4)")
        self.assertTrue(state["finished"])

    def test_pairings_are_present_so_the_page_draws_a_board(self):
        # Regression: without pairings the tournament page shows "Waiting for pairings".
        with tempfile.TemporaryDirectory() as tmp:
            state = bridge.build_state(write(Path(tmp), PGN), "m")
        round_one = state["rounds"][0]
        self.assertEqual(round_one["pairings"][0]["game_id"], "r1b1")
        self.assertEqual(round_one["pairings"][0]["white"], "DeepSeek V4.1 Flash")

    def test_every_field_the_page_renders_is_present(self):
        # Undefined/NaN on screen = a field this bridge forgot.
        with tempfile.TemporaryDirectory() as tmp:
            state = bridge.build_state(write(Path(tmp), PGN), "m")
        for row in state["standings"]:
            for key in ("name", "rank", "points", "played", "elo", "elo_delta",
                        "wins", "draws", "losses", "forfeits", "flags", "invalid_attempts"):
                self.assertIn(key, row)
                self.assertIsNotNone(row[key])
        for key in ("timeControlMs", "incrementMs", "maxAttempts", "startElo", "eloK", "rounds"):
            self.assertIn(key, state["config"])
        for move in state["games"]["r1b1"]["moves"]:
            for key in ("ply", "side", "san", "uci", "tries", "elapsed_ms", "comment"):
                self.assertIn(key, move)

    def test_live_game(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = bridge.build_state(write(Path(tmp), LIVE), "m")
        self.assertEqual(state["games"]["r1b1"]["status"], "live")
        self.assertFalse(state["finished"])
        self.assertEqual(state["standings"][0]["played"], 0)

    def test_archive_extras_are_picked_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            pgn = write(tmp, PGN)
            (tmp / "m.json").write_text(
                '{"games":[{"moves":[{"ply":1,"uci":"e2e4","elapsed_ms":2171,"tries":1,'
                '"comment":"king pawn"}],"invalid_attempts":{"white":2,"black":0}}]}', encoding="utf-8")
            state = bridge.build_state(pgn, "m")
        move = state["games"]["r1b1"]["moves"][0]
        self.assertEqual(move["elapsed_ms"], 2171)
        self.assertEqual(move["comment"], "king pawn")
        row = [r for r in state["standings"] if r["name"] == "DeepSeek V4.1 Flash"][0]
        self.assertEqual(row["invalid_attempts"], 2)

    def test_missing_or_empty_file_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(bridge.build_state(Path(tmp) / "nope.pgn", "m"))
            empty = Path(tmp) / "empty.pgn"
            empty.write_text("   ", encoding="utf-8")
            self.assertIsNone(bridge.build_state(empty, "m"))

    def test_write_state_is_readable_and_atomic(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = bridge.build_state(write(Path(tmp), PGN), "m")
            target = Path(tmp) / "m-tournament.json"
            bridge.write_state(state, target)
            import json
            self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["id"], "m")


if __name__ == "__main__":
    unittest.main()
