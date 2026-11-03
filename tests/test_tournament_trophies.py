"""Tournament titles, rather than individual game wins, earn trophies."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import llm_tournament_viewer as viewer


class TrophyCounts(unittest.TestCase):
    def test_completed_titles_only_once_and_real_zero_counts(self):
        rows = [
            {"id": "one", "finished": True, "champion": "Alpha", "runner_up": "Beta", "players": ["Alpha", "Beta"]},
            {"id": "one", "finished": True, "champion": "Alpha", "runner_up": "Beta"},
            {"id": "two", "finished": False, "champion": "Beta", "runner_up": "Alpha"},
            {"id": "three", "finished": True, "champion": ""},
            {"id": "four", "finished": True, "champion": "Past model"},
        ]
        result = viewer.tournament_wins(rows, [{"name": "Alpha"}, {"name": "Beta"}, {"name": "New model"}])
        self.assertEqual(result["completed"], 2)
        self.assertEqual({p["name"]: p["wins"] for p in result["players"]},
                         {"Alpha": 1, "Beta": 0, "New model": 0, "Past model": 1})
        self.assertEqual({p["name"]: p["runners_up"] for p in result["players"]},
                         {"Alpha": 0, "Beta": 1, "New model": 0, "Past model": 0})

    def test_empty_history_still_shows_participating_models(self):
        self.assertEqual(viewer.tournament_wins([], [{"name": "Alpha"}]),
                         {"completed": 0, "players": [{"name": "Alpha", "wins": 0, "runners_up": 0}]})

    def test_summary_preserves_recorded_runner_up(self):
        row = viewer.tournament_summary({"id": "cup", "finished": True,
                                        "knockout": {"champion": "Alpha", "runner_up": "Beta"}})
        self.assertEqual(row["runner_up"], "Beta")

    def test_missing_or_invalid_runner_up_is_not_invented(self):
        rows = [{"id": "one", "finished": True, "champion": "Alpha", "runner_up": "Alpha"},
                {"id": "two", "finished": True, "champion": "Alpha"}]
        result = viewer.tournament_wins(rows, [{"name": "Alpha"}, {"name": "Beta"}])
        self.assertTrue(all(p["runners_up"] == 0 for p in result["players"]))
