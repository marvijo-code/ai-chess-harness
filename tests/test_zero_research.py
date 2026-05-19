import importlib.util
import json
import random
import sys
import tempfile
import unittest
from pathlib import Path

import chess


ROOT = Path(__file__).resolve().parents[1]


def load_zero_module():
    path = ROOT / "engines" / "codex-chess-zero" / "zero_research.py"
    spec = importlib.util.spec_from_file_location("codex_chess_zero_research_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ZeroResearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.zero = load_zero_module()

    def test_board_planes_and_legal_mask_are_stable(self):
        board = chess.Board()

        planes = self.zero.board_planes(board)
        mask = self.zero.legal_move_mask(board)
        e2e4 = chess.Move.from_uci("e2e4")

        self.assertEqual(planes["shape"], [18, 8, 8])
        self.assertEqual(planes["plane_names"][0], "white_pawn")
        self.assertEqual(planes["planes"][0][1][4], 1)
        self.assertEqual(planes["planes"][6][6][4], 1)
        self.assertEqual(planes["planes"][12][0][0], 1)
        self.assertEqual(mask["size"], 64 * 64 * 5)
        self.assertIn(self.zero.move_to_index(e2e4), mask["indices"])
        self.assertEqual(self.zero.index_to_move(self.zero.move_to_index(e2e4)), e2e4)

    def test_puct_search_returns_legal_human_readable_choice(self):
        board = chess.Board()
        network = self.zero.PolicyValueNetwork(network_id="test-net")

        result = self.zero.run_mcts(board, network, visits=6)

        self.assertIn(result.move, board.legal_moves)
        self.assertEqual(result.network_id, "test-net")
        self.assertGreaterEqual(result.visits, 1)
        self.assertGreater(result.nodes, 1)
        self.assertTrue(result.candidates)
        self.assertEqual(result.explanation["selected_move"]["uci"], result.move.uci())
        self.assertTrue(result.comment.startswith(f"{board.san(result.move)}:"))
        self.assertEqual(result.explanation["reasoning_controller"], "deliberative-human-v1")
        self.assertIn("candidate_generation", result.explanation)
        self.assertIn("calculation_verifier", result.explanation)
        self.assertIn("calculation support", result.explanation["puct_role"])
        self.assertIn("candidate_moves", result.explanation)
        self.assertIn("tactical_blunder_check", result.explanation)
        self.assertTrue(all("role_tags" in candidate for candidate in result.candidates))
        self.assertTrue(all("plan_intent" in candidate for candidate in result.candidates))
        self.assertTrue(all("refutation" in candidate for candidate in result.candidates))
        self.assertNotIn("chain", result.comment.lower())

    def test_deliberative_selection_bounds_expensive_refutation_scan(self):
        board = chess.Board()
        network = self.zero.PolicyValueNetwork(network_id="test-net")
        calls = []
        original = self.zero.refutation_check

        def counted_refutation(board_arg, move_arg):
            calls.append(move_arg.uci())
            return original(board_arg, move_arg)

        self.zero.refutation_check = counted_refutation
        try:
            self.zero.run_mcts(board, network, visits=1)
        finally:
            self.zero.refutation_check = original

        self.assertLessEqual(len(calls), self.zero.DELIBERATIVE_REFUTATION_CANDIDATE_LIMIT)
        self.assertGreater(len(calls), 0)

    def test_deliberative_shortlist_reserves_safe_quiet_candidates(self):
        board = chess.Board()
        root = self.zero.PuctNode(board.copy(stack=False), visit_count=20, expanded=True)
        for move in board.legal_moves:
            child_board = board.copy(stack=False)
            child_board.push(move)
            root.children[move.uci()] = self.zero.PuctNode(
                child_board,
                prior=0.9,
                move=move,
                visit_count=4,
                value_sum=0.0,
            )
        quiet = chess.Move.from_uci("a2a3")
        original_cheap = self.zero.cheap_deliberative_child_score
        original_safe = self.zero.safe_deliberative_child_score

        def cheap_score(board_arg, child, root_visits):
            assert child.move is not None
            if child.move == quiet:
                return (-100.0, 0, child.move.uci())
            return original_cheap(board_arg, child, root_visits)

        def safe_score(board_arg, child):
            assert child.move is not None
            if child.move == quiet:
                return (10.0, 10.0, 10.0, 10.0, child.visit_count, child.move.uci())
            return original_safe(board_arg, child)

        self.zero.cheap_deliberative_child_score = cheap_score
        self.zero.safe_deliberative_child_score = safe_score
        try:
            children = self.zero.select_deliberative_candidate_children(
                board,
                root,
                list(root.children.values()),
                self.zero.DELIBERATIVE_REFUTATION_CANDIDATE_LIMIT,
            )
        finally:
            self.zero.cheap_deliberative_child_score = original_cheap
            self.zero.safe_deliberative_child_score = original_safe

        self.assertLessEqual(len(children), self.zero.DELIBERATIVE_REFUTATION_CANDIDATE_LIMIT)
        self.assertIn(quiet.uci(), {child.move.uci() for child in children if child.move})

    def test_self_play_exploration_can_choose_from_own_candidate_list(self):
        board = chess.Board()
        greedy = chess.Move.from_uci("e2e4")
        exploratory = chess.Move.from_uci("d2d4")
        result = self.zero.ZeroSearchResult(
            move=greedy,
            network_id="test-net",
            root_value=0.0,
            visits=2,
            nodes=3,
            candidates=[
                {"uci": greedy.uci(), "human_score": 0.0},
                {"uci": exploratory.uci(), "human_score": 10.0},
            ],
            explanation={},
            comment="",
        )

        move, metadata = self.zero.select_self_play_move(
            board,
            result,
            random.Random(1),
            ply=1,
            exploration_plies=8,
            temperature=0.1,
        )

        self.assertEqual(move, exploratory)
        self.assertEqual(metadata["selection"], "exploratory")
        self.assertEqual(metadata["greedy_move"], greedy.uci())

    def test_deliberative_controller_rejects_major_material_refutations(self):
        board = chess.Board("rnbqkbnr/pppp1ppp/8/8/8/6P1/PPPPPP1P/RNBQKBNR b KQkq - 0 1")
        risky = chess.Move.from_uci("d8h4")

        refutation = self.zero.refutation_check(board, risky)
        tags = self.zero.move_role_tags(board, risky)

        self.assertIn(refutation["status"], {"watch", "unsafe"})
        self.assertIn("tactical_risk", tags)

    def test_deliberative_score_suppresses_refuted_forcing_bonus(self):
        board = chess.Board("4k3/3r4/8/8/8/8/8/3QK3 w - - 0 1")
        risky = chess.Move.from_uci("d1d7")
        quiet = chess.Move.from_uci("d1a4")

        def score(move):
            child_board = board.copy(stack=False)
            child_board.push(move)
            child = self.zero.PuctNode(child_board, prior=0.2, move=move, visit_count=4, value_sum=0.0)
            return self.zero.deliberative_score(
                board,
                child,
                8,
                self.zero.move_features(board, move),
                self.zero.refutation_check(board, move),
            )

        self.assertEqual(self.zero.refutation_check(board, risky)["status"], "unsafe")
        self.assertLess(score(risky), score(quiet))

    def test_best_reply_flags_checkmate_refutations(self):
        board = chess.Board("rnbqkbnr/pppp1ppp/8/4p3/8/5P2/PPPPP1PP/RNBQKBNR w KQkq - 0 2")
        weakening = chess.Move.from_uci("g2g4")

        reply = self.zero.best_reply_summary(board, weakening)
        refutation = self.zero.refutation_check(board, weakening)

        self.assertEqual(reply["uci"], "d8h4")
        self.assertTrue(reply["gives_check"])
        self.assertTrue(reply["is_checkmate"])
        self.assertEqual(refutation["status"], "unsafe")

    def test_value_backup_flips_perspective_by_ply(self):
        root = self.zero.PuctNode(chess.Board())
        child_board = chess.Board()
        child_board.push(chess.Move.from_uci("e2e4"))
        child = self.zero.PuctNode(child_board, move=chess.Move.from_uci("e2e4"))

        self.zero.backup([root, child], 0.75)

        self.assertEqual(child.visit_count, 1)
        self.assertAlmostEqual(child.value, 0.75)
        self.assertEqual(root.visit_count, 1)
        self.assertAlmostEqual(root.value, -0.75)

    def test_replay_buffer_dedupes_repeated_positions(self):
        board = chess.Board()
        move = chess.Move.from_uci("e2e4")
        record = {
            "fen": board.fen(),
            "chosen_move": move.uci(),
            "outcome": 1.0,
        }

        with tempfile.TemporaryDirectory() as tmp:
            replay_path = Path(tmp) / "replay.jsonl"
            first = self.zero.append_replay_records([record, dict(record)], replay_path)
            second = self.zero.append_replay_records([dict(record)], replay_path)

            lines = replay_path.read_text(encoding="utf-8").splitlines()

        self.assertEqual(first["added"], 1)
        self.assertEqual(second["added"], 0)
        self.assertEqual(len(lines), 1)

    def test_replay_buffer_updates_stale_duplicate_outcome_signal(self):
        board = chess.Board()
        record = {
            "fen": board.fen(),
            "chosen_move": "e2e4",
            "outcome": 0.0,
            "outcome_source": "none",
        }
        improved = dict(record)
        improved["outcome"] = 1.0
        improved["outcome_source"] = "self_material_adjudication"

        with tempfile.TemporaryDirectory() as tmp:
            replay_path = Path(tmp) / "replay.jsonl"
            self.zero.append_replay_records([record], replay_path)
            result = self.zero.append_replay_records([improved], replay_path)
            saved = json.loads(replay_path.read_text(encoding="utf-8"))

        self.assertEqual(result["added"], 0)
        self.assertEqual(result["updated_duplicates"], 1)
        self.assertEqual(saved["outcome"], 1.0)
        self.assertEqual(saved["outcome_source"], "self_material_adjudication")

    def test_replay_buffer_updates_stronger_same_direction_duplicate_signal(self):
        board = chess.Board()
        record = {
            "fen": board.fen(),
            "chosen_move": "e2e4",
            "outcome": self.zero.SELF_PLAY_DRAW_PENALTY,
            "outcome_source": "terminal_draw_non_win_penalty",
            "training_sources": {source: False for source in self.zero.FORBIDDEN_TRAINING_SOURCES},
        }
        stronger = dict(record)
        stronger["outcome"] = self.zero.SELF_PLAY_FAILED_CONVERSION_DRAW_PENALTY
        opposite = dict(record)
        opposite["outcome"] = 1.0

        with tempfile.TemporaryDirectory() as tmp:
            replay_path = Path(tmp) / "replay.jsonl"
            self.zero.append_replay_records([record], replay_path)
            result = self.zero.append_replay_records([stronger], replay_path)
            skipped = self.zero.append_replay_records([opposite], replay_path)
            saved = json.loads(replay_path.read_text(encoding="utf-8"))

        self.assertEqual(result["updated_duplicates"], 1)
        self.assertEqual(skipped["skipped_duplicates"], 1)
        self.assertEqual(saved["outcome"], self.zero.SELF_PLAY_FAILED_CONVERSION_DRAW_PENALTY)

    def test_training_preserves_forbidden_source_flags(self):
        board = chess.Board()
        record = {
            "fen": board.fen(),
            "chosen_move": "e2e4",
            "outcome": 1.0,
        }
        base = self.zero.PolicyValueNetwork(network_id="base", generation=3)

        with tempfile.TemporaryDirectory() as tmp:
            original_candidate = self.zero.CANDIDATE_NETWORK_PATH
            try:
                self.zero.CANDIDATE_NETWORK_PATH = Path(tmp) / "candidate-network.json"
                candidate = self.zero.train_from_replay(base=base, records=[record], epochs=2)
                saved = json.loads(self.zero.CANDIDATE_NETWORK_PATH.read_text(encoding="utf-8"))
            finally:
                self.zero.CANDIDATE_NETWORK_PATH = original_candidate

        self.assertEqual(candidate.generation, 4)
        self.assertGreater(candidate.source_positions, base.source_positions)
        self.assertTrue(saved["training_sources"])
        self.assertTrue(all(value is False for value in saved["training_sources"].values()))

    def test_non_terminal_self_play_can_use_material_outcome_signal(self):
        board = chess.Board()
        board.remove_piece_at(chess.D8)

        white = self.zero.self_play_material_outcome(board, chess.WHITE)
        black = self.zero.self_play_material_outcome(board, chess.BLACK)

        self.assertGreater(white, 0)
        self.assertLess(black, 0)

    def test_drawn_self_play_is_treated_as_not_winning_signal(self):
        white = self.zero.game_result_value("1/2-1/2", chess.WHITE)
        black = self.zero.game_result_value("1/2-1/2", chess.BLACK)

        self.assertLess(white, 0)
        self.assertEqual(white, black)

    def test_drawn_self_play_penalizes_failed_conversion_more(self):
        board = chess.Board()
        board.remove_piece_at(chess.D8)

        white = self.zero.self_play_draw_outcome(board, chess.WHITE)
        black = self.zero.self_play_draw_outcome(board, chess.BLACK)

        self.assertLess(white, self.zero.SELF_PLAY_DRAW_PENALTY)
        self.assertLess(black, 0)
        self.assertGreater(black, white)

    def test_draw_penalty_does_not_globally_depress_bias(self):
        board = chess.Board()
        record = {
            "fen": board.fen(),
            "chosen_move": "e2e4",
            "outcome": self.zero.SELF_PLAY_FAILED_CONVERSION_DRAW_PENALTY,
            "outcome_source": "terminal_draw_non_win_penalty",
        }
        base = self.zero.PolicyValueNetwork(network_id="base", generation=3)

        with tempfile.TemporaryDirectory() as tmp:
            original_candidate = self.zero.CANDIDATE_NETWORK_PATH
            try:
                self.zero.CANDIDATE_NETWORK_PATH = Path(tmp) / "candidate-network.json"
                candidate = self.zero.train_from_replay(base=base, records=[record], epochs=1)
            finally:
                self.zero.CANDIDATE_NETWORK_PATH = original_candidate

        self.assertEqual(candidate.weights["bias"], base.weights["bias"])
        self.assertLess(candidate.weights["center_to"], base.weights["center_to"])

    def test_risky_forcing_non_wins_get_stronger_feature_penalty(self):
        board = chess.Board("4k3/3r4/8/8/8/8/8/3QK3 w - - 0 1")
        record = {
            "fen": board.fen(),
            "chosen_move": "d1d7",
            "outcome": -0.05,
            "outcome_source": "terminal_draw_non_win_penalty",
        }

        self.assertTrue(self.zero.record_has_risky_forcing_non_win(record, -0.05))
        self.assertEqual(self.zero.training_feature_scale(record, "capture_value", -0.05), 1.6)
        self.assertEqual(self.zero.training_feature_scale(record, "moved_piece_risk", -0.05), 2.0)
        self.assertEqual(self.zero.training_feature_scale(record, "bias", -0.05), 0.0)

    def test_material_up_draw_penalizes_quiet_conversion_stalls(self):
        board = chess.Board("4k3/8/8/8/8/8/P7/4K2R w - - 0 1")
        stall = chess.Move.from_uci("e1d1")
        pawn_progress = chess.Move.from_uci("a2a3")
        record = {
            "fen": board.fen(),
            "chosen_move": stall.uci(),
            "outcome": self.zero.SELF_PLAY_FAILED_CONVERSION_DRAW_PENALTY,
            "outcome_source": "terminal_draw_non_win_penalty",
        }
        base = self.zero.PolicyValueNetwork(network_id="base", generation=3)

        self.assertEqual(self.zero.move_features(board, stall)["conversion_stall"], 1.0)
        self.assertEqual(self.zero.move_features(board, pawn_progress)["conversion_stall"], 0.0)
        self.assertTrue(self.zero.record_has_failed_conversion_stall(record, record["outcome"]))
        self.assertEqual(self.zero.training_feature_scale(record, "conversion_stall", record["outcome"]), 2.2)
        self.assertEqual(self.zero.training_feature_scale({**record, "outcome": 1.0}, "conversion_stall", 1.0), 0.0)

        with tempfile.TemporaryDirectory() as tmp:
            original_candidate = self.zero.CANDIDATE_NETWORK_PATH
            try:
                self.zero.CANDIDATE_NETWORK_PATH = Path(tmp) / "candidate-network.json"
                candidate = self.zero.train_from_replay(base=base, records=[record], epochs=1)
            finally:
                self.zero.CANDIDATE_NETWORK_PATH = original_candidate

        self.assertLess(candidate.weights["conversion_stall"], base.weights["conversion_stall"])
        self.assertEqual(candidate.weights["bias"], base.weights["bias"])

    def test_deliberative_score_downgrades_quiet_conversion_stalls(self):
        board = chess.Board("4k3/8/8/8/8/8/P7/4K2R w - - 0 1")
        stall = chess.Move.from_uci("e1d1")
        progress = chess.Move.from_uci("a2a3")

        def score(move):
            child_board = board.copy(stack=False)
            child_board.push(move)
            child = self.zero.PuctNode(child_board, prior=0.2, move=move, visit_count=4, value_sum=0.0)
            return self.zero.deliberative_score(
                board,
                child,
                8,
                self.zero.move_features(board, move),
                self.zero.refutation_check(board, move),
            )

        self.assertEqual(self.zero.move_features(board, stall)["conversion_stall"], 1.0)
        self.assertEqual(self.zero.move_features(board, progress)["conversion_stall"], 0.0)
        self.assertLess(score(stall), score(progress))

    def test_training_record_selection_keeps_recent_and_high_signal(self):
        records = [{"id": index, "outcome": 0.0} for index in range(10)]
        records[1]["outcome"] = -1.0
        records[3]["outcome"] = 0.8
        records[4]["outcome"] = -0.4

        selected = self.zero.select_training_records(records, max_records=6)
        selected_ids = [record["id"] for record in selected]

        self.assertEqual(len(selected), 6)
        self.assertEqual(selected_ids[-3:], [7, 8, 9])
        self.assertIn(1, selected_ids)
        self.assertIn(3, selected_ids)
        self.assertIn(4, selected_ids)

    def test_replay_training_signal_prioritizes_failed_draw_patterns(self):
        plain_board = chess.Board()
        plain_draw = {
            "id": "plain",
            "fen": plain_board.fen(),
            "chosen_move": "e2e4",
            "outcome": self.zero.SELF_PLAY_DRAW_PENALTY,
            "outcome_source": "terminal_draw_non_win_penalty",
        }
        conversion_board = chess.Board("4k3/8/8/8/8/8/P7/4K2R w - - 0 1")
        conversion_stall = {
            "id": "conversion",
            "fen": conversion_board.fen(),
            "chosen_move": "e1d1",
            "outcome": self.zero.SELF_PLAY_FAILED_CONVERSION_DRAW_PENALTY,
            "outcome_source": "terminal_draw_non_win_penalty",
        }
        risky_board = chess.Board("4k3/3r4/8/8/8/8/8/3QK3 w - - 0 1")
        risky_forcing = {
            "id": "risky",
            "fen": risky_board.fen(),
            "chosen_move": "d1d7",
            "outcome": self.zero.SELF_PLAY_DRAW_PENALTY,
            "outcome_source": "terminal_draw_non_win_penalty",
        }

        self.assertGreater(self.zero.replay_training_signal(conversion_stall), self.zero.replay_training_signal(plain_draw))
        self.assertGreater(self.zero.replay_training_signal(risky_forcing), self.zero.replay_training_signal(plain_draw))

        records = [plain_draw, conversion_stall, risky_forcing, {"id": "quiet", "outcome": 0.0}]
        records.extend({"id": f"recent-{index}", "outcome": 0.0} for index in range(4))
        selected_ids = [record["id"] for record in self.zero.select_training_records(records, max_records=4)]

        self.assertIn("conversion", selected_ids)
        self.assertIn("risky", selected_ids)

    def test_replay_training_clamps_runaway_feature_weights(self):
        base = self.zero.PolicyValueNetwork(
            network_id="base",
            generation=3,
            weights={
                **self.zero.DEFAULT_WEIGHTS,
                "gives_check": 100.0,
                "value_material": 100.0,
                "conversion_stall": -100.0,
            },
        )

        with tempfile.TemporaryDirectory() as tmp:
            original_candidate = self.zero.CANDIDATE_NETWORK_PATH
            try:
                self.zero.CANDIDATE_NETWORK_PATH = Path(tmp) / "candidate-network.json"
                candidate = self.zero.train_from_replay(base=base, records=[], epochs=1)
                saved = json.loads(self.zero.CANDIDATE_NETWORK_PATH.read_text(encoding="utf-8"))
            finally:
                self.zero.CANDIDATE_NETWORK_PATH = original_candidate

        self.assertEqual(candidate.weights["gives_check"], self.zero.WEIGHT_BOUNDS["gives_check"][1])
        self.assertEqual(candidate.weights["value_material"], self.zero.WEIGHT_BOUNDS["value_material"][1])
        self.assertEqual(candidate.weights["conversion_stall"], self.zero.WEIGHT_BOUNDS["conversion_stall"][0])
        self.assertEqual(saved["weights"], candidate.weights)

    def test_wisdom_delta_is_readable_and_external_label_safe(self):
        board = chess.Board()
        game = {
            "result": "1/2-1/2",
            "outcome_source": "terminal_draw_non_win_penalty",
            "records": [
                {
                    "fen": board.fen(),
                    "chosen_move": "e2e4",
                    "selection": "exploratory",
                    "outcome": -0.05,
                    "training_sources": {source: False for source in self.zero.FORBIDDEN_TRAINING_SOURCES},
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            selfplay = root / "selfplay.json"
            latest_json = root / "wisdom.json"
            latest_md = root / "wisdom.md"
            log_path = root / "wisdom.jsonl"
            selfplay.write_text(json.dumps(game), encoding="utf-8")
            result = self.zero.write_wisdom_delta(
                [selfplay],
                {"status": "candidate_not_stronger_yet", "replay_added": 1},
                {"network_id": "candidate", "generation": 2, "source_positions": 1},
                {"promoted": False},
                latest_json_path=latest_json,
                latest_markdown_path=latest_md,
                log_path=log_path,
            )
            saved = json.loads(latest_json.read_text(encoding="utf-8"))
            markdown = latest_md.read_text(encoding="utf-8")

        self.assertGreaterEqual(result["lesson_count"], 1)
        self.assertIn("Human-Readable Zero Wisdom Delta", markdown)
        self.assertIn("Treat repeated drawn self-play", markdown)
        self.assertNotIn(board.fen(), markdown)
        self.assertTrue(all(value is False for value in saved["training_sources"].values()))

    def test_promotion_gate_can_promote_candidate_without_external_labels(self):
        current = self.zero.PolicyValueNetwork(network_id="current", generation=1)
        candidate = self.zero.PolicyValueNetwork(network_id="candidate", generation=2)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            current_path = tmp_path / "current.json"
            candidate_path = tmp_path / "candidate.json"
            log_path = tmp_path / "promotion.jsonl"
            current.save(current_path)
            candidate.save(candidate_path)
            original_log = self.zero.PROMOTION_LOG_PATH
            try:
                self.zero.PROMOTION_LOG_PATH = log_path
                result = self.zero.promotion_gate(current_path, candidate_path, force=True)
            finally:
                self.zero.PROMOTION_LOG_PATH = original_log
            promoted = json.loads(current_path.read_text(encoding="utf-8"))
            log_lines = log_path.read_text(encoding="utf-8").count("\n")

        self.assertTrue(result["promoted"])
        self.assertEqual(promoted["network_id"], "candidate")
        self.assertEqual(log_lines, 1)

    def test_exact_fen_move_rules_are_flagged_but_concepts_are_allowed(self):
        exact = "In rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1, best move e2e4."
        concept = "Prefer development when the center is stable and king safety is unresolved."

        violations = self.zero.find_exact_move_rules(exact)
        allowed = self.zero.find_exact_move_rules(concept)

        self.assertEqual(violations[0]["move"], "e2e4")
        self.assertEqual(allowed, [])

    def test_benchmark_ladder_keeps_external_engines_evaluation_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stockfish = root / "stockfish.exe"
            lc0 = root / "lc0.exe"
            stockfish.write_text("", encoding="utf-8")
            lc0.write_text("", encoding="utf-8")
            config = root / "engines.json"
            config.write_text(
                json.dumps(
                    [
                        {"name": "Stockfish 18", "path": str(stockfish), "version": "18"},
                        {"name": "Lc0", "path": str(lc0), "version": "latest"},
                    ]
                ),
                encoding="utf-8",
            )

            rows = self.zero.benchmark_ladder(config)

        by_name = {row["name"]: row for row in rows}
        self.assertTrue(by_name["Full Stockfish 18"]["available"])
        self.assertFalse(by_name["Full Stockfish 18"]["training_allowed"])
        self.assertTrue(by_name["Lc0 installed network"]["available"])
        self.assertFalse(by_name["Lc0 installed network"]["training_allowed"])
        self.assertTrue(by_name["Codex-chess-zero deliberative"]["training_allowed"])


if __name__ == "__main__":
    unittest.main()
