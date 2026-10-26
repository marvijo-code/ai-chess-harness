"""Player-authored verification tools: policy, runner, prompt wiring (PRD 195, Validation 91).

The point of these tests is the contract, not a specific tool: an agent writes
python-chess checks in its own tools/ folder, the harness runs them on the real
board before the move, and the output reaches the prompt. No engine, no network.
"""
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

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
            block = pt.run_tools(tools, play(BLUNDER_GAME), BLUNDER_GAME)
            self.assertEqual(len(block.results), 1)
            result = block.results[0]
            self.assertTrue(result.ok, result.error)
            self.assertIn("g5f7", result.output)

    def test_broken_tool_never_raises_and_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            tools = Path(tmp) / "tools"
            tools.mkdir()
            (tools / "boom.py").write_text("raise SystemExit(3)\n", encoding="utf-8")
            block = pt.run_tools(tools, chess.Board(), [])
            self.assertFalse(block.results[0].ok)
            self.assertIn("exit", block.results[0].error)

    def test_empty_folder_is_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(pt.run_tools(Path(tmp) / "none", chess.Board(), []).any_ran)

    def test_source_is_rechecked_at_run_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            tools = Path(tmp) / "tools"
            tools.mkdir()
            (tools / "sneaky.py").write_text("import socket\nprint('x')\n", encoding="utf-8")
            result = pt.run_tools(tools, chess.Board(), []).results[0]
            self.assertFalse(result.ok)
            self.assertIn("blocked", result.error)


class PromptWiringTests(unittest.TestCase):
    def test_tool_output_reaches_the_prompt_last(self):
        with tempfile.TemporaryDirectory() as tmp:
            tools = Path(tmp) / "tools"
            tools.mkdir()
            (tools / "hang-check.py").write_text(HANG_TOOL, encoding="utf-8")
            board = play(BLUNDER_GAME)
            block = pt.run_tools(tools, board, BLUNDER_GAME)
            text = pt.render_block(block, board, BLUNDER_GAME)
            prompt = sp.position_block(board, {"wtime": 600000, "btime": 600000}, True, tools_text=text)
        self.assertIn("your own verification tools", text.lower())
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


if __name__ == "__main__":
    unittest.main()
