import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

import chess

from tools import mirror_fastchess_live_pgn as mirror_module
from tools.mirror_fastchess_live_pgn import (
    build_game,
    collect_engine_tracks,
    game_with_timeout,
    log_timestamp_ms,
    mirror,
    run_artifacts_recent,
    select_board_game,
    select_engine_state,
    status_text,
)


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class TimeLossHandlingTests(unittest.TestCase):
    def test_codex_engine_sanitizes_unicode_uci_info_output(self):
        module = load_module("codex_chess_uci_sanitize_test", ROOT / "engines" / "codex-chess" / "codex_chess_uci.py")

        class StrictAsciiStdout:
            def __init__(self):
                self.text = ""

            def write(self, value: str):
                value.encode("ascii")
                self.text += value

            def flush(self):
                pass

        original_stdout = sys.stdout
        strict_stdout = StrictAsciiStdout()
        try:
            sys.stdout = strict_stdout
            emitted = module.emit_uci_line("info string Challenge White\u2019s pawn chain \u2014 now", optional=True)
        finally:
            sys.stdout = original_stdout

        self.assertTrue(emitted)
        self.assertEqual(strict_stdout.text, "info string Challenge White's pawn chain - now\n")

    def test_codex_engine_bounds_move_timeout_by_clock(self):
        module = load_module("codex_chess_uci_budget_test", ROOT / "engines" / "codex-chess" / "codex_chess_uci.py")
        client = module.CodexAppServer("gpt-test", "high")

        self.assertEqual(client.move_timeout_seconds(300000), 45)
        self.assertEqual(client.move_timeout_seconds(204125), 45)
        self.assertEqual(client.move_timeout_seconds(172213), 43)
        self.assertEqual(client.move_timeout_seconds(59000), 14)
        self.assertEqual(client.move_timeout_seconds(24000), 3)
        self.assertEqual(client.retry_timeout_seconds(204125, 45.0), 8)

    def test_codex_engine_urgent_retry_prompt_drops_context_and_comments(self):
        module = load_module("codex_chess_uci_retry_prompt_test", ROOT / "engines" / "codex-chess" / "codex_chess_uci.py")
        client = module.CodexAppServer("gpt-test", "high")
        prompt = {
            "legal_moves": ["e2e4"],
            "learner_context": {"memory": "large"},
            "comment_policy": "Optional comment",
            "turn_timeout_seconds": 25,
        }

        retry_prompt = client.urgent_retry_prompt(prompt, 8, "timeout")

        self.assertNotIn("learner_context", retry_prompt)
        self.assertEqual(retry_prompt["turn_timeout_seconds"], 8)
        self.assertEqual(retry_prompt["_turn_effort"], "low")
        self.assertIn("copy one legal uci", retry_prompt["learner_context_summary"])
        self.assertIn("empty string", retry_prompt["comment_policy"])
        self.assertEqual(prompt["turn_timeout_seconds"], 25)

    def test_codex_engine_non_urgent_comments_are_required_and_visible(self):
        module = load_module("codex_chess_uci_comment_test", ROOT / "engines" / "codex-chess" / "codex_chess_uci.py")
        client = module.CodexAppServer("gpt-test", "high")

        schema = client.comment_schema(True)

        self.assertEqual(schema["minLength"], 1)
        self.assertEqual(schema["maxLength"], 240)
        self.assertEqual(client.visible_move_comment("e2e4", "", True), "Selected e2e4; model returned no comment.")
        self.assertEqual(client.visible_move_comment("e2e4", "  contests   the center  ", True), "contests the center")

    def test_codex_engine_urgent_comments_may_stay_empty(self):
        module = load_module("codex_chess_uci_urgent_comment_test", ROOT / "engines" / "codex-chess" / "codex_chess_uci.py")
        client = module.CodexAppServer("gpt-test", "high")

        schema = client.comment_schema(False)

        self.assertNotIn("minLength", schema)
        self.assertEqual(client.visible_move_comment("e2e4", "", False), "")

    def test_codex_engine_lean_context_is_smaller_than_full_context(self):
        module = load_module("codex_chess_uci_context_test", ROOT / "engines" / "codex-chess" / "codex_chess_uci.py")
        with tempfile.TemporaryDirectory() as tmp:
            context_dir = Path(tmp)
            knowledgebase_dir = context_dir / "knowledgebase"
            skills_dir = context_dir / "skills"
            tools_dir = context_dir / "tools"
            knowledgebase_dir.mkdir()
            (skills_dir / "master-game-wisdom").mkdir(parents=True)
            tools_dir.mkdir()
            (knowledgebase_dir / "master-wisdom.md").write_text("Always use the authored principles.\n", encoding="utf-8")
            (knowledgebase_dir / "other.md").write_text("Recent but not pinned.\n", encoding="utf-8")
            (skills_dir / "master-game-wisdom" / "SKILL.md").write_text("Use master-game principles.\n", encoding="utf-8")

            original_paths = (
                module.CONTEXT_DIR,
                module.MEMORY_PATH,
                module.SKILLS_DIR,
                module.KNOWLEDGEBASE_DIR,
                module.TOOLS_DIR,
                module.FEN_KNOWLEDGE_PATH,
                module.STRATEGY_LESSONS_PATH,
            )
            module.CONTEXT_DIR = context_dir
            module.MEMORY_PATH = context_dir / "MEMORY.md"
            module.SKILLS_DIR = skills_dir
            module.KNOWLEDGEBASE_DIR = knowledgebase_dir
            module.TOOLS_DIR = tools_dir
            module.FEN_KNOWLEDGE_PATH = knowledgebase_dir / "fen-curriculum-lessons.md"
            module.STRATEGY_LESSONS_PATH = knowledgebase_dir / "strategy-lessons.md"
            try:
                client = module.CodexAppServer("gpt-test", "high", use_memory=True, use_skills=True, learning_mode=True)

                full_context = client.learner_context("full")
                lean_context = client.learner_context("lean")
            finally:
                (
                    module.CONTEXT_DIR,
                    module.MEMORY_PATH,
                    module.SKILLS_DIR,
                    module.KNOWLEDGEBASE_DIR,
                    module.TOOLS_DIR,
                    module.FEN_KNOWLEDGE_PATH,
                    module.STRATEGY_LESSONS_PATH,
                ) = original_paths

        self.assertEqual(lean_context["profile"], "lean")
        self.assertLessEqual(len(lean_context["memory"]), len(full_context["memory"]))
        self.assertLessEqual(len(lean_context["fen_knowledge"]), len(full_context["fen_knowledge"]))
        self.assertLessEqual(len(lean_context["strategy_lessons"]), len(full_context["strategy_lessons"]))
        self.assertLessEqual(len(lean_context["knowledgebase"]), len(full_context["knowledgebase"]))
        self.assertLessEqual(len(lean_context["skills"]), len(full_context["skills"]))
        self.assertLessEqual(len(lean_context["tools"]), len(full_context["tools"]))
        self.assertIn("apply the existing authored master wisdom in knowledgebase/master-wisdom.md first", full_context["policy"].lower())
        self.assertIn("skills/master-game-wisdom/skill.md", full_context["policy"].lower())
        self.assertIn("authored master-wisdom principle", lean_context["policy"].lower())
        self.assertEqual(full_context["knowledgebase"][0]["path"], "master-wisdom.md")
        self.assertEqual(lean_context["knowledgebase"][0]["path"], "master-wisdom.md")
        self.assertTrue(any(item["path"] == "master-game-wisdom/SKILL.md" for item in full_context["skills"]))
        self.assertTrue(any(item["path"] == "master-game-wisdom/SKILL.md" for item in lean_context["skills"]))
        self.assertIn("tools_path", full_context)

    def test_codex_engine_training_effort_is_lower_than_default_high(self):
        module = load_module("codex_chess_uci_fast_training_effort_test", ROOT / "engines" / "codex-chess" / "codex_chess_uci.py")

        baseline = module.CodexAppServer("gpt-test", "high")
        learner = module.CodexAppServer("gpt-test", "high", learning_mode=True)
        zero = module.CodexAppServer("gpt-test", "high", learning_mode=True, zero_mode=True)

        self.assertEqual(baseline.move_effort(False), "high")
        self.assertEqual(learner.move_effort(False), "medium")
        self.assertEqual(learner.move_effort(True), "low")
        self.assertEqual(zero.move_effort(False), "low")

    def test_codex_zero_mode_uses_fast_lean_first_principles_context(self):
        module = load_module("codex_chess_uci_zero_context_test", ROOT / "engines" / "codex-chess" / "codex_chess_uci.py")
        client = module.CodexAppServer(
            "gpt-test",
            "high",
            use_memory=True,
            use_skills=False,
            learning_mode=True,
            zero_mode=True,
            force_lean_context=True,
        )

        context = client.learner_context("lean")

        self.assertTrue(client.zero_mode)
        self.assertTrue(client.force_lean_context)
        self.assertEqual(client.move_timeout_seconds(None), 6)
        self.assertEqual(client.move_timeout_seconds(600000), 6)
        self.assertEqual(client.retry_timeout_seconds(600000, 0), 3)
        self.assertEqual(context["profile"], "lean")
        self.assertEqual(context["skills"], [])
        self.assertEqual(context["tools"], [])
        self.assertIn("Zero mode", context["policy"])
        self.assertIn("first principles", context["policy"])

    def test_codex_zero_local_puct_does_not_start_model_turn(self):
        module = load_module("codex_chess_uci_zero_local_puct_test", ROOT / "engines" / "codex-chess" / "codex_chess_uci.py")
        client = module.CodexAppServer("gpt-test", "high", learning_mode=True, zero_mode=True)

        async def fail_start():
            raise AssertionError("start should not be called while ZeroLocalPuct is enabled")

        client.start = fail_start
        original_env = {name: os.environ.get(name) for name in ["CODEX_CHESS_ZERO_LOCAL_PUCT", "CODEX_CHESS_ZERO_PUCT_VISITS", "CODEX_CHESS_ZERO_PUCT_TIME_LIMIT_MS"]}
        try:
            os.environ["CODEX_CHESS_ZERO_LOCAL_PUCT"] = "true"
            os.environ["CODEX_CHESS_ZERO_PUCT_VISITS"] = "2"
            os.environ["CODEX_CHESS_ZERO_PUCT_TIME_LIMIT_MS"] = "100"
            board = chess.Board()
            move = asyncio.run(client.choose_move(board, {"wtime": 300000, "btime": 300000}, []))
        finally:
            for name, value in original_env.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

        self.assertIn(chess.Move.from_uci(move), board.legal_moves)

    def test_codex_engine_material_safety_flags_live_qxd4_blunder(self):
        module = load_module("codex_chess_uci_material_safety_test", ROOT / "engines" / "codex-chess" / "codex_chess_uci.py")
        board = chess.Board("r4rk1/3p1ppp/p1p2q2/8/2pb4/8/PPP2PPP/R2Q1RK1 w - - 0 17")
        legal_moves = [move.uci() for move in board.legal_moves]

        summary = module.material_safety_summary(board, legal_moves)
        qxd4 = next(item for item in summary["risky_moves"] if item["uci"] == "d1d4")

        self.assertEqual(qxd4["san"], "Qxd4")
        self.assertEqual(qxd4["moved_piece"], "queen")
        self.assertEqual(qxd4["captured_piece"], "bishop")
        self.assertEqual(qxd4["reply_uci"], "f6d4")
        self.assertEqual(qxd4["reply_san"], "Qxd4")
        self.assertTrue(qxd4["captures_moved_piece"])
        self.assertGreaterEqual(qxd4["material_swing_cp"], 900)
        self.assertIn("capturing the moved queen", qxd4["warning"])

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
            "total": 1,
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

    def test_live_mirror_marks_wall_clock_expired_clock_in_pgn_and_status(self):
        current = {
            "game": 1,
            "total": 1,
            "white": "Codex-chess",
            "black": "Codex-chess-learner",
            "result": "*",
            "reason": "",
            "finished": False,
        }
        state = {
            "moves": ["e2e4"],
            "wtime": 180000,
            "btime": 5000,
            "updated_at": 100000,
            "running_side": "Black",
        }

        game = build_game(current, state, now_ms=106001)
        completed = game_with_timeout(current, state, now_ms=106001)
        status = json.loads(status_text([completed], Path("live.pgn"), 1))

        self.assertEqual(game.headers["Result"], "1-0")
        self.assertEqual(game.headers["BlackClockMs"], "0")
        self.assertEqual(game.headers["ClockRunningSide"], "")
        self.assertIn("Codex-chess-learner (Black) lost on time", game.headers["Termination"])
        self.assertTrue(status["games"][0]["finished"])
        self.assertEqual(status["games"][0]["status"], "Completed")
        self.assertIn("lost on time", status["games"][0]["reason"])

    def test_live_mirror_advances_from_inferred_timeout_to_next_running_game(self):
        stdout_text = "\n".join(
            [
                "Started game 1 of 100 (Codex-chess vs Codex-chess-learner)",
                "Started game 2 of 100 (Codex-chess-learner vs Codex-chess)",
            ]
        )
        expired_first_game_track = "\n".join(
            [
                "[2026-05-12 10:00:00] > position startpos",
                "[2026-05-12 10:00:00] > go wtime 1000 btime 300000",
            ]
        )
        running_second_game_track = "\n".join(
            [
                "[2026-05-12 10:01:00] > position startpos",
                "[2026-05-12 10:01:00] > go wtime 300000 btime 300000",
                "[2026-05-12 10:01:05] > position startpos moves e2e4 e7e5 g1f3 b8c6",
                "[2026-05-12 10:01:05] > go wtime 295000 btime 295000",
            ]
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stdout_path = root / "run-launch.out.log"
            log_dir = root / "logs"
            output_path = root / "live" / "run-live.pgn"
            log_dir.mkdir()
            output_path.parent.mkdir()
            stdout_path.write_text(stdout_text, encoding="utf-8")
            os.utime(stdout_path, (log_timestamp_ms("2026-05-12 09:59:55") / 1000, log_timestamp_ms("2026-05-12 09:59:55") / 1000))
            (log_dir / "codex-chess-game1.log").write_text(expired_first_game_track, encoding="utf-8")
            (log_dir / "codex-chess-game2.log").write_text(running_second_game_track, encoding="utf-8")

            original_now = mirror_module.current_epoch_ms
            mirror_module.current_epoch_ms = lambda: log_timestamp_ms("2026-05-12 10:01:06")
            try:
                mirror(stdout_path, log_dir, output_path, interval=0, once=True)
            finally:
                mirror_module.current_epoch_ms = original_now
            status = json.loads(output_path.with_suffix(".status.json").read_text(encoding="utf-8"))
            with Path(status["output_pgn"]).open("r", encoding="utf-8") as handle:
                game = chess.pgn.read_game(handle)

        self.assertIsNotNone(game)
        assert game is not None
        self.assertEqual(game.headers["Round"], "2")
        self.assertEqual(game.headers["Result"], "*")
        self.assertIn("1. e4 e5 2. Nf3 Nc6", str(game))
        self.assertEqual(status["locked_game"], 2)
        self.assertEqual(status["games"][0]["status"], "Completed")
        self.assertIn("lost on time", status["games"][0]["reason"])
        self.assertEqual(status["games"][1]["status"], "In progress")
        self.assertTrue(status["games"][1]["is_board_game"])

    def test_live_mirror_advances_stale_completed_selection_to_running_game(self):
        games = [
            {"game": 1, "finished": True},
            {"game": 2, "finished": True},
            {"game": 3, "finished": True},
            {"game": 4, "finished": False},
        ]

        current, locked_game = select_board_game(games, 3)

        self.assertEqual(current["game"], 4)
        self.assertEqual(locked_game, 4)

    def test_live_mirror_keeps_explicit_completed_selection_pinned(self):
        games = [
            {"game": 1, "finished": True},
            {"game": 2, "finished": True},
            {"game": 3, "finished": True},
            {"game": 4, "finished": False},
        ]

        current, locked_game = select_board_game(games, 3, pin_locked=True)

        self.assertEqual(current["game"], 3)
        self.assertEqual(locked_game, 3)

    def test_live_mirror_keeps_selected_game_when_it_is_running(self):
        games = [
            {"game": 1, "finished": True},
            {"game": 2, "finished": False},
            {"game": 3, "finished": False},
        ]

        current, locked_game = select_board_game(games, 3)

        self.assertEqual(current["game"], 3)
        self.assertEqual(locked_game, 3)

    def test_live_mirror_keeps_repeated_opening_tracks_separate(self):
        first_game = "\n".join(
            [
                "[2026-05-12 10:00:00] > position startpos",
                "[2026-05-12 10:00:00] > go wtime 300000 btime 300000",
                "[2026-05-12 10:00:10] > position startpos moves e2e4 e7e5",
                "[2026-05-12 10:00:10] > go wtime 290000 btime 290000",
            ]
        )
        second_game = "\n".join(
            [
                "[2026-05-12 10:05:00] > position startpos",
                "[2026-05-12 10:05:00] > go wtime 300000 btime 300000",
                "[2026-05-12 10:05:10] > position startpos moves e2e4 e7e5 g1f3",
                "[2026-05-12 10:05:10] > go wtime 288000 btime 292000",
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp)
            (log_dir / "codex-chess-first.log").write_text(first_game, encoding="utf-8")
            (log_dir / "codex-chess-second.log").write_text(second_game, encoding="utf-8")

            tracks = collect_engine_tracks(log_dir)

        self.assertEqual([track["moves"] for track in tracks], [["e2e4", "e7e5"], ["e2e4", "e7e5", "g1f3"]])

    def test_live_mirror_uses_freshest_track_when_live_game_exceeds_loaded_tracks(self):
        older_track = "\n".join(
            [
                "[2026-05-12 10:00:00] > position startpos",
                "[2026-05-12 10:00:00] > go wtime 300000 btime 300000",
                "[2026-05-12 10:00:10] > position startpos moves e2e4 e7e5",
                "[2026-05-12 10:00:10] > go wtime 290000 btime 290000",
            ]
        )
        current_track = "\n".join(
            [
                "[2026-05-12 10:05:00] > position startpos",
                "[2026-05-12 10:05:00] > go wtime 300000 btime 300000",
                "[2026-05-12 10:05:10] > position startpos moves d2d4 d7d5",
                "[2026-05-12 10:05:10] > go wtime 288000 btime 292000",
            ]
        )
        games = [
            {"game": 1, "finished": True},
            {"game": 2, "finished": True},
            {"game": 3, "finished": True},
            {"game": 4, "finished": False},
        ]
        current = games[-1]
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp)
            (log_dir / "codex-chess-older.log").write_text(older_track, encoding="utf-8")
            (log_dir / "codex-chess-current.log").write_text(current_track, encoding="utf-8")

            state, locked_moves = select_engine_state(log_dir, games, current, None)

        self.assertEqual(state["moves"], ["d2d4", "d7d5"])
        self.assertEqual(locked_moves, ["d2d4", "d7d5"])

    def test_live_mirror_prefers_fresh_single_game_track_over_stale_timeout_track(self):
        games = [
            {"game": 1, "finished": False, "white": "Codex-chess", "black": "Codex-chess-learner"},
        ]
        stale_track = {
            "moves": ["e2e4", "c7c5"],
            "updated_at": 1000,
            "wtime": 0,
            "btime": 200000,
            "running_side": "White",
            "clocks_by_ply": {},
        }
        current_track = {
            "moves": ["g1f3", "d7d5", "d2d4", "g8f6"],
            "updated_at": 2000,
            "wtime": 290000,
            "btime": 291000,
            "running_side": "White",
            "clocks_by_ply": {},
        }

        state, locked_moves = select_engine_state(
            Path("."),
            games,
            games[0],
            None,
            tracks=[stale_track, current_track],
            now_ms=2500,
        )

        self.assertEqual(state["moves"], current_track["moves"])
        self.assertEqual(locked_moves, current_track["moves"])

    def test_live_mirror_does_not_pin_unfinished_game_to_stale_locked_moves(self):
        games = [
            {"game": 1, "finished": True},
            {"game": 2, "finished": True},
            {"game": 3, "finished": True},
            {"game": 4, "finished": True},
            {"game": 5, "finished": True},
            {"game": 6, "finished": True},
            {"game": 7, "finished": False},
        ]
        stale_line = ["e2e4", "e7e5", "g1f3", "b8c6", "f1b5", "a7a6", "b5a4", "g8f6", "e1g1", "f8e7", "f1e1", "b7b5", "a4b3", "e8g8", "c2c3", "c8b7", "d2d4", "e5d4", "c3d4", "c6d4", "d1d4", "f6e4", "d4e4", "b7e4", "e1e4", "d7d5", "e4e8"]
        current_line = stale_line[:26]
        tracks = [
            {"moves": ["e2e4"], "updated_at": 1000, "clocks_by_ply": {}},
            {"moves": stale_line, "updated_at": 2000, "clocks_by_ply": {}},
            {
                "moves": current_line,
                "updated_at": 3000,
                "wtime": 138172,
                "btime": 133638,
                "running_side": "White",
                "clocks_by_ply": {},
            },
        ]

        state, locked = select_engine_state(Path("."), games, games[-1], stale_line, tracks=tracks)

        self.assertEqual(state["moves"], current_line)
        self.assertEqual(locked, current_line)

    def test_live_mirror_detects_stale_run_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stdout_path = root / "run-launch.out.log"
            pgnout_path = root / "run.pgn"
            stdout_path.write_text("Started game 1 of 100 (A vs B)", encoding="utf-8")
            pgnout_path.write_text("", encoding="utf-8")
            old = time.time() - (45 * 60)
            os.utime(stdout_path, (old, old))
            os.utime(pgnout_path, (old, old))

            recent = run_artifacts_recent(stdout_path, pgnout_path, int(time.time() * 1000))

        self.assertFalse(recent)

    def test_live_mirror_scopes_engine_tracks_to_current_run_start(self):
        stale_track = "\n".join(
            [
                "[2026-05-12 10:00:00] > position startpos",
                "[2026-05-12 10:00:00] > go wtime 300000 btime 300000",
                "[2026-05-12 10:00:10] > position startpos moves e2e4 c7c5",
                "[2026-05-12 10:00:10] > go wtime 290000 btime 290000",
            ]
        )
        current_track = "\n".join(
            [
                "[2026-05-12 10:05:00] > position startpos",
                "[2026-05-12 10:05:00] > go wtime 300000 btime 300000",
                "[2026-05-12 10:05:10] > position startpos moves e2e4 e7e5",
                "[2026-05-12 10:05:10] > go wtime 288000 btime 292000",
            ]
        )
        since_ms = log_timestamp_ms("2026-05-12 10:04:55")
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp)
            (log_dir / "codex-chess-stale.log").write_text(stale_track, encoding="utf-8")
            (log_dir / "codex-chess-current.log").write_text(current_track, encoding="utf-8")

            tracks = collect_engine_tracks(log_dir, since_ms)

        self.assertEqual([track["moves"] for track in tracks], [["e2e4", "e7e5"]])

    def test_live_mirror_e2e_keeps_explicit_completed_selection_pinned(self):
        stdout_text = "\n".join(
            [
                "Started game 1 of 100 (Codex-chess vs Codex-chess-learner)",
                "Warning; Illegal move 0000 played by Codex-chess",
                "Finished game 1 (Codex-chess vs Codex-chess-learner): 0-1 {White makes an illegal move}",
                "Started game 2 of 100 (Codex-chess-learner vs Codex-chess)",
            ]
        )
        first_game_track = "\n".join(
            [
                "[2026-05-12 10:00:00] > position startpos",
                "[2026-05-12 10:00:00] > go wtime 999999999 btime 999999999",
                "[2026-05-12 10:00:10] > position startpos moves e2e4 e7e5 g1f3 b8c6 f1b5",
                "[2026-05-12 10:00:10] > go wtime 999990000 btime 999990000",
            ]
        )
        current_game_track = "\n".join(
            [
                "[2026-05-12 10:05:00] > position startpos",
                "[2026-05-12 10:05:00] > go wtime 999999999 btime 999999999",
                "[2026-05-12 10:05:10] > position startpos moves e2e4 e7e5 g1f3 b8c6 f1c4",
                "[2026-05-12 10:05:10] > go wtime 999990000 btime 999990000",
            ]
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stdout_path = root / "run-launch.out.log"
            log_dir = root / "logs"
            output_path = root / "live" / "run-live.pgn"
            log_dir.mkdir()
            output_path.parent.mkdir()
            stdout_path.write_text(stdout_text, encoding="utf-8")
            os.utime(stdout_path, (log_timestamp_ms("2026-05-12 09:59:55") / 1000, log_timestamp_ms("2026-05-12 09:59:55") / 1000))
            (output_path.with_suffix(".selection.json")).write_text(json.dumps({"locked_game": 1}), encoding="utf-8")
            (log_dir / "codex-chess-game1.log").write_text(first_game_track, encoding="utf-8")
            (log_dir / "codex-chess-game2.log").write_text(current_game_track, encoding="utf-8")

            mirror(stdout_path, log_dir, output_path, interval=0, once=True)
            status = json.loads(output_path.with_suffix(".status.json").read_text(encoding="utf-8"))
            with Path(status["output_pgn"]).open("r", encoding="utf-8") as handle:
                game = chess.pgn.read_game(handle)

        self.assertIsNotNone(game)
        assert game is not None
        self.assertEqual(game.headers["Round"], "1")
        self.assertEqual(game.headers["Result"], "0-1")
        self.assertIn("3. Bb5", str(game))
        self.assertEqual(status["locked_game"], 1)
        self.assertEqual(status["games"][0]["status"], "Completed")
        self.assertEqual(status["games"][1]["status"], "In progress")
        self.assertTrue(status["games"][0]["is_board_game"])
        self.assertFalse(status["games"][1]["is_board_game"])

    def test_live_mirror_reuses_existing_completed_pinned_game_after_restart(self):
        stdout_text = "\n".join(
            [
                "Started game 1 of 100 (Codex-chess vs Codex-chess-learner)",
                "Finished game 1 (Codex-chess vs Codex-chess-learner): 0-1 {Black mates}",
                "Started game 2 of 100 (Codex-chess-learner vs Codex-chess)",
            ]
        )
        current_game_track = "\n".join(
            [
                "[2026-05-12 10:05:00] > position startpos",
                "[2026-05-12 10:05:00] > go wtime 999999999 btime 999999999",
                "[2026-05-12 10:05:10] > position startpos moves d2d4 d7d5",
                "[2026-05-12 10:05:10] > go wtime 999990000 btime 999990000",
            ]
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stdout_path = root / "run-launch.out.log"
            log_dir = root / "logs"
            output_path = root / "live" / "run-live.pgn"
            existing_game_path = root / "live" / "run-20260512-095500-game-1-live.pgn"
            log_dir.mkdir()
            output_path.parent.mkdir()
            stdout_path.write_text(stdout_text, encoding="utf-8")
            os.utime(stdout_path, (log_timestamp_ms("2026-05-12 09:59:55") / 1000, log_timestamp_ms("2026-05-12 09:59:55") / 1000))
            existing_game_path.write_text(
                "\n".join(
                    [
                        '[Event "FastChess live mirror"]',
                        '[Site "C:/dev/chess-harness-codex"]',
                        '[Date "2026.05.12"]',
                        '[Round "1"]',
                        '[White "Codex-chess"]',
                        '[Black "Codex-chess-learner"]',
                        '[Result "*"]',
                        '[TotalGames "100"]',
                        "",
                        "1. a3 e5 *",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            (output_path.with_suffix(".selection.json")).write_text(json.dumps({"locked_game": 1}), encoding="utf-8")
            (log_dir / "codex-chess-game2.log").write_text(current_game_track, encoding="utf-8")

            mirror(stdout_path, log_dir, output_path, interval=0, once=True)
            status = json.loads(output_path.with_suffix(".status.json").read_text(encoding="utf-8"))
            text = existing_game_path.read_text(encoding="utf-8")

        self.assertEqual(Path(status["output_pgn"]), existing_game_path)
        self.assertEqual(status["locked_game"], 1)
        self.assertEqual(status["games"][0]["status"], "Completed")
        self.assertTrue(status["games"][0]["is_board_game"])
        self.assertIn('[Result "0-1"]', text)
        self.assertIn('[Termination "Black mates"]', text)
        self.assertIn("1. a3 e5", text)
        self.assertNotIn("1. d4 d5", text)

    def test_live_mirror_e2e_reconciles_stale_stdout_with_pgnout_and_active_track(self):
        stdout_text = "\n".join(
            [
                "Started game 1 of 100 (Codex-chess vs Codex-chess-learner)",
                "Finished game 1 (Codex-chess vs Codex-chess-learner): 1-0 {White wins}",
                "Started game 2 of 100 (Codex-chess-learner vs Codex-chess)",
                "Finished game 2 (Codex-chess-learner vs Codex-chess): 0-1 {Black wins}",
                "Started game 3 of 100 (Codex-chess vs Codex-chess-learner)",
            ]
        )
        pgnout_text = "\n\n".join(
            [
                "\n".join(
                    [
                        '[Event "FastChess Tournament"]',
                        '[Round "1"]',
                        '[White "Codex-chess"]',
                        '[Black "Codex-chess-learner"]',
                        '[Result "1-0"]',
                        '[Termination "White wins"]',
                        "",
                        "1. e4 e5 1-0",
                    ]
                ),
                "\n".join(
                    [
                        '[Event "FastChess Tournament"]',
                        '[Round "2"]',
                        '[White "Codex-chess-learner"]',
                        '[Black "Codex-chess"]',
                        '[Result "0-1"]',
                        '[Termination "Black wins"]',
                        "",
                        "1. c4 e5 0-1",
                    ]
                ),
                "\n".join(
                    [
                        '[Event "FastChess Tournament"]',
                        '[Round "3"]',
                        '[White "Codex-chess"]',
                        '[Black "Codex-chess-learner"]',
                        '[Result "1-0"]',
                        '[Termination "White wins"]',
                        "",
                        "1. Nf3 d5 1-0",
                    ]
                ),
                "\n".join(
                    [
                        '[Event "FastChess Tournament"]',
                        '[Round "4"]',
                        '[White "Codex-chess-learner"]',
                        '[Black "Codex-chess"]',
                        '[Result "0-1"]',
                        '[Termination "Black wins"]',
                        "",
                        "1. Nc3 d5 0-1",
                    ]
                ),
                "",
            ]
        )
        tracks = [
            ("game1", "2026-05-12 10:00:00", "e2e4 e7e5 g1f3"),
            ("game2", "2026-05-12 10:05:00", "c2c4 e7e5 b1c3"),
            ("game3", "2026-05-12 10:10:00", "g1f3 d7d5 d2d4"),
            ("game4", "2026-05-12 10:15:00", "b1c3 d7d5 e2e4"),
            ("game5", "2026-05-12 10:20:00", "d2d4 d7d5 c1f4"),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stdout_path = root / "run-launch.out.log"
            pgnout_path = root / "run.pgn"
            log_dir = root / "logs"
            output_path = root / "live" / "run-live.pgn"
            log_dir.mkdir()
            output_path.parent.mkdir()
            stdout_path.write_text(stdout_text, encoding="utf-8")
            pgnout_path.write_text(pgnout_text, encoding="utf-8")
            os.utime(stdout_path, (log_timestamp_ms("2026-05-12 09:59:55") / 1000, log_timestamp_ms("2026-05-12 09:59:55") / 1000))
            for name, first_ts, moves in tracks:
                later_ts = first_ts[:-2] + "10"
                (log_dir / f"codex-chess-{name}.log").write_text(
                    "\n".join(
                        [
                            f"[{first_ts}] > position startpos",
                            f"[{first_ts}] > go wtime 999999999 btime 999999999",
                            f"[{later_ts}] > position startpos moves {moves}",
                            f"[{later_ts}] > go wtime 999990000 btime 999990000",
                        ]
                    ),
                    encoding="utf-8",
                )

            original_now = mirror_module.current_epoch_ms
            mirror_module.current_epoch_ms = lambda: log_timestamp_ms("2026-05-12 10:20:15")
            try:
                mirror(stdout_path, log_dir, output_path, interval=0, once=True)
            finally:
                mirror_module.current_epoch_ms = original_now
            status = json.loads(output_path.with_suffix(".status.json").read_text(encoding="utf-8"))
            with Path(status["output_pgn"]).open("r", encoding="utf-8") as handle:
                game = chess.pgn.read_game(handle)

        self.assertIsNotNone(game)
        assert game is not None
        self.assertEqual(game.headers["Round"], "5")
        self.assertEqual(game.headers["Result"], "*")
        self.assertIn("1. d4 d5 2. Bf4", str(game))
        self.assertEqual(status["locked_game"], 5)
        self.assertEqual([item["status"] for item in status["games"][:4]], ["Completed"] * 4)
        self.assertEqual(status["games"][4]["status"], "In progress")
        self.assertTrue(status["games"][4]["is_board_game"])

    def test_mirror_writes_board_game_to_game_timestamped_live_pgn(self):
        from tools.live_pgn_viewer import collect_stats, tournament_slug

        stdout_text = "\n".join(
            [
                "Started game 1 of 100 (Codex-chess vs Codex-chess-learner)",
                "Finished game 1 (Codex-chess vs Codex-chess-learner): 1/2-1/2 {normal}",
                "Started game 2 of 100 (Codex-chess-learner vs Codex-chess)",
            ]
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stdout_path = root / "codex-vs-codex-learner-live-20260514-125617-launch.out.log"
            log_dir = root / "logs"
            output_path = root / "live" / "codex-vs-codex-learner-live-20260514-125617-live.pgn"
            log_dir.mkdir()
            output_path.parent.mkdir()
            stdout_path.write_text(stdout_text, encoding="utf-8")
            os.utime(stdout_path, (log_timestamp_ms("2026-05-14 12:56:10") / 1000, log_timestamp_ms("2026-05-14 12:56:10") / 1000))
            (root / "codex-vs-codex-learner-live-20260514-125617.pgn").write_text("", encoding="utf-8")
            (log_dir / "codex-chess-game1.log").write_text(
                "\n".join(
                    [
                        "[2026-05-14 12:56:17] > position startpos",
                        "[2026-05-14 12:56:17] > go wtime 999999999 btime 999999999",
                        "[2026-05-14 12:56:21] > position startpos moves e2e4",
                        "[2026-05-14 12:56:21] > go wtime 999990000 btime 999999999",
                    ]
                ),
                encoding="utf-8",
            )
            (log_dir / "codex-chess-game2.log").write_text(
                "\n".join(
                    [
                        "[2026-05-14 15:47:11] > position startpos",
                        "[2026-05-14 15:47:11] > go wtime 999999999 btime 999999999",
                        "[2026-05-14 15:47:15] > position startpos moves d2d4",
                        "[2026-05-14 15:47:15] > go wtime 999990000 btime 999999999",
                    ]
                ),
                encoding="utf-8",
            )

            original_now = mirror_module.current_epoch_ms
            mirror_module.current_epoch_ms = lambda: log_timestamp_ms("2026-05-14 15:47:18")
            try:
                mirror(stdout_path, log_dir, output_path, interval=0, once=True)
            finally:
                mirror_module.current_epoch_ms = original_now
            status = json.loads(output_path.with_suffix(".status.json").read_text(encoding="utf-8"))
            board_path = Path(status["output_pgn"])
            with board_path.open("r", encoding="utf-8") as handle:
                game = chess.pgn.read_game(handle)
            stats = collect_stats(root, None, None, output_path)
            live_match = next(item for item in stats["matches"] if item["kind"] == "live")

        self.assertEqual(board_path.name, "codex-vs-codex-learner-live-20260514-154711-game-2-live.pgn")
        self.assertEqual(tournament_slug(board_path), "codex-vs-codex-learner-live-20260514-154711")
        self.assertEqual(live_match["tournament_slug"], "codex-vs-codex-learner-live-20260514-154711")
        self.assertEqual(live_match["game_index"], 2)
        self.assertEqual(Path(live_match["control_path"]), output_path)
        self.assertEqual(Path(status["control_pgn"]), output_path)
        self.assertIsNotNone(game)
        assert game is not None
        self.assertEqual(game.headers["Round"], "2")
        self.assertEqual(game.headers["TotalGames"], "100")
        self.assertEqual(game.headers["GameStartTime"], "2026-05-14 15:47:11")
        self.assertIn("1. d4", str(game))

    def test_viewer_stats_prefers_newest_in_progress_live_status(self):
        from tools.live_pgn_viewer import collect_stats

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            live_dir = out_dir / "live"
            live_dir.mkdir()
            old_pgn = live_dir / "codex-vs-codex-learner-live-20260512-165211-live.pgn"
            new_pgn = live_dir / "codex-vs-codex-learner-live-20260512-170000-live.pgn"
            now = time.time()
            old_pgn.write_text(
                "\n".join(
                    [
                        '[Event "Stale live"]',
                        '[White "Codex-chess"]',
                        '[Black "Codex-chess-learner"]',
                        '[Result "*"]',
                        '[WhiteClockMs "0"]',
                        '[BlackClockMs "180000"]',
                        '[ClockUpdatedAtEpochMs "100000"]',
                        '[ClockRunningSide "White"]',
                        "",
                        "*",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            new_pgn.write_text(
                "\n".join(
                    [
                        '[Event "Active live"]',
                        '[White "Codex-chess"]',
                        '[Black "Codex-chess-learner"]',
                        '[Result "*"]',
                        '[WhiteClockMs "300000"]',
                        '[BlackClockMs "300000"]',
                        f'[ClockUpdatedAtEpochMs "{int(now * 1000)}"]',
                        '[ClockRunningSide "White"]',
                        "",
                        "*",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            (old_pgn.with_suffix(".status.json")).write_text(
                json.dumps(
                    {
                        "generated_at": "2026-05-12 16:52:11",
                        "generated_at_epoch": now - 10,
                        "output_pgn": str(old_pgn),
                        "locked_game": 1,
                        "games": [
                            {
                                "game": 1,
                                "total": 1,
                                "white": "Codex-chess",
                                "black": "Codex-chess-learner",
                                "result": "*",
                                "finished": False,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (new_pgn.with_suffix(".status.json")).write_text(
                json.dumps(
                    {
                        "generated_at": "2026-05-12 17:00:00",
                        "generated_at_epoch": now,
                        "output_pgn": str(new_pgn),
                        "locked_game": 1,
                        "games": [
                            {
                                "game": 1,
                                "total": 10,
                                "white": "Codex-chess",
                                "black": "Codex-chess-learner",
                                "result": "*",
                                "finished": False,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            stats = collect_stats(out_dir, None, None, old_pgn)
            live_matches = [match for match in stats["matches"] if match["kind"] == "live"]

        self.assertEqual(live_matches[0]["tournament_slug"], "codex-vs-codex-learner-live-20260512-170000")
        self.assertEqual(live_matches[0]["game_index"], 1)

    def test_viewer_stats_ignores_stale_live_status_files(self):
        from tools.live_pgn_viewer import collect_stats

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            live_dir = out_dir / "live"
            live_dir.mkdir()
            stale_pgn = live_dir / "codex-vs-codex-learner-live-20260512-163508-live.pgn"
            now = time.time()
            stale_pgn.write_text(
                "\n".join(
                    [
                        '[Event "Stale live"]',
                        '[White "Codex-chess"]',
                        '[Black "Codex-chess-learner"]',
                        '[Result "*"]',
                        '[WhiteClockMs "300000"]',
                        '[BlackClockMs "300000"]',
                        f'[ClockUpdatedAtEpochMs "{int(now * 1000)}"]',
                        '[ClockRunningSide "White"]',
                        "",
                        "*",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            status_path = stale_pgn.with_suffix(".status.json")
            status_path.write_text(
                json.dumps(
                    {
                        "generated_at": "2026-05-12 16:35:08",
                        "generated_at_epoch": now - 600,
                        "output_pgn": str(stale_pgn),
                        "locked_game": 1,
                        "games": [
                            {
                                "game": 1,
                                "total": 10,
                                "white": "Codex-chess",
                                "black": "Codex-chess-learner",
                                "result": "*",
                                "finished": False,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            stale = now - 600
            os.utime(status_path, (stale, stale))

            stats = collect_stats(out_dir, None, None, stale_pgn)
            live_matches = [match for match in stats["matches"] if match["kind"] == "live"]

        self.assertEqual(live_matches, [])

    def test_viewer_stats_keeps_completed_archive_rows_for_explicit_game_hashes(self):
        from tools.live_pgn_viewer import collect_stats

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            archive = out_dir / "codex-vs-codex-learner-live-20260512-165211.pgn"
            archive.write_text(
                "\n".join(
                    [
                        '[Event "Archived game"]',
                        '[Site "C:/dev/chess-harness-codex"]',
                        '[Date "2026.05.12"]',
                        '[Round "1"]',
                        '[White "Codex-chess"]',
                        '[Black "Codex-chess-learner"]',
                        '[Result "1-0"]',
                        "",
                        "1. e4 e5 1-0",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            stats = collect_stats(out_dir, None, None, None)
            match = next(
                item
                for item in stats["matches"]
                if item["kind"] == "completed" and item["tournament_slug"] == "codex-vs-codex-learner-live-20260512-165211"
            )

        self.assertEqual(match["game_index"], 1)
        self.assertEqual(match["winner_label"], "Codex-chess (White) won")


if __name__ == "__main__":
    unittest.main()
