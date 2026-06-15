import tempfile
import unittest
import json
from pathlib import Path
from unittest import mock

import chess
import chess.pgn

import tools.live_pgn_viewer as viewer
from tools.live_pgn_viewer import collect_master_wisdom_data, collect_stats, collect_zero_research_data, resolve_live_default_game_path, viewer_event_signatures


def write_finished_game(path: Path, event: str = "Synthetic game") -> None:
    game = chess.pgn.Game()
    game.headers["Event"] = event
    game.headers["White"] = "WhiteEngine"
    game.headers["Black"] = "BlackEngine"
    game.headers["Result"] = "1-0"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(game) + "\n", encoding="utf-8")


class LivePgnViewerStatsTests(unittest.TestCase):
    def test_collect_stats_excludes_live_and_backup_archives(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            out_dir = Path(temp_dir)
            write_finished_game(out_dir / "active.pgn", "Active")
            write_finished_game(out_dir / "live" / "active-live.pgn", "Live")
            write_finished_game(out_dir / "backups" / "games-reset" / "old.pgn", "Backup")

            stats = collect_stats(out_dir, None, None)

        self.assertEqual(stats["games"], 1)
        self.assertEqual([match["file"] for match in stats["matches"]], ["active.pgn"])

    def test_collect_stats_uses_headers_without_parsing_movetext(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            out_dir = Path(temp_dir)
            pgn_path = out_dir / "fastchess" / "headers-only.pgn"
            pgn_path.parent.mkdir(parents=True, exist_ok=True)
            pgn_path.write_text("placeholder", encoding="utf-8")
            returned = False

            def fake_read_headers(handle):
                nonlocal returned
                if returned:
                    return None
                returned = True
                return {
                    "Date": "2026.05.19",
                    "Round": "1",
                    "White": "HeaderWhite",
                    "Black": "HeaderBlack",
                    "Result": "1-0",
                }

            with (
                mock.patch("tools.live_pgn_viewer.chess.pgn.read_headers", side_effect=fake_read_headers),
                mock.patch("tools.live_pgn_viewer.chess.pgn.read_game", side_effect=AssertionError("movetext parsed")),
            ):
                stats = collect_stats(out_dir, None, None)

        self.assertEqual(stats["games"], 1)
        self.assertEqual(stats["matches"][0]["white"], "HeaderWhite")
        self.assertEqual(stats["matches"][0]["winner_label"], "HeaderWhite (White) won")

    def test_read_game_without_analysis_does_not_call_analyzer(self):
        class ExplodingAnalyzer:
            enabled = True

            def analyze(self, board):
                raise AssertionError("analysis blocked board load")

        with tempfile.TemporaryDirectory() as temp_dir:
            pgn_path = Path(temp_dir) / "live.pgn"
            write_finished_game(pgn_path, "Board only")

            data = viewer.read_game(pgn_path, ExplodingAnalyzer(), include_analysis=False)

        self.assertTrue(data["has_game"])
        self.assertNotIn("analysis", data)

    def test_missing_control_pgn_resolves_to_status_output_pgn(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            out_dir = Path(temp_dir)
            control_pgn = out_dir / "live" / "run-live.pgn"
            board_pgn = out_dir / "live" / "run-game-2-live.pgn"
            write_finished_game(board_pgn, "Board")
            control_pgn.with_suffix(".status.json").write_text(
                json.dumps({"output_pgn": str(board_pgn)}),
                encoding="utf-8",
            )

            resolved = resolve_live_default_game_path(control_pgn)

        self.assertEqual(resolved, board_pgn)

    def test_zero_research_data_has_network_ladder_and_memorization_status(self):
        data = collect_zero_research_data()

        self.assertIn("current_network", data)
        self.assertIn("benchmark_ladder", data)
        self.assertIn("anti_memorization", data)
        self.assertIn("climb", data)
        self.assertTrue(any(row["name"] == "Codex-chess-zero deliberative" for row in data["benchmark_ladder"]))
        self.assertTrue(data["anti_memorization"]["ok"])

    def test_master_wisdom_data_has_separate_leaderboard_contract(self):
        data = collect_master_wisdom_data()

        self.assertIn("summary", data)
        self.assertIn("source", data)
        self.assertIn("wisdom", data)
        self.assertIn("leaderboard", data)
        self.assertIn("current_attempt", data)
        self.assertIn("current_depth", data["summary"])

    def test_zero_climb_data_reads_persisted_state_without_touching_live_game(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_path = root / "climb-state.json"
            log_path = root / "climb-log.jsonl"
            state_path.write_text(
                json.dumps(
                    {
                        "updated_at": "test",
                        "current_stage_index": 0,
                        "beaten_stages": [],
                        "attempts": [{"stage_index": 0}],
                        "last_result": {
                            "evaluation": {
                                "stage": "random-legal",
                                "score": 0.6875,
                                "pass_score": 0.7,
                                "training_sources": {"opponent_labels_used": False},
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            log_path.write_text(json.dumps({"stage_index": 0}) + "\n", encoding="utf-8")
            original_state = viewer.ZERO_CLIMB_STATE_PATH
            original_log = viewer.ZERO_CLIMB_LOG_PATH
            try:
                viewer.ZERO_CLIMB_STATE_PATH = state_path
                viewer.ZERO_CLIMB_LOG_PATH = log_path
                data = viewer.collect_zero_climb_data()
            finally:
                viewer.ZERO_CLIMB_STATE_PATH = original_state
                viewer.ZERO_CLIMB_LOG_PATH = original_log

        self.assertTrue(data["exists"])
        self.assertEqual(data["current_stage"]["name"], "random-legal")
        self.assertEqual(data["last_result"]["evaluation"]["score"], 0.6875)
        self.assertFalse(data["last_result"]["evaluation"]["training_sources"]["opponent_labels_used"])

    def test_viewer_event_signatures_include_research_channel(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            out_dir = Path(temp_dir)
            pgn_path = out_dir / "live.pgn"
            write_finished_game(pgn_path)

            signatures = viewer_event_signatures(pgn_path, out_dir)

        self.assertIn("research", signatures)
        self.assertIn("learner", signatures)
        self.assertIn("master-wisdom", signatures)

    def test_isolated_zero_stockfish_depth_match_writes_outside_live_matches(self):
        class FakeNetwork:
            network_id = "fake-zero"

        class FakeZero:
            class PolicyValueNetwork:
                @staticmethod
                def load():
                    return FakeNetwork()

            @staticmethod
            def run_mcts(board, network, visits=1):
                class Result:
                    pass

                result = Result()
                result.move = next(iter(board.legal_moves))
                result.comment = "zero local move"
                return result

        class FakeStockfish:
            def __init__(self, config_path, depth):
                self.depth = depth

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def choose(self, board):
                return next(iter(board.legal_moves))

        with tempfile.TemporaryDirectory() as temp_dir:
            match_dir = Path(temp_dir) / "zero-depth-matches"
            live_pgn_path = Path(temp_dir) / "live" / "zero-vs-stockfish-depth-1-20260519-101010-live.pgn"
            original_loader = viewer.load_zero_research_module
            original_player = viewer.StockfishDepthMatchPlayer
            try:
                viewer.load_zero_research_module = lambda: FakeZero
                viewer.StockfishDepthMatchPlayer = FakeStockfish
                result = viewer.write_isolated_zero_stockfish_depth_match(
                    Path("unused.json"),
                    depth=1,
                    zero_visits=1,
                    max_plies=4,
                    match_dir=match_dir,
                    stamp="20260519-101010",
                    live_pgn_path=live_pgn_path,
                )
            finally:
                viewer.load_zero_research_module = original_loader
                viewer.StockfishDepthMatchPlayer = original_player

            pgn_path = Path(result["pgn_path"])
            metadata = viewer.collect_zero_depth_match_data(match_dir)
            live_exists = live_pgn_path.exists()
            live_status = json.loads(live_pgn_path.with_suffix(".status.json").read_text(encoding="utf-8"))

        self.assertTrue(result["ok"])
        self.assertIn("zero-depth-matches", str(pgn_path))
        self.assertNotIn("\\live\\", str(pgn_path).lower())
        self.assertTrue(live_exists)
        self.assertEqual(result["live_pgn_path"], str(live_pgn_path))
        self.assertEqual(result["live_tournament_slug"], "zero-vs-stockfish-depth-1-20260519-101010")
        self.assertEqual(live_status["locked_game"], 1)
        self.assertTrue(live_status["games"][0]["finished"])
        self.assertEqual(result["depth"], 1)
        self.assertEqual(result["training_sources"]["stockfish_labels_used"], False)
        self.assertTrue(metadata["exists"])
        self.assertEqual(metadata["result"], "1/2-1/2")


if __name__ == "__main__":
    unittest.main()
