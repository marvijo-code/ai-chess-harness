import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_master_wisdom():
    path = ROOT / "tools" / "master_wisdom.py"
    spec = importlib.util.spec_from_file_location("master_wisdom_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


SAMPLE_PGN = """
[Event "Fixture 1"]
[Site "Lichess"]
[Date "2025.01.01"]
[White "MasterA"]
[Black "MasterB"]
[Result "1-0"]
[ECO "C20"]
[Opening "King's Pawn Game"]

1. e4 e5 2. Nf3 Nc6 3. Bc4 Bc5 4. O-O Nf6 5. d3 O-O 6. Nc3 d6 7. Bg5 h6 8. Bh4 g5 9. Nxg5 hxg5 10. Bxg5 1-0

[Event "Fixture 2"]
[Site "Lichess"]
[Date "2025.01.02"]
[White "MasterC"]
[Black "MasterD"]
[Result "0-1"]
[ECO "D00"]
[Opening "Queen's Pawn Game"]

1. d4 d5 2. Nc3 Nf6 3. Bf4 e6 4. e3 Be7 5. Bd3 O-O 6. Nf3 c5 7. O-O Nc6 8. Ne5 cxd4 9. exd4 Nxd4 10. Bxh7+ Kxh7 0-1
""".strip()


class MasterWisdomTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mw = load_master_wisdom()

    def make_paths(self, root: Path):
        paths = self.mw.default_paths(root)
        paths.downloads_dir.mkdir(parents=True, exist_ok=True)
        paths.learner_knowledgebase_dir.mkdir(parents=True, exist_ok=True)
        paths.learner_skills_dir.mkdir(parents=True, exist_ok=True)
        (root / "chess-harness.config.json").write_text(
            json.dumps(
                {
                    "masterWisdom": {
                        "batchSize": 2,
                        "maxBatchSize": 8,
                        "gamesPerAttempt": 10,
                        "passScore": 0.8,
                        "targetDepth": 8,
                        "modelSynthesis": False,
                    }
                }
            ),
            encoding="utf-8",
        )
        pgn_path = paths.downloads_dir / "fixture.pgn"
        pgn_path.write_text(SAMPLE_PGN, encoding="utf-8")
        paths.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        paths.manifest_path.write_text(
            json.dumps(
                {
                    "schema": "lichess-elite-manifest-v1",
                    "files": [
                        {
                            "month": "fixture",
                            "filename": "fixture.pgn",
                            "url": "file://fixture.pgn",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return paths

    def test_learn_batch_writes_human_wisdom_and_learner_skill(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = self.make_paths(Path(temp_dir))

            result = self.mw.learn_batch(paths, batch_size=2)
            state = json.loads(paths.state_path.read_text(encoding="utf-8"))
            wisdom = paths.wisdom_md_path.read_text(encoding="utf-8")
            skill = paths.skill_path.read_text(encoding="utf-8")

        self.assertTrue(result["ok"])
        self.assertEqual(result["processed"], 2)
        self.assertEqual(state["processed_games"], 2)
        self.assertIn("Master Game Wisdom", wisdom)
        self.assertIn("Processed games: 2", wisdom)
        self.assertIn("exact FEN-to-move", skill)
        self.assertNotIn("Common Opening Families", wisdom)
        self.assertNotIn("White score", wisdom)
        self.assertNotIn("evidence", skill.lower())
        self.assertNotIn("score", skill.lower())
        self.assertTrue(state["patterns"])
        self.assertNotIn("openings", state)

    def test_authored_wisdom_is_preserved_across_batch_learning(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = self.make_paths(Path(temp_dir))
            authored_md = "# Master Game Wisdom\n\nAuthored-by: Codex batch synthesis\n\n- Think before moving.\n"
            authored_json = {"schema": "master-wisdom-authored-v1", "authored_by": "Codex batch synthesis", "principles": ["Think before moving."]}
            authored_skill = "---\nname: master-game-wisdom\n---\n\n# Master Game Wisdom\n\nLearner-local Codex-authored principles.\n"
            paths.wisdom_md_path.write_text(authored_md, encoding="utf-8")
            paths.wisdom_json_path.write_text(json.dumps(authored_json), encoding="utf-8")
            paths.skill_path.parent.mkdir(parents=True, exist_ok=True)
            paths.skill_path.write_text(authored_skill, encoding="utf-8")

            result = self.mw.learn_batch(paths, batch_size=2)

            self.assertTrue(result["ok"])
            self.assertEqual(paths.wisdom_md_path.read_text(encoding="utf-8"), authored_md)
            self.assertEqual(json.loads(paths.wisdom_json_path.read_text(encoding="utf-8")), authored_json)
            self.assertEqual(paths.skill_path.read_text(encoding="utf-8"), authored_skill)

    def test_model_synthesis_defaults_to_gpt55_extra_high_when_enabled(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = self.make_paths(root)
            config_path = root / "chess-harness.config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["masterWisdom"]["modelSynthesis"] = True
            config["masterWisdom"]["synthesisModel"] = "gpt-5.5"
            config["masterWisdom"]["synthesisEffort"] = "xhigh"
            config_path.write_text(json.dumps(config), encoding="utf-8")

            settings = self.mw.configured_model_synthesis(paths)

        self.assertTrue(settings["enabled"])
        self.assertEqual(settings["model"], "gpt-5.5")
        self.assertEqual(settings["effort"], "xhigh")

    def test_model_synthesis_writes_authored_wisdom_with_configured_model(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = self.make_paths(Path(temp_dir))
            captured = {}

            class Completed:
                returncode = 0
                stderr = ""
                stdout = json.dumps(
                    {
                        "thread_id": "thread-test",
                        "turn_id": "turn-test",
                        "text": json.dumps(
                            {
                                "priority_principles": ["Check forcing replies before starting your plan"],
                                "principles": ["Improve king safety and loose-piece coordination before pawn hunting"],
                                "skill_principles": ["Check forcing replies before starting your plan"],
                            }
                        ),
                    }
                )

            original_run = self.mw.subprocess.run

            def fake_run(command, **kwargs):
                captured["command"] = command
                prompt_file = Path(command[command.index("--prompt-file") + 1])
                captured["prompt"] = json.loads(prompt_file.read_text(encoding="utf-8"))
                return Completed()

            try:
                self.mw.subprocess.run = fake_run
                result = self.mw.learn_batch(
                    paths,
                    batch_size=1,
                    model_synthesis=True,
                    synthesis_model="gpt-5.5",
                    synthesis_effort="xhigh",
                    synthesis_timeout=1,
                )
            finally:
                self.mw.subprocess.run = original_run

            wisdom = paths.wisdom_md_path.read_text(encoding="utf-8")
            payload = json.loads(paths.wisdom_json_path.read_text(encoding="utf-8"))

        self.assertEqual(result["synthesis"]["status"], "ok")
        self.assertIn("gpt-5.5", captured["command"])
        self.assertIn("xhigh", captured["command"])
        self.assertIn("latest_batch_move_by_move_evidence", captured["prompt"])
        self.assertEqual(captured["prompt"]["latest_batch_move_by_move_evidence"]["games_scanned"], 1)
        self.assertIn("Synthesis model: gpt-5.5", wisdom)
        self.assertIn("Check forcing replies", wisdom)
        self.assertEqual(payload["synthesis"]["effort"], "xhigh")

    def test_learn_batch_records_abstract_move_by_move_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = self.make_paths(Path(temp_dir))

            result = self.mw.learn_batch(paths, batch_size=2)
            state = json.loads(paths.state_path.read_text(encoding="utf-8"))
            evidence = state["last_move_evidence"]
            artifact = Path(evidence["artifact"])
            first_line = json.loads(artifact.read_text(encoding="utf-8").splitlines()[0])
            first_move = first_line["move_trace"][0]

        self.assertTrue(result["ok"])
        self.assertEqual(evidence["schema"], "master-wisdom-move-evidence-v1")
        self.assertEqual(evidence["games_scanned"], 2)
        self.assertGreater(evidence["plies_scanned"], 0)
        self.assertGreater(len(evidence["top_move_patterns"]), 0)
        self.assertIn("phase", first_move)
        self.assertIn("piece", first_move)
        self.assertIn("events", first_move)
        self.assertNotIn("uci", first_move)
        self.assertNotIn("san", first_move)
        self.assertNotIn("fen", first_move)

    def test_learn_batch_resumes_partial_archive_from_saved_offset(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = self.make_paths(Path(temp_dir))
            first = self.mw.learn_batch(paths, batch_size=1)
            state = json.loads(paths.state_path.read_text(encoding="utf-8"))
            saved_offset = int(state["processed_file_offsets"]["fixture.pgn"])
            observed_offsets = []
            original_iter = self.mw.iter_games_with_offsets

            def tracking_iter(path, start_offset=0):
                observed_offsets.append(start_offset)
                yield from original_iter(path, start_offset=start_offset)

            try:
                self.mw.iter_games_with_offsets = tracking_iter
                second = self.mw.learn_batch(paths, batch_size=1)
            finally:
                self.mw.iter_games_with_offsets = original_iter
            state = json.loads(paths.state_path.read_text(encoding="utf-8"))

        self.assertEqual(first["processed"], 1)
        self.assertGreater(saved_offset, 0)
        self.assertEqual(second["processed"], 1)
        self.assertIn(saved_offset, observed_offsets)
        self.assertEqual(state["processed_files"]["fixture.pgn"], 2)

    def test_learn_batch_skips_known_exhausted_archives_without_reopening(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = self.make_paths(Path(temp_dir))
            first = self.mw.learn_batch(paths, batch_size=3)
            state = json.loads(paths.state_path.read_text(encoding="utf-8"))
            original_iter = self.mw.iter_games_with_offsets

            def fail_if_reopened(path, start_offset=0):
                raise AssertionError("exhausted archive should have been skipped")
                yield

            try:
                self.mw.iter_games_with_offsets = fail_if_reopened
                second = self.mw.learn_batch(paths, batch_size=1)
            finally:
                self.mw.iter_games_with_offsets = original_iter

        self.assertEqual(first["processed"], 2)
        self.assertEqual(state["processed_file_totals"]["fixture.pgn"], 2)
        self.assertEqual(second["processed"], 0)
        self.assertIn("fixture.pgn", second["skipped_files"])

    def test_infer_exhausted_prefix_totals_leaves_frontier_and_out_of_order_seed(self):
        state = {
            "processed_files": {
                "2020-06.pgn": 10,
                "2020-07.pgn": 20,
                "2025-11.pgn": 5,
            },
            "processed_file_totals": {},
        }
        manifest = {
            "files": [
                {"month": "2020-06", "filename": "2020-06.pgn"},
                {"month": "2020-07", "filename": "2020-07.pgn"},
                {"month": "2020-08", "filename": "2020-08.pgn"},
                {"month": "2025-11", "filename": "2025-11.pgn"},
            ]
        }

        self.mw.infer_exhausted_prefix_totals(state, manifest)

        self.assertEqual(state["processed_file_totals"], {"2020-06.pgn": 10})

    def test_failed_gate_next_cycle_marks_batch_for_move_learning(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = self.make_paths(root)
            config_path = root / "chess-harness.config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["masterWisdom"]["batchSize"] = 1
            config_path.write_text(json.dumps(config), encoding="utf-8")

            result = self.mw.run_cycles(paths, cycles=2, dry_run_evaluation=True, simulated_score=0.0, model_synthesis=False)
            state = json.loads(paths.state_path.read_text(encoding="utf-8"))
            batches = state["recent_batches"]

        self.assertTrue(result["ok"])
        self.assertGreaterEqual(len(batches), 2)
        self.assertEqual(batches[1]["trigger"], "failed_gate")
        self.assertFalse(batches[1]["after_failed_attempt"]["score"])
        self.assertEqual(batches[1]["move_evidence"]["trigger"], "failed_gate")
        self.assertGreaterEqual(batches[1]["move_evidence"]["games_scanned"], 1)

    def test_learn_batch_downloads_next_missing_archive_when_batch_short(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = self.make_paths(Path(temp_dir))
            manifest = json.loads(paths.manifest_path.read_text(encoding="utf-8"))
            manifest["files"].append(
                {
                    "month": "fixture2",
                    "filename": "fixture2.pgn",
                    "url": "file://fixture2.pgn",
                }
            )
            paths.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            original_download_file = self.mw.download_file

            def fake_download_file(entry, received_paths):
                target = received_paths.downloads_dir / str(entry["filename"])
                target.write_text(SAMPLE_PGN, encoding="utf-8")
                return {"filename": target.name, "path": str(target), "downloaded": True, "size": target.stat().st_size}

            try:
                self.mw.download_file = fake_download_file
                result = self.mw.learn_batch(paths, batch_size=3)
                state = json.loads(paths.state_path.read_text(encoding="utf-8"))
            finally:
                self.mw.download_file = original_download_file

        self.assertTrue(result["ok"])
        self.assertEqual(result["processed"], 3)
        self.assertEqual(len(result["downloaded"]), 1)
        self.assertEqual(result["downloaded"][0]["filename"], "fixture2.pgn")
        self.assertEqual(state["processed_files"]["fixture.pgn"], 2)
        self.assertEqual(state["processed_files"]["fixture2.pgn"], 1)

    def test_clock_helpers_write_viewer_metadata_and_clk_comment(self):
        game = self.mw.chess.pgn.Game()
        self.mw.set_game_clock_headers(game, white_ms=600000, black_ms=590000, running_side="Black", updated_at_ms=123456)

        move = self.mw.chess.Move.from_uci("e2e4")
        node = game.add_variation(move)
        node.comment = f"e4 [%clk {self.mw.format_clock_comment(598000)}]"

        self.assertEqual(game.headers["WhiteClockMs"], "600000")
        self.assertEqual(game.headers["BlackClockMs"], "590000")
        self.assertEqual(game.headers["ClockUpdatedAtEpochMs"], "123456")
        self.assertEqual(game.headers["ClockRunningSide"], "Black")
        self.assertIn("[%clk 9:58]", str(game))

    def test_depth_attempt_escalates_batch_on_failure_and_advances_on_pass(self):
        state = self.mw.initial_state()
        state["batch_size"] = 500

        failed = self.mw.record_depth_attempt(state, depth=1, games=10, learner_points=5.0, wins=5, draws=0, losses=5)
        passed = self.mw.record_depth_attempt(state, depth=1, games=10, learner_points=8.0, wins=8, draws=0, losses=2)

        self.assertFalse(failed["passed"])
        self.assertEqual(state["batch_size"], 1000)
        self.assertTrue(passed["passed"])
        self.assertEqual(state["current_depth"], 2)

    def test_depth_attempt_can_stop_early_when_target_is_unreachable(self):
        state = self.mw.initial_state()
        state["batch_size"] = 1000

        row = self.mw.record_depth_attempt(
            state,
            depth=1,
            games=7,
            learner_points=1.0,
            wins=0,
            draws=2,
            losses=5,
            total_games=10,
            early_stopped=True,
            stop_reason="target unreachable after 7/10 games",
        )

        self.assertFalse(row["passed"])
        self.assertTrue(row["early_stopped"])
        self.assertEqual(row["games"], 7)
        self.assertEqual(row["total_games"], 10)
        self.assertEqual(row["score"], 0.1)
        self.assertEqual(state["batch_size"], 2000)

    def test_dry_run_evaluation_writes_separate_leaderboard(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = self.make_paths(Path(temp_dir))

            result = self.mw.evaluate_depth(paths, dry_run=True, simulated_score=0.8)
            leaderboard = json.loads(paths.leaderboard_path.read_text(encoding="utf-8"))

        self.assertTrue(result["ok"])
        self.assertEqual(leaderboard["schema"], "master-wisdom-leaderboard-v1")
        self.assertTrue(leaderboard["rows"][0]["passed"])
        self.assertEqual(leaderboard["rows"][0]["depth"], 1)

    def test_evaluate_depth_blocks_unavailable_model_without_attempt_loss(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = self.make_paths(Path(temp_dir))
            original_preflight = self.mw.preflight_learner_play_model
            original_stockfish = self.mw.load_stockfish_path

            def failing_preflight(paths_arg, model, effort, timeout_seconds):
                return {"ok": False, "message": "unsupported model", "stderr": "unsupported model"}

            try:
                self.mw.preflight_learner_play_model = failing_preflight
                self.mw.load_stockfish_path = lambda: (_ for _ in ()).throw(AssertionError("stockfish should not start"))
                result = self.mw.evaluate_depth(paths, games=1, max_plies=1)
            finally:
                self.mw.preflight_learner_play_model = original_preflight
                self.mw.load_stockfish_path = original_stockfish
            state = json.loads(paths.state_path.read_text(encoding="utf-8"))

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "learner_model_preflight_failed")
        self.assertEqual(state["attempts"], [])
        self.assertEqual(state["last_training_blocker"]["message"], "unsupported model")

    def test_evaluate_depth_passes_play_model_override_to_learner_process(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = self.make_paths(root)
            config = json.loads((root / "chess-harness.config.json").read_text(encoding="utf-8"))
            config["masterWisdom"]["playModel"] = "gpt-5.5"
            config["masterWisdom"]["playEffort"] = "high"
            (root / "chess-harness.config.json").write_text(json.dumps(config), encoding="utf-8")
            instances = []

            class FakeUciPlayer:
                def __init__(self, name, command, options=None, env=None):
                    self.name = name
                    self.env = env or {}
                    instances.append(self)

                def new_game(self):
                    return None

                def bestmove(self, board, go_command, timeout_seconds=120):
                    return next(iter(board.legal_moves), None)

                def close(self):
                    return None

            original_preflight = self.mw.preflight_learner_play_model
            original_stockfish = self.mw.load_stockfish_path
            original_player = self.mw.UciPlayer
            try:
                self.mw.preflight_learner_play_model = lambda paths_arg, model, effort, timeout_seconds: {"ok": True, "message": "ok"}
                self.mw.load_stockfish_path = lambda: Path("stockfish")
                self.mw.UciPlayer = FakeUciPlayer
                result = self.mw.evaluate_depth(paths, games=1, max_plies=1)
            finally:
                self.mw.preflight_learner_play_model = original_preflight
                self.mw.load_stockfish_path = original_stockfish
                self.mw.UciPlayer = original_player

        learner = next(item for item in instances if item.name == "Codex-chess-learner")
        self.assertTrue(result["ok"])
        self.assertEqual(learner.env["CODEX_CHESS_MODEL"], "gpt-5.5")
        self.assertEqual(learner.env["CODEX_CHESS_EFFORT"], "high")

    def test_evaluate_depth_records_learner_bestmove_timeout_as_forfeit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = self.make_paths(Path(temp_dir))

            class FakeUciPlayer:
                def __init__(self, name, command, options=None, env=None):
                    self.name = name

                def new_game(self):
                    return None

                def bestmove(self, board, go_command, timeout_seconds=120):
                    if self.name == "Codex-chess-learner":
                        raise TimeoutError("hung waiting for bestmove")
                    return next(iter(board.legal_moves), None)

                def close(self):
                    return None

            original_preflight = self.mw.preflight_learner_play_model
            original_stockfish = self.mw.load_stockfish_path
            original_player = self.mw.UciPlayer
            try:
                self.mw.preflight_learner_play_model = lambda paths_arg, model, effort, timeout_seconds: {"ok": True, "message": "ok"}
                self.mw.load_stockfish_path = lambda: Path("stockfish")
                self.mw.UciPlayer = FakeUciPlayer
                result = self.mw.evaluate_depth(paths, games=1, max_plies=2)
            finally:
                self.mw.preflight_learner_play_model = original_preflight
                self.mw.load_stockfish_path = original_stockfish
                self.mw.UciPlayer = original_player

            state = json.loads(paths.state_path.read_text(encoding="utf-8"))
            pgn_text = Path(result["pgn_path"]).read_text(encoding="utf-8")
            status = json.loads(paths.live_pgn_path.with_suffix(".status.json").read_text(encoding="utf-8"))

        self.assertTrue(result["ok"])
        self.assertEqual(result["attempt"]["losses"], 1)
        self.assertEqual(result["attempt"]["score"], 0.0)
        self.assertEqual(result["attempt"]["games"], 1)
        self.assertEqual(state["attempts"][-1]["stop_reason"], "target unreachable after 1/1 games")
        self.assertIn('[Termination "learner timeout waiting for bestmove"]', pgn_text)
        self.assertIn('[Result "0-1"]', pgn_text)
        self.assertTrue(status["games"][0]["finished"])
        self.assertEqual(status["games"][0]["reason"], "learner timeout waiting for bestmove")

    def test_live_game_state_writes_viewer_sidecar(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = self.make_paths(Path(temp_dir))
            game = self.mw.chess.pgn.Game()
            game.headers["Event"] = "Master Wisdom depth 1"
            game.headers["White"] = "Codex-chess-learner"
            game.headers["Black"] = "Stockfish depth 1"
            game.headers["Result"] = "*"
            game.headers["TotalGames"] = "10"

            self.mw.write_live_game_state(paths, game, [], game_number=3, total_games=10, completed=False)
            status = json.loads(paths.live_pgn_path.with_suffix(".status.json").read_text(encoding="utf-8"))
            live_exists = paths.live_pgn_path.exists()

        self.assertTrue(live_exists)
        self.assertEqual(status["output_pgn"], str(paths.live_pgn_path))
        self.assertEqual(status["locked_game"], 3)
        self.assertEqual(status["games"][0]["total"], 10)
        self.assertFalse(status["games"][0]["finished"])

    def test_collect_view_data_exposes_wisdom_and_leaderboard(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = self.make_paths(Path(temp_dir))
            self.mw.learn_batch(paths, batch_size=1)
            self.mw.evaluate_depth(paths, dry_run=True, simulated_score=0.2)

            data = self.mw.collect_view_data(paths)

        self.assertEqual(data["summary"]["processed_games"], 1)
        self.assertTrue(data["wisdom"]["exists"])
        self.assertTrue(data["skill"]["exists"])
        self.assertIn("principles", data)
        self.assertNotIn("openings", data)
        self.assertEqual(len(data["leaderboard"]), 1)
        self.assertIn("current_attempt", data)

    def test_collect_view_data_exposes_current_match_leaderboard(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = self.make_paths(Path(temp_dir))
            state = self.mw.initial_state(Path(temp_dir))
            self.mw.save_state(state, paths)
            game_one = self.mw.chess.pgn.Game()
            game_one.headers["Event"] = "Master Wisdom depth 3"
            game_one.headers["Round"] = "1"
            game_one.headers["White"] = "Codex-chess-learner"
            game_one.headers["Black"] = "Stockfish depth 3"
            game_one.headers["Result"] = "1-0"
            game_one.headers["BatchSize"] = "1000"
            game_one.headers["TotalGames"] = "10"
            game_two = self.mw.chess.pgn.Game()
            game_two.headers["Event"] = "Master Wisdom depth 3"
            game_two.headers["Round"] = "2"
            game_two.headers["White"] = "Stockfish depth 3"
            game_two.headers["Black"] = "Codex-chess-learner"
            game_two.headers["Result"] = "*"
            game_two.headers["BatchSize"] = "1000"
            game_two.headers["TotalGames"] = "10"
            paths.live_pgn_path.parent.mkdir(parents=True, exist_ok=True)
            paths.live_pgn_path.write_text(f"{game_one}\n\n{game_two}\n\n", encoding="utf-8")
            paths.live_pgn_path.with_suffix(".status.json").write_text(
                json.dumps({"locked_game": 2, "games": [{"game": 2, "total": 10, "white": "Stockfish depth 3", "black": "Codex-chess-learner", "result": "*", "finished": False}]}),
                encoding="utf-8",
            )

            data = self.mw.collect_view_data(paths)
            attempt = data["current_attempt"]

        self.assertTrue(attempt["exists"])
        self.assertEqual(attempt["depth"], 3)
        self.assertEqual(attempt["current_game"], 2)
        self.assertEqual(attempt["completed_games"], 1)
        self.assertEqual(attempt["wins"], 1)
        self.assertEqual(attempt["learner_points"], 1.0)
        self.assertEqual(len(attempt["games"]), 2)

    def test_finalize_unreachable_live_attempt_records_partial_and_escalates_batch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = self.make_paths(Path(temp_dir))
            state = self.mw.initial_state(Path(temp_dir))
            state["batch_size"] = 1000
            state["max_batch_size"] = 4000
            self.mw.save_state(state, paths)
            games = []
            for index, result in enumerate(["0-1", "1-0", "1/2-1/2", "1-0", "0-1", "1/2-1/2", "0-1"], start=1):
                game = self.mw.chess.pgn.Game()
                game.headers["Event"] = "Master Wisdom depth 1"
                game.headers["Round"] = str(index)
                learner_white = index % 2 == 1
                game.headers["White"] = "Codex-chess-learner" if learner_white else "Stockfish depth 1"
                game.headers["Black"] = "Stockfish depth 1" if learner_white else "Codex-chess-learner"
                game.headers["Result"] = result
                game.headers["BatchSize"] = "1000"
                game.headers["TotalGames"] = "10"
                games.append(str(game))
            paths.live_pgn_path.parent.mkdir(parents=True, exist_ok=True)
            paths.live_pgn_path.write_text("\n\n".join(games) + "\n\n", encoding="utf-8")
            paths.live_pgn_path.with_suffix(".status.json").write_text(
                json.dumps({"locked_game": 7, "games": [{"game": 7, "total": 10, "white": "Codex-chess-learner", "black": "Stockfish depth 1", "result": "0-1", "finished": True}]}),
                encoding="utf-8",
            )

            result = self.mw.finalize_unreachable_live_attempt(paths)
            updated_state = json.loads(paths.state_path.read_text(encoding="utf-8"))

        self.assertTrue(result["recorded"])
        self.assertEqual(result["attempt"]["games"], 7)
        self.assertEqual(result["attempt"]["total_games"], 10)
        self.assertTrue(result["attempt"]["early_stopped"])
        self.assertEqual(updated_state["batch_size"], 2000)


if __name__ == "__main__":
    unittest.main()
