"""Player-authored verification tools: policy, runner, prompt wiring (PRD 195, Validation 91).

The point of these tests is the contract, not a specific tool: an agent writes
python-chess checks in its own tools/ folder, the harness runs them on the real
board before the move, and the output reaches the prompt. No engine, no network.
"""
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

import ai_chess_memory as mem  # noqa: E402
import player_tools as pt  # noqa: E402
import subscription_providers as sp  # noqa: E402

BLUNDER_GAME = ["e2e4", "e7e5", "g1f3", "b8c6", "f1b5", "g8f6", "e1g1", "f6e4",
                "d2d4", "e4d6", "b5c6", "d7c6", "d4e5", "d6f5", "d1d8", "e8d8",
                "b1c3", "f8e7", "f1d1", "d8e8", "c1g5", "e7g5", "f3g5", "h7h6"]

HANG_TOOL = textwrap.dedent('''
    import json, sys
    import chess

    payload = json.load(sys.stdin)
    board = chess.Board(payload["fen"])
    out = []
    for move in board.legal_moves:
        victim = board.piece_at(move.to_square)
        if victim is None:
            continue
        board.push(move)
        attackers = board.attackers(board.turn, move.to_square)
        defenders = board.attackers(not board.turn, move.to_square)
        board.pop()
        if attackers and not defenders:
            out.append(f"{move.uci()} hangs the {victim.symbol()}")
    print("\\n".join(out) if out else "no free captures")
''').strip() + "\n"


def play(ucis):
    board = chess.Board()
    for uci in ucis:
        board.push_uci(uci)
    return board


class ToolPolicyTests(unittest.TestCase):
    def test_accepts_a_valid_tool(self):
        ok, why = pt.validate_tool_edit("tools/hang-check.py", HANG_TOOL)
        self.assertTrue(ok, why)

    def test_rejects_wrong_names_and_paths(self):
        for path in ("tools/Bad.py", "notes/x.md", "MEMORY.md", "../tools/x.py", "tools/x.txt"):
            ok, _ = pt.validate_tool_edit(path, "print(1)\n")
            self.assertFalse(ok, path)

    def test_rejects_engine_network_and_fs(self):
        for source in ("import chess.engine\n", "import urllib.request\n", "import requests\n",
                       "import subprocess\n", "open('x')\n", "exec('1')\n"):
            ok, why = pt.validate_tool_edit("tools/x.py", source)
            self.assertFalse(ok, source)
            self.assertTrue(why)

    def test_rejects_oversize_and_broken_code(self):
        ok, _ = pt.validate_tool_edit("tools/big.py", "#" * (pt.TOOL_MAX_BYTES + 1))
        self.assertFalse(ok)
        ok, why = pt.validate_tool_edit("tools/broken.py", "def f(:\n")
        self.assertFalse(ok)
        self.assertIn("does not compile", why)


class ToolRunnerTests(unittest.TestCase):
    def test_tool_runs_on_the_real_board_and_finds_the_hang(self):
        with tempfile.TemporaryDirectory() as tmp:
            tools = Path(tmp) / "tools"
            tools.mkdir()
            (tools / "hang-check.py").write_text(HANG_TOOL, encoding="utf-8")
            block = pt.run_tools(tools, play(BLUNDER_GAME), BLUNDER_GAME, include_shared=False)
            self.assertEqual(len(block.results), 1)
            result = block.results[0]
            self.assertTrue(result.ok, result.error)
            self.assertIn("g5f7", result.output)

    def test_broken_tool_never_raises_and_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            tools = Path(tmp) / "tools"
            tools.mkdir()
            (tools / "boom.py").write_text("raise SystemExit(3)\n", encoding="utf-8")
            block = pt.run_tools(tools, chess.Board(), [], include_shared=False)
            self.assertFalse(block.results[0].ok)
            self.assertIn("exit", block.results[0].error)

    def test_relative_tools_dir_still_runs(self):
        # Regression: cwd becomes the tool's folder, so a relative path must be
        # resolved first or the child cannot find its own script.
        with tempfile.TemporaryDirectory() as tmp:
            tools = Path(tmp) / "tools"
            tools.mkdir()
            (tools / "ok.py").write_text("import json, sys\nprint('ran')\n", encoding="utf-8")
            cwd = Path.cwd()
            try:
                os.chdir(tmp)
                result = pt.run_tools(Path("tools"), chess.Board(), [], include_shared=False).results[0]
            finally:
                os.chdir(cwd)
            self.assertTrue(result.ok, result.error)
            self.assertEqual(result.output, "ran")

    def test_empty_folder_is_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(pt.run_tools(Path(tmp) / "none", chess.Board(), [], include_shared=False).any_ran)

    def test_source_is_rechecked_at_run_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            tools = Path(tmp) / "tools"
            tools.mkdir()
            (tools / "sneaky.py").write_text("import socket\nprint('x')\n", encoding="utf-8")
            result = pt.run_tools(tools, chess.Board(), [], include_shared=False).results[0]
            self.assertFalse(result.ok)
            self.assertIn("blocked", result.error)


