import tempfile
import unittest
import json
from pathlib import Path

import chess.pgn

import tools.live_pgn_viewer as viewer
from tools.live_pgn_viewer import collect_stats, collect_zero_research_data, resolve_live_default_game_path, viewer_event_signatures


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


if __name__ == "__main__":
    unittest.main()
