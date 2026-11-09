"""Offline checks for the Lichess puzzle gauntlet, the fail-fast suite and the tool-building climb.

No model, no network, no engine: a scripted fake UCI engine and a fake author stand in, so these run
in CI. The live 100-puzzle suite is tests/test_lichess_puzzles_live.py (RUN_LIVE_TESTS=1).
"""
import io
import json
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

import chess

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "engines" / "llm-chess-engine"))
sys.path.insert(0, str(ROOT / "tests"))

import lichess_puzzles as lp  # noqa: E402
import play_llm_series as pls  # noqa: E402
import puzzle_gauntlet as pg  # noqa: E402
import puzzle_tool_climb as climb  # noqa: E402

# Small hand-made puzzles in the Lichess shape: Moves[0] is the opponent's setup move, then the solution.
BACK_RANK = {"id": "TEST1", "number": 1, "rating": 3000, "themes": ["mateIn1"], "url": "u",
             "fen": "6k1/5ppp/8/8/8/8/5PPP/R5K1 b - - 0 1", "moves": ["g8f8", "a1a8"]}
TWO_STEP = {"id": "TEST2", "number": 2, "rating": 3010, "themes": ["fork"], "url": "u",
            "fen": "r3k3/2p5/8/3N4/8/8/8/4K3 b - - 0 1", "moves": ["a8b8", "d5c7", "e8d7", "c7a6"]}

GENERAL_TOOL = textwrap.dedent('''
    import json, sys, chess
    payload = json.load(sys.stdin)
    board = chess.Board(payload["fen"])
    checks = [board.san(m) for m in board.legal_moves if board.gives_check(m)]
    print("checks: " + (", ".join(checks) or "none"))
''').strip()


class FakeEngine:
    """Answers `go` from a script: script(board, ply) -> uci (or '0000')."""

    def __init__(self, script):
        self.script = script
        self.board = chess.Board()
        self.pending: list[str] = []
        self.sent: list[str] = []
        self.closed = False
        self.games = 0

    def send(self, line: str) -> None:
        self.sent.append(line)
        if line.startswith("position fen "):
            body = line[len("position fen "):]
            fen, _, moves = body.partition(" moves ")
            self.board = chess.Board(fen)
            for uci in moves.split():
                self.board.push_uci(uci)
        elif line.startswith("go"):
            self.pending = [f"info string note thinking", f"bestmove {self.script(self.board.copy(), self.board.ply())}"]

    def wait_for(self, prefix, timeout):
        lines, self.pending = self.pending, []
        return lines

    def new_game(self):
        self.games += 1

    def move_budget_seconds(self, go_line=None):
        return 1

    def close(self):
        self.closed = True


def solution_script(puzzles):
    """Plays every puzzle's solution: the fake 'model' knows all the answers."""
    table = {}
    for p in puzzles:
        for board, move in lp.solver_positions(p):
            table[board.fen()] = move
    return lambda board, ply: table.get(board.fen(), "0000")


class FakeSpend:
    def __init__(self, steps=None):
        self.value = 0.0
        self.steps = steps or 0.0

    def used(self):
        self.value += self.steps
        return self.value


class FixtureTests(unittest.TestCase):
    @unittest.skipUnless(lp.FIXTURE.is_file(), "fixture not built yet")
    def test_fixture_is_100_hard_unique_legal_puzzles(self):
        data = json.loads(lp.FIXTURE.read_text(encoding="utf-8"))
        puzzles = data["puzzles"]
        self.assertEqual(len(puzzles), 100)
        self.assertEqual(len({p["id"] for p in puzzles}), 100)
        self.assertEqual([p["number"] for p in puzzles], list(range(1, 101)))
        self.assertEqual([p["rating"] for p in puzzles], sorted(p["rating"] for p in puzzles))
        self.assertIn("unique_check", data["selection"])
        for p in puzzles:
            self.assertGreaterEqual(p["rating"], lp.MIN_RATING)
            self.assertLessEqual(p["rating_deviation"], lp.MAX_DEVIATION)
            board = chess.Board(p["fen"])
            for uci in p["moves"]:
                move = chess.Move.from_uci(uci)
                self.assertIn(move, board.legal_moves, f"{p['id']}: {uci}")
                board.push(move)
            self.assertEqual(len(p["moves"]) % 2, 0, p["id"])

    def test_candidate_filter(self):
        row = {"PuzzleId": "x", "FEN": BACK_RANK["fen"], "Moves": "g8f8 a1a8", "Rating": "3000",
               "RatingDeviation": "75", "Popularity": "90", "NbPlays": "1000", "Themes": "mateIn1 short"}
        self.assertEqual(lp.candidate(row)["moves"], ["g8f8", "a1a8"])
        self.assertIsNone(lp.candidate({**row, "Rating": "2000"}))
        self.assertIsNone(lp.candidate({**row, "RatingDeviation": "200"}))
        self.assertIsNone(lp.candidate({**row, "Moves": "g8f8 a1a8 h7h6"}))

    def test_solver_positions_skip_the_setup_move(self):
        steps = list(lp.solver_positions(TWO_STEP))
        self.assertEqual([m for _, m in steps], ["d5c7", "c7a6"])
        self.assertEqual(steps[0][0].turn, chess.WHITE)