class PromptWiringTests(unittest.TestCase):
    def test_tool_output_reaches_the_prompt_last(self):
        with tempfile.TemporaryDirectory() as tmp:
            tools = Path(tmp) / "tools"
            tools.mkdir()
            (tools / "hang-check.py").write_text(HANG_TOOL, encoding="utf-8")
            board = play(BLUNDER_GAME)
            block = pt.run_tools(tools, board, BLUNDER_GAME, include_shared=False)
            text = pt.render_block(block, board, BLUNDER_GAME)
            prompt = sp.position_block(board, {"wtime": 600000, "btime": 600000}, True, tools_text=text)
        self.assertIn("verification tools have already run", text.lower())
        self.assertIn("g5f7", text)
        self.assertIn("g5f7", prompt)
        self.assertTrue(prompt.rstrip().endswith("Reply with ONLY the JSON object."))

    def test_client_runs_tools_once_per_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            tools = Path(tmp) / "tools"
            tools.mkdir()
            (tools / "hang-check.py").write_text(HANG_TOOL, encoding="utf-8")
            client = sp.SubscriptionChessClient("opencode-go", lambda _m: None)
            client.tools_dir = tools
            board = play(BLUNDER_GAME)
            first = client._tools_text(board, {}, BLUNDER_GAME)
            second = client._tools_text(board, {}, BLUNDER_GAME)
            client.new_game()
            third = client._tools_text(board, {}, BLUNDER_GAME)
        self.assertIn("g5f7", first)
        self.assertEqual(first, second)          # one run per position
        self.assertEqual(first, third)           # re-runs after a new game
        self.assertTrue(client.last_report["tools"][0]["ok"])


class MemoryAllowlistTests(unittest.TestCase):
    def test_reflection_may_write_tools(self):
        edits = [
            {"path": "tools/hang-check.py", "content": HANG_TOOL},
            {"path": "tools/bad.py", "content": "import chess.engine\n"},
            {"path": "MEMORY.md", "content": "# m\n- lesson\n"},
        ]
        accepted, rejected = mem.validate_edits(edits, [])
        self.assertEqual([a["path"] for a in accepted], ["tools/hang-check.py", "MEMORY.md"])
        self.assertTrue(any("no chess engine" in r for r in rejected))

    def test_tool_file_cap(self):
        existing = {f"tools/old{i}.py": "print(1)\n" for i in range(pt.MAX_TOOL_FILES)}
        accepted, rejected = mem.validate_edits([{"path": "tools/new.py", "content": "print(1)\n"}], [],
                                                current=existing)
        self.assertEqual(accepted, [])
        self.assertTrue(any("over the limit" in r for r in rejected), rejected)

    def test_reflection_counts_the_tools_already_on_disk(self):
        # 2026-10-30: current_texts listed only MEMORY.md and notes, so the limit never saw the
        # tools on disk and Sonnet grew to 7 own tools while only 6 could run.
        with tempfile.TemporaryDirectory() as tmp:
            repo = mem.MemoryRepo(Path(tmp), log=lambda *_: None)
            tools = repo.agent_dir("Sonnet") / "tools"
            tools.mkdir(parents=True)
            for i in range(pt.MAX_TOOL_FILES):
                (tools / f"own{i}.py").write_text("print(1)\n", encoding="utf-8")
            current = repo.current_texts("Sonnet")
            self.assertEqual(sum(1 for p in current if p.startswith("tools/")), pt.MAX_TOOL_FILES)
            accepted, rejected = mem.validate_edits(
                [{"path": "tools/ninth.py", "content": "print(1)\n"}], [], current=current)
            self.assertEqual(accepted, [])
            self.assertTrue(any("ninth.py" in r and "over the limit" in r and "shared tools do not count" in r
                                for r in rejected), rejected)
            # Rewriting an existing tool is fine, and a delete in the same reflection frees a place.
            accepted, rejected = mem.validate_edits(
                [{"path": "tools/own0.py", "content": "print(2)\n"},
                 {"path": "tools/own1.py", "delete": True},
                 {"path": "tools/ninth.py", "content": "print(3)\n"}], [], current=current)
            self.assertEqual(rejected, [])
            self.assertEqual([a["path"] for a in accepted], ["tools/own0.py", "tools/own1.py", "tools/ninth.py"])


