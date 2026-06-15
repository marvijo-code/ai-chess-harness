import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load_climb_module():
    path = ROOT / "tools" / "run_zero_climb.py"
    spec = importlib.util.spec_from_file_location("zero_climb_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ZeroClimbTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.climb = load_climb_module()

    def test_stage_catalog_marks_stockfish_as_evaluation_only(self):
        stages = self.climb.stage_catalog(stockfish_available=True)
        stockfish = [stage for stage in stages if stage.opponent == "stockfish"]

        self.assertTrue(stockfish)
        self.assertTrue(all(stage.evaluation_only for stage in stockfish))
        self.assertTrue(all(not stage.training_allowed for stage in stockfish))

    def test_gm_sprint_profile_scales_bounded_learning_work(self):
        defaults = self.climb.profile_defaults("gm-sprint")

        self.assertGreater(defaults["zero_visits"], self.climb.PROFILE_DEFAULTS["quick"]["zero_visits"])
        self.assertGreater(defaults["self_play_games"], self.climb.PROFILE_DEFAULTS["quick"]["self_play_games"])
        self.assertGreater(defaults["promotion_games"], self.climb.PROFILE_DEFAULTS["quick"]["promotion_games"])
        self.assertEqual(defaults["self_play_max_plies"], 120)

    def test_profile_settings_keep_cli_overrides(self):
        args = type(
            "Args",
            (),
            {
                "profile": "gm-sprint",
                "cycles": 1,
                "zero_visits": None,
                "self_play_games": 2,
                "self_play_visits": None,
                "self_play_max_plies": None,
                "train_epochs": None,
                "promotion_games": None,
                "promotion_visits": None,
            },
        )()

        settings = self.climb.resolve_profile_settings(args)

        self.assertEqual(settings["cycles"], 1)
        self.assertEqual(settings["self_play_games"], 2)
        self.assertEqual(settings["zero_visits"], self.climb.PROFILE_DEFAULTS["gm-sprint"]["zero_visits"])

    def test_climb_advances_when_stage_gate_is_passed(self):
        stages = [
            self.climb.LadderStage(
                name="always-pass-random",
                opponent="random",
                games=1,
                pass_score=0.0,
                max_plies=4,
            )
        ]

        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            log = Path(tmp) / "log.jsonl"
            result = self.climb.run_climb_cycle(
                state_path=state,
                log_path=log,
                stages=stages,
                zero_visits=1,
                self_play_games=0,
            )
            saved = json.loads(state.read_text(encoding="utf-8"))

        self.assertEqual(result["action"], "advanced")
        self.assertEqual(saved["current_stage_index"], 1)
        self.assertEqual(saved["beaten_stages"][0]["stage"], "always-pass-random")

    def test_failed_gate_trains_only_from_zero_self_play(self):
        stages = [
            self.climb.LadderStage(
                name="impossible-random",
                opponent="random",
                games=1,
                pass_score=1.1,
                max_plies=2,
            )
        ]

        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            log = Path(tmp) / "log.jsonl"
            with patch.object(self.climb, "train_after_failed_gate") as train:
                train.return_value = {
                    "self_play_games": 1,
                    "training_sources": {source: False for source in self.climb.zero.FORBIDDEN_TRAINING_SOURCES},
                    "promotion": {"promoted": False},
                }
                result = self.climb.run_climb_cycle(
                    state_path=state,
                    log_path=log,
                    stages=stages,
                    zero_visits=1,
                    self_play_games=1,
                )

        self.assertEqual(result["action"], "trained_self_play_and_retried_later")
        self.assertTrue(train.called)
        self.assertFalse(any(result["training"]["training_sources"].values()))
        self.assertFalse(result["evaluation"]["training_sources"]["opponent_labels_used"])

    def test_external_regression_guard_blocks_bad_internal_promotion(self):
        stage = self.climb.LadderStage(
            name="stockfish-depth-2",
            opponent="stockfish",
            games=1,
            pass_score=0.5,
            max_plies=2,
            stockfish_depth=2,
        )
        incumbent = self.climb.zero.PolicyValueNetwork(network_id="current", generation=10)
        candidate = self.climb.zero.PolicyValueNetwork(network_id="candidate", generation=11)
        promotion = {"promoted": True, "committed": False, "match": {"score": 0.75, "games": 8}}
        baseline = {"available": True, "network_id": "current", "score": 0.25}

        with tempfile.TemporaryDirectory() as tmp:
            current_path = Path(tmp) / "current-network.json"
            original_current = self.climb.zero.CURRENT_NETWORK_PATH
            self.climb.zero.CURRENT_NETWORK_PATH = current_path
            incumbent.save(current_path)
            try:
                with patch.object(self.climb, "evaluate_stage") as evaluate:
                    evaluate.return_value = {
                        "available": True,
                        "network_id": "candidate",
                        "score": 0.0,
                        "training_sources": {"opponent_labels_used": False, "stockfish_labels_used": False},
                    }
                    result = self.climb.finalize_candidate_promotion(
                        candidate,
                        promotion,
                        stage=stage,
                        baseline_evaluation=baseline,
                        zero_visits=1,
                        seed=99,
                    )
                saved = json.loads(current_path.read_text(encoding="utf-8"))
            finally:
                self.climb.zero.CURRENT_NETWORK_PATH = original_current

        self.assertFalse(result["promoted"])
        self.assertFalse(result["committed"])
        self.assertTrue(result["internal_promoted"])
        self.assertFalse(result["external_regression_guard"]["passed"])
        self.assertEqual(saved["network_id"], "current")

    def test_neutral_internal_gate_can_commit_when_external_guard_is_safe(self):
        stage = self.climb.LadderStage(
            name="stockfish-depth-2",
            opponent="stockfish",
            games=6,
            pass_score=0.5,
            max_plies=120,
            stockfish_depth=2,
        )
        incumbent = self.climb.zero.PolicyValueNetwork(network_id="current", generation=12)
        candidate = self.climb.zero.PolicyValueNetwork(network_id="candidate", generation=13)
        promotion = {"promoted": False, "committed": False, "match": {"score": 0.5, "games": 8}}
        baseline = {"available": True, "network_id": "current", "score": 0.416667}

        with tempfile.TemporaryDirectory() as tmp:
            current_path = Path(tmp) / "current-network.json"
            original_current = self.climb.zero.CURRENT_NETWORK_PATH
            self.climb.zero.CURRENT_NETWORK_PATH = current_path
            incumbent.save(current_path)
            try:
                with patch.object(self.climb, "evaluate_stage") as evaluate:
                    evaluate.return_value = {
                        "available": True,
                        "network_id": "candidate",
                        "score": 0.416667,
                        "training_sources": {"opponent_labels_used": False, "stockfish_labels_used": False},
                    }
                    result = self.climb.finalize_candidate_promotion(
                        candidate,
                        promotion,
                        stage=stage,
                        baseline_evaluation=baseline,
                        zero_visits=16,
                        seed=99,
                    )
                saved = json.loads(current_path.read_text(encoding="utf-8"))
            finally:
                self.climb.zero.CURRENT_NETWORK_PATH = original_current

        self.assertTrue(result["promoted"])
        self.assertTrue(result["committed"])
        self.assertFalse(result["internal_promoted"])
        self.assertTrue(result["internal_neutral"])
        self.assertTrue(result["external_regression_guard"]["passed"])
        self.assertEqual(saved["network_id"], "candidate")

    def test_failed_gate_seed_windows_do_not_overlap_between_attempts(self):
        stages = [
            self.climb.LadderStage(
                name="impossible-random",
                opponent="random",
                games=1,
                pass_score=1.1,
                max_plies=2,
            )
        ]

        def training_result(**kwargs):
            first = kwargs["seed"]
            games = kwargs["self_play_games"]
            return {
                "self_play_games": games,
                "self_play_seed_window": {
                    "first": first,
                    "last": first + games - 1,
                    "games_requested": games,
                    "games_written": games,
                },
                "training_sources": {source: False for source in self.climb.zero.FORBIDDEN_TRAINING_SOURCES},
                "promotion": {"promoted": False},
                "diagnosis": {"training_sources": {source: False for source in self.climb.zero.FORBIDDEN_TRAINING_SOURCES}},
            }

        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            log = Path(tmp) / "log.jsonl"
            with patch.object(self.climb, "evaluate_stage") as evaluate, patch.object(self.climb, "train_after_failed_gate") as train:
                evaluate.return_value = {
                    "stage": "impossible-random",
                    "available": True,
                    "passed": False,
                    "score": 0.0,
                    "training_sources": {"opponent_labels_used": False, "stockfish_labels_used": False},
                }
                train.side_effect = training_result
                first = self.climb.run_climb_cycle(
                    state_path=state,
                    log_path=log,
                    stages=stages,
                    seed=10,
                    self_play_games=6,
                )
                second = self.climb.run_climb_cycle(
                    state_path=state,
                    log_path=log,
                    stages=stages,
                    seed=10,
                    self_play_games=6,
                )

        seeds = [call.kwargs["seed"] for call in train.call_args_list]
        self.assertEqual(seeds, [10, 16])
        self.assertLess(first["training"]["self_play_seed_window"]["last"], second["training"]["self_play_seed_window"]["first"])
        self.assertFalse(any(first["training"]["training_sources"].values()))

    def test_training_diagnosis_flags_duplicate_stale_self_play(self):
        game = {
            "result": "1-0",
            "plies": 2,
            "replay_append": {"added": 0, "skipped_duplicates": 2},
            "records": [
                {"position_key": "start", "chosen_move": "e2e4", "selection": "greedy"},
                {"position_key": "after", "chosen_move": "e7e5", "selection": "greedy"},
            ],
        }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first.json"
            second = root / "second.json"
            first.write_text(json.dumps(game), encoding="utf-8")
            second.write_text(json.dumps(game), encoding="utf-8")
            candidate = type("Candidate", (), {"network_id": "candidate"})()

            diagnosis = self.climb.summarize_training_feedback(
                [first, second],
                candidate,
                {"promoted": False},
            )

        self.assertEqual(diagnosis["status"], "stalled_no_new_replay_signal")
        self.assertTrue(diagnosis["duplicate_trajectories"])
        self.assertEqual(diagnosis["replay_added"], 0)
        self.assertIn("candidate failed promotion gate", diagnosis["reasons"])
        self.assertFalse(any(diagnosis["training_sources"].values()))

    def test_round_metrics_report_external_internal_and_replay_fields(self):
        row = {
            "generated_at": "now",
            "action": "trained_self_play_and_retried_later",
            "stage": {"name": "stockfish-depth-2"},
            "evaluation": {
                "score": 0.25,
                "games": 4,
                "rows": [
                    {"zero_score": 1.0},
                    {"zero_score": 0.5},
                    {"zero_score": 0.0},
                    {"zero_score": 0.0},
                ],
            },
            "training": {
                "promotion": {"promoted": False, "match": {"score": 0.5, "games": 2}},
                "diagnosis": {
                    "true_draw_positions": 3,
                    "capped_draw_positions": 2,
                    "repetition_draw_positions": 1,
                    "unique_opening_signatures": 2,
                    "replay_added": 5,
                    "replay_updated_duplicates": 1,
                    "replay_skipped_duplicates": 7,
                    "training_metrics": {"policy_loss": 0.12, "value_loss": 0.34},
                },
            },
        }

        metrics = self.climb.build_round_metrics(row)

        self.assertEqual(metrics["external_wins"], 1)
        self.assertEqual(metrics["external_draws"], 1)
        self.assertEqual(metrics["external_losses"], 2)
        self.assertEqual(metrics["internal_gate_score"], 0.5)
        self.assertFalse(metrics["internal_gate_neutral"])
        self.assertEqual(metrics["policy_loss"], 0.12)


if __name__ == "__main__":
    unittest.main()