class GradeTests(unittest.TestCase):
    def test_exact_move_mate_alternative_wrong_and_forfeit(self):
        board = chess.Board("6k1/5ppp/8/8/8/8/5PPP/R4RK1 w - - 0 1")
        self.assertEqual(pg.grade(board, "a1a8", "a1a8"), (True, ""))
        self.assertTrue(pg.grade(board, "f1f8", "a1a8")[0] is False)   # Rf8 is illegal (the f2 pawn blocks)
        board2 = chess.Board("6k1/5ppp/8/8/8/8/5PPP/RR4K1 w - - 0 1")
        self.assertEqual(pg.grade(board2, "b1b8", "a1a8"), (True, ""))  # another mate is accepted, as on Lichess
        self.assertFalse(pg.grade(board, "0000", "a1a8")[0])
        self.assertIn("illegal", pg.grade(board, "a1h8", "a1a8")[1])


class SolveAndRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_solves_a_puzzle_through_uci_with_fen_positions_and_context(self):
        engine = FakeEngine(solution_script([TWO_STEP]))
        result = pg.solve(engine, TWO_STEP, self.out, log=lambda m: None)
        self.assertTrue(result.passed, result.reason)
        self.assertEqual([m["played"] for m in result.moves], ["d5c7", "c7a6"])
        positions = [s for s in engine.sent if s.startswith("position fen ")]
        self.assertEqual(positions[0], f"position fen {TWO_STEP['fen']} moves a8b8")
        self.assertEqual(positions[1], f"position fen {TWO_STEP['fen']} moves a8b8 d5c7 e8d7")
        ctx = next(s for s in engine.sent if "GameContextFile" in s).split(" value ", 1)[1]
        header = json.loads(Path(ctx).read_text(encoding="utf-8"))["header"]
        self.assertIn("exactly ONE move", header)
        self.assertIn("White", header)

    def test_first_wrong_move_fails_the_puzzle_and_no_later_move_is_asked(self):
        engine = FakeEngine(lambda board, ply: "c7e8" if ply > 2 else "d5c7")
        result = pg.solve(engine, TWO_STEP, self.out, log=lambda m: None)
        self.assertFalse(result.passed)
        self.assertEqual((result.expected, result.played), ("c7a6", "c7e8"))
        self.assertEqual(result.failed_ply, 4)

    def test_forfeit_fails(self):
        result = pg.solve(FakeEngine(lambda b, p: "0000"), BACK_RANK, self.out, log=lambda m: None)
        self.assertFalse(result.passed)
        self.assertIn("forfeit", result.reason)

    def test_run_stops_at_the_first_failed_puzzle(self):
        third = {**BACK_RANK, "id": "TEST3", "number": 3}
        engine = FakeEngine(lambda board, ply: "a1a8" if board.fen().startswith("5k2") else "0000")
        report = pg.run([BACK_RANK, TWO_STEP, third], engine, self.out, log=lambda m: None)
        self.assertEqual((report["passed"], report["played"]), (1, 2))
        self.assertIn("first failure: puzzle 2", report["stopped"])
        saved = json.loads((self.out / "results.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["first_failure"]["id"], "TEST2")

    def test_no_fail_fast_plays_everything(self):
        engine = FakeEngine(lambda board, ply: "0000")
        report = pg.run([BACK_RANK, TWO_STEP], engine, self.out, fail_fast=False, log=lambda m: None)
        self.assertEqual((report["passed"], report["played"]), (0, 2))

    def test_budget_stops_before_a_puzzle(self):
        engine = FakeEngine(solution_script([BACK_RANK]))
        report = pg.run([BACK_RANK, BACK_RANK], engine, self.out, budget_usd=0.5, spend=FakeSpend(0.3),
                        log=lambda m: None)
        self.assertEqual(report["played"], 1)
        self.assertIn("budget", report["stopped"])


class EngineWiringTests(unittest.TestCase):
    def test_player_config_is_glm_on_openrouter_without_shared_tools(self):
        player = pg.player_config(tools_dir="C:/x")
        env = pls.engine_env(player, pg.engine_cfg())
        self.assertEqual((env["LLM_PROVIDER"], env["LLM_MODEL"]), ("openrouter-chat", "z-ai/glm-5.3-flash"))
        self.assertEqual(env["LLM_SHARED_TOOLS"], "false")
        self.assertEqual(env["LLM_TOOLS_DIR"], "C:/x")
        self.assertEqual(json.loads(env["LLM_MAX_PRICE"]), pg.MAX_PRICE)
        self.assertNotIn("LLM_SHARED_TOOLS", pls.engine_env({**player, "sharedTools": True}, pg.engine_cfg()))

    def test_engine_runs_no_shared_tool_when_switched_off(self):
        import subscription_providers as sp

        with mock.patch.dict(os.environ, {"LLM_SHARED_TOOLS": "false", "LLM_TOOLS_DIR": ""}):
            client = sp.SubscriptionChessClient("openrouter-chat", lambda m: None)
        self.assertFalse(client.shared_tools)
        with mock.patch.object(sp.player_tools_mod, "run_tools") as run_tools:
            self.assertEqual(client._tools_text(chess.Board(), {}, []), "")
            run_tools.assert_not_called()
        with mock.patch.dict(os.environ, {"LLM_SHARED_TOOLS": "", "LLM_TOOLS_DIR": ""}):
            self.assertTrue(sp.SubscriptionChessClient("openrouter-chat", lambda m: None).shared_tools)


class FenStartTests(unittest.TestCase):
    def test_move_record_starts_from_the_puzzle_fen_not_the_start_position(self):
        """Live run 2026-11-09: move 2 of a puzzle forfeited because the record was replayed from the start."""
        import subscription_providers as sp

        board = chess.Board(TWO_STEP["fen"])
        history = ["a8b8", "d5c7", "e8d7"]
        for uci in history:
            board.push_uci(uci)
        self.assertEqual(sp.root_of(board, history).fen(), TWO_STEP["fen"])
        self.assertEqual(sp.san_history(history, sp.root_of(board, history)), "1... Rb8 2. Nxc7+ Kd7")
        prompt = sp.build_prompt(board, {"wtime": 60000, "btime": 60000}, history, [])
        self.assertIn("Moves so far: 1... Rb8 2. Nxc7+ Kd7", prompt)
        full = sp.build_full_turn({"memory": "", "header": "puzzle"}, board, {"wtime": 60000, "btime": 60000}, history)
        self.assertIn("1... Rb8 2. Nxc7+ Kd7", full)
        delta = sp.build_delta_turn(board, {"wtime": 60000, "btime": 60000}, history, 1)
        self.assertIn("2. Nxc7+ Kd7", delta)
        # A record that does not fit its start position is shown as UCI instead of crashing the move.
        self.assertEqual(sp.san_moves_since(["a8b8"], 0), "a8b8")
        self.assertEqual(sp.san_history(["e2e4", "e7e5"]), "1. e4 e5")

    def test_puzzle_engine_config_has_no_short_move_cap(self):
        cfg = pg.engine_cfg()
        self.assertFalse(cfg["moveCap"])
        self.assertGreaterEqual(cfg["attemptTimeoutSeconds"], pg.MOVE_CLOCK_MS // 1000)


class FailFastSuiteTests(unittest.TestCase):
    def test_suite_stops_at_the_first_failed_test(self):
        import test_lichess_puzzles_live as live

        ran = []

        class Demo(live.FailFastCase):
            def test_a(self):
                ran.append("a")

            def test_b(self):
                ran.append("b")
                self.fail_fast("missed")

            def test_c(self):
                ran.append("c")

        live.FailFastCase.stopped_by = None
        try:
            suite = unittest.defaultTestLoader.loadTestsFromTestCase(Demo)
            result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
        finally:
            live.FailFastCase.stopped_by = None
        self.assertEqual(ran, ["a", "b"])
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(result.testsRun, 2)

    @unittest.skipUnless(lp.FIXTURE.is_file(), "fixture not built yet")
    def test_live_suite_has_one_test_per_puzzle_in_order(self):
        import test_lichess_puzzles_live as live

        names = unittest.defaultTestLoader.getTestCaseNames(live.HardestLichessPuzzles)
        self.assertEqual(len(names), 100)
        self.assertEqual(names, sorted(names))
        self.assertTrue(names[0].startswith("test_p001_"))


class AnswerKeyTests(unittest.TestCase):
    def test_general_tool_passes_and_answer_keys_are_rejected(self):
        self.assertIsNone(climb.answer_key_problem(GENERAL_TOOL, [TWO_STEP]))
        self.assertIn("FEN", climb.answer_key_problem(GENERAL_TOOL + '\nX = "r3k3/2p5/8/3N4/8/8/8/4K3"', [TWO_STEP]))
        self.assertIn("TEST2", climb.answer_key_problem(GENERAL_TOOL + "\n# TEST2", [TWO_STEP]))
        self.assertIn("sequence", climb.answer_key_problem(GENERAL_TOOL + '\nL = ["d5c7", "e8d7"]', [TWO_STEP]))

    def test_apply_edits_validates_like_the_tournament(self):
        with tempfile.TemporaryDirectory() as tmp:
            tools = Path(tmp)
            applied, rejected = climb.apply_edits({"edits": [
                {"path": "tools/checks.py", "content": GENERAL_TOOL},
                {"path": "tools/engine.py", "content": "import chess.engine\nprint(1)"},
                {"path": "tools/key.py", "content": GENERAL_TOOL + '\nF = "r3k3/2p5/8/3N4/8/8/8/4K3"'},
                {"path": "../evil.py", "content": GENERAL_TOOL},
            ]}, tools, [TWO_STEP])
            self.assertEqual([p.name for p in tools.glob("*.py")], ["checks.py"])
            self.assertEqual(len(applied), 1)
            self.assertEqual(len(rejected), 3)
            applied, rejected = climb.apply_edits({"edits": [{"path": "tools/checks.py", "delete": True}]},
                                                  tools, [TWO_STEP])
            self.assertEqual(list(tools.glob("*.py")), [])


class ClimbTests(unittest.TestCase):
    def test_baseline_then_tools_then_full_pass(self):
        """Round 0 has no tools and misses; the author writes a tool; round 1 restarts at puzzle 1 and passes."""
        puzzles = [BACK_RANK, TWO_STEP]
        solved = solution_script(puzzles)

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            tools_dir = out / "tools"
            # The fake model only knows the second puzzle once it has a tool.
            engines = []

            def factory():
                has_tool = any(tools_dir.glob("*.py"))
                engine = FakeEngine(lambda board, ply: solved(board, ply)
                                    if has_tool or board.fen().startswith("5k2") else "0000")
                engines.append(engine)
                return engine

            class Author:
                prompts = []

                def ask_text(self, system, prompt, timeout):
                    Author.prompts.append(prompt)
                    return json.dumps({"summary": "check every check", "edits": [
                        {"path": "tools/checks.py", "content": GENERAL_TOOL}]})

            state = climb.climb(puzzles, out, model="m", provider="fake", effort="high", budget_usd=1.0,
                                max_rounds=3, engine_factory=factory, author_factory=lambda *a: Author(),
                                spend=FakeSpend(), log=lambda m: None)
            self.assertTrue(state["solved_all"])
            self.assertEqual([(r["round"], r["passed"]) for r in state["rounds"]], [(0, 1), (1, 2)])
            self.assertEqual(state["rounds"][0]["tools"], [])          # baseline: nothing at all
            self.assertEqual(state["rounds"][1]["tools"], ["checks.py"])
            self.assertTrue(all(e.closed for e in engines))
            prompt = Author.prompts[0]
            self.assertIn("The only winning move was Nxc7+", prompt)
            self.assertIn("(you had no tools)", prompt)
            self.assertIn("may NOT contain positions", prompt)
            self.assertTrue((out / "round-00" / "results.json").is_file())
            self.assertTrue((out / "climb.json").is_file())

    def test_rejected_tool_is_sent_back_once_more(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, tools = Path(tmp), Path(tmp) / "tools"
            tools.mkdir()
            replies = iter([json.dumps({"edits": [{"path": "tools/a.py", "content": "import chess.engine"}]}),
                            json.dumps({"edits": [{"path": "tools/a.py", "content": GENERAL_TOOL}]})])

            class Author:
                prompts = []

                def ask_text(self, system, prompt, timeout):
                    Author.prompts.append(prompt)
                    return next(replies)

            failed = {"expected": "d5c7", "played": "d5f6", "failed_ply": 2}
            record = climb.retro(Author(), out, tools, "story", [], [TWO_STEP], TWO_STEP, failed, 0, 0,
                                 log=lambda m: None, iterations=2)
            self.assertEqual(len(Author.prompts), 2)
            self.assertIn("NOT FULLY ACCEPTED", Author.prompts[1])
            self.assertTrue((tools / "a.py").is_file())
            self.assertEqual(len(record["rejected"]), 1)


class ToolVerdictTests(unittest.TestCase):
    """2026-11-09: a tool that only mentioned the winning move (on a WEAK line) was logged as FOUND."""

    def verdict(self, sources: dict[str, str], expected: str = "e2e4"):
        with tempfile.TemporaryDirectory() as tmp:
            for name, line in sources.items():
                (Path(tmp) / f"{name}.py").write_text(f"import chess\nprint({line!r})\n", encoding="utf-8")
            return climb.tool_verdict(Path(tmp), chess.Board(), [], expected)

    def test_a_mention_is_not_a_find(self):
        hit, text = self.verdict({"a": "WEAK: e2e4 quiet, prefer search\npv: d2d4 e2e4"})
        self.assertFalse(hit)
        self.assertIn("only MENTIONS e2e4", text)

    def test_only_the_first_move_of_a_play_line_counts(self):
        self.assertFalse(self.verdict({"a": "PLAY: d2d4 then e2e4"})[0])
        self.assertTrue(self.verdict({"a": "PLAY: e2e4 eval 30"})[0])

    def test_tools_that_disagree_do_not_count(self):
        hit, text = self.verdict({"a": "PLAY: e2e4", "b": "PLAY: d2d4"})
        self.assertFalse(hit)
        self.assertIn("disagree", text)
        self.assertTrue(self.verdict({"a": "PLAY: e2e4", "b": "no forced mate"})[0])


CAPTURE_TOOL = textwrap.dedent('''
    import json, sys, chess
    board = chess.Board(json.load(sys.stdin)["fen"])
    captures = [m.uci() for m in board.legal_moves if board.is_capture(m)]
    print("PLAY: " + captures[0] if captures else "no capture")
''').strip()


class RetroSelfTestTests(unittest.TestCase):
    """2026-11-09: after a rejected edit the next prompt hid that the rest of the puzzle line still failed."""

    def test_every_prompt_shows_the_whole_line_and_broken_earlier_puzzles(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, tools = Path(tmp), Path(tmp) / "tools"
            tools.mkdir()
            # Try 1: a tool that finds the first move of TWO_STEP only, and no move of BACK_RANK.
            # Try 2: a banned tool (rejected). Try 3 must still be told about both gaps.
            replies = iter([json.dumps({"edits": [{"path": "tools/a.py", "content": CAPTURE_TOOL}]}),
                            json.dumps({"edits": [{"path": "tools/b.py", "content": "import chess.engine"}]}),
                            json.dumps({"edits": []})])

            class Author:
                prompts = []

                def ask_text(self, system, prompt, timeout):
                    Author.prompts.append(prompt)
                    return next(replies)

            failed = {"expected": "d5c7", "played": "d5f6", "failed_ply": 2}
            record = climb.retro(Author(), out, tools, "story", [], [BACK_RANK, TWO_STEP], TWO_STEP, failed, 1, 1,
                                 log=lambda m: None, iterations=3)
            self.assertEqual(len(Author.prompts), 3)
            for prompt in Author.prompts[1:]:
                self.assertIn("recommend 1 of the 2 winning moves", prompt)
                self.assertIn("BROKE a puzzle you had already solved", prompt)
            self.assertEqual(record["selftest_found"], 1)
            self.assertEqual(record["selftest_regressions"], 1)


class ImportedToolsTests(unittest.TestCase):
    """Owner 2026-11-09: start from every tournament player's tools, not from scratch."""

    def test_import_copies_every_players_tools_with_a_player_prefix_and_checks_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            agents = Path(tmp) / "agents"
            for player, name, content in (("claude", "loose-sweep", GENERAL_TOOL), ("glm", "loose-sweep", GENERAL_TOOL),
                                          ("muse", "bad", "import chess.engine\nprint(1)")):
                (agents / player / "tools").mkdir(parents=True, exist_ok=True)
                (agents / player / "tools" / f"{name}.py").write_text(content, encoding="utf-8")
            dest = Path(tmp) / "imported"
            report = climb.import_tools(agents, dest, [TWO_STEP], log=lambda m: None)
            self.assertEqual(sorted(p.name for p in dest.glob("*.py")), ["claude-loose-sweep.py", "glm-loose-sweep.py"])
            self.assertEqual([r["ok"] for r in report], [True, True, False])

    def test_imported_tools_run_like_shared_ones_and_do_not_use_own_slots(self):
        import player_tools as pt

        with tempfile.TemporaryDirectory() as tmp:
            imported, own = Path(tmp) / "imported", Path(tmp) / "own"
            imported.mkdir()
            own.mkdir()
            for index in range(12):
                (imported / f"borrowed-{index:02d}.py").write_text(GENERAL_TOOL, encoding="utf-8")
            for index in range(pt.MAX_TOOL_FILES):
                (own / f"mine-{index}.py").write_text(GENERAL_TOOL, encoding="utf-8")
            found, skipped = pt.discover_with_skipped(own, include_shared=False, extra_dirs=[imported])
            self.assertEqual(len(found), 12 + pt.MAX_TOOL_FILES)
            self.assertEqual(skipped, [])
            block = pt.run_tools(None, chess.Board(), [], include_shared=False, extra_dirs=[imported])
            self.assertEqual(len(block.results), 12)
            self.assertTrue(all(r.ok and r.output.startswith("checks:") for r in block.results))

    def test_engine_gets_the_imported_folder(self):
        import subscription_providers as sp

        player = pg.player_config(tools_dir="C:/own", shared_tools=True, extra_dirs=["C:/imp"])
        env = pls.engine_env(player, pg.engine_cfg())
        self.assertEqual(env["LLM_EXTRA_TOOLS_DIRS"], "C:/imp")
        self.assertNotIn("LLM_SHARED_TOOLS", env)
        with mock.patch.dict(os.environ, {"LLM_EXTRA_TOOLS_DIRS": "C:/imp", "LLM_TOOLS_DIR": ""}):
            client = sp.SubscriptionChessClient("openrouter-chat", lambda m: None)
        self.assertEqual(client.extra_tool_dirs, [Path("C:/imp")])
        with mock.patch.object(sp.player_tools_mod, "run_tools", return_value=sp.player_tools_mod.ToolsBlock()) as run:
            client._tools_text(chess.Board(), {}, [])
        self.assertEqual(run.call_args.kwargs["extra_dirs"], [Path("C:/imp")])

    def test_author_prompt_lists_borrowed_tools(self):
        text = climb.author_prompt("story", [], {}, 0, 0, 100, borrowed=["claude-loose-sweep: flags loose pieces"])
        self.assertIn("BORROWED TOOLS", text)
        self.assertIn("claude-loose-sweep: flags loose pieces", text)
        self.assertNotIn("You start from nothing", text)
        self.assertIn("You start from nothing", climb.author_prompt("story", [], {}, 0, 0, 100))


if __name__ == "__main__":
    unittest.main()