def _write_tools(folder: Path, count: int, prefix: str = "own") -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        (folder / f"{prefix}{i}.py").write_text(f"print('{prefix}{i} ran')\n", encoding="utf-8")


class ToolCapTests(unittest.TestCase):
    def test_limit_is_eight_own_tools(self):
        self.assertEqual(pt.MAX_TOOL_FILES, 8)

    def test_shared_tool_does_not_take_an_own_place(self):
        shared = pt.discover(None)
        self.assertIn("loose-piece-check", shared)
        with tempfile.TemporaryDirectory() as tmp:
            own = Path(tmp) / "tools"
            _write_tools(own, 7)
            block = pt.run_tools(own, chess.Board(), [])
        names = [r.name for r in block.results]
        self.assertEqual(len(names), 8, names)          # 1 shared + all 7 own
        self.assertIn("loose-piece-check", names)
        self.assertEqual(block.skipped, [])
        self.assertTrue(all(r.ok for r in block.results), [r.error for r in block.results])

    def test_extra_own_tools_are_named_not_dropped_silently(self):
        with tempfile.TemporaryDirectory() as tmp:
            own = Path(tmp) / "tools"
            _write_tools(own, 9)
            board = chess.Board()
            block = pt.run_tools(own, board, [], include_shared=False)
            self.assertEqual(len(block.results), 8)
            self.assertEqual(block.skipped, ["own8"])
            text = pt.render_block(block, board, [])
            with_shared = pt.run_tools(own, board, [])
        self.assertIn("Not run: own8", text)
        self.assertIn("shared tools do not count", text)
        self.assertEqual(len(with_shared.results), 9)   # the shared tool still runs on top of 8 own
        self.assertEqual(with_shared.skipped, ["own8"])


class OutputCutTests(unittest.TestCase):
    def test_short_output_is_untouched(self):
        self.assertEqual(pt.cut_output("short"), "short")

    def test_cut_output_ends_with_a_marker(self):
        text = pt.cut_output("x" * (pt.MAX_OUTPUT_CHARS + 50))
        self.assertTrue(text.endswith(f"[output cut at {pt.MAX_OUTPUT_CHARS} characters - put your answer first]"))
        self.assertTrue(text.startswith("x" * pt.MAX_OUTPUT_CHARS))

    def test_long_tool_output_is_cut_but_the_gate_still_reads_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            tools = Path(tmp) / "tools"
            tools.mkdir()
            (tools / "chatty.py").write_text(
                "print('filler line ' * 200)\nprint('AVOID: e2e4 - leaves the bishop loose')\n", encoding="utf-8")
            board = chess.Board()
            result = pt.run_tools(tools, board, [], include_shared=False).results[0]
        self.assertTrue(result.ok, result.error)
        self.assertIn("[output cut at", result.output)
        self.assertNotIn("AVOID", result.output)
        self.assertEqual(pt.flagged_moves([result], board), {"e2e4": "leaves the bishop loose"})


class RestrictedEnvTests(unittest.TestCase):
    def test_keeps_systemdrive_and_drops_secrets(self):
        with mock.patch.dict(os.environ, {"SYSTEMDRIVE": "C:", "OPENAI_API_KEY": "secret"}):
            env = pt._restricted_env()
        self.assertEqual(env.get("SYSTEMDRIVE"), "C:")
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertEqual(env["PYTHONIOENCODING"], "utf-8")


if __name__ == "__main__":
    unittest.main()
