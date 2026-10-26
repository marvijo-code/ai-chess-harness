"""Focused tests for the self-review loop (PRD 198, Validation 94).

The loop's contract: a review reply is applied only when it passes the shared
tool policy and the memory validator, and a tool it writes really becomes the
tool the next game runs.
"""
import io
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import series_with_review as review  # noqa: E402


class ReviewLoopTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.agent = Path(self._tmp.name) / "agents" / "deepseek1017"
        (self.agent / "tools").mkdir(parents=True)
        (self.agent / "notes").mkdir(parents=True)
        (self.agent / "MEMORY.md").write_text("# memory\n\n- lesson\n", encoding="utf-8")
        self._old = (review.AGENT, review.TOOLS, review.MEMORY)
        review.AGENT = self.agent
        review.TOOLS = self.agent / "tools"
        review.MEMORY = self.agent / "MEMORY.md"

    def tearDown(self):
        review.AGENT, review.TOOLS, review.MEMORY = self._old
        self._tmp.cleanup()

    def test_prompt_offers_tools_and_memory(self):
        prompt = review.review_prompt("1. e4 e5", "0-1 (checkmate)")
        self.assertIn("GAME", prompt)
        self.assertIn("tools/<name>.py", prompt)
        self.assertIn("python-chess", prompt)
        self.assertIn("MEMORY.md", prompt)

    def test_good_tool_is_written_and_becomes_live(self):
        reply = ('{"summary": "hung a knight", "edits": ['
                 '{"path": "tools/hang-check.py", "content": "import chess\\nprint(1)\\n"},'
                 '{"path": "MEMORY.md", "content": "# memory\\n\\n- count defenders\\n"}]}')
        applied = review.apply_edits(reply)
        self.assertTrue(applied)
        self.assertTrue((self.agent / "tools" / "hang-check.py").is_file())
        self.assertIn("count defenders", (self.agent / "MEMORY.md").read_text(encoding="utf-8"))
        names = [p.stem for p in review.ptools.discover(review.TOOLS).values()]
        self.assertIn("hang-check", names)

    def test_engine_tool_is_rejected(self):
        reply = ('{"summary": "x", "edits": ['
                 '{"path": "tools/engine.py", "content": "import chess.engine\\n"}]}')
        review.apply_edits(reply)
        self.assertFalse((self.agent / "tools" / "engine.py").exists())

    def test_garbage_reply_changes_nothing(self):
        before = (self.agent / "MEMORY.md").read_text(encoding="utf-8")
        applied = review.apply_edits("I think I played badly.")
        self.assertEqual(len(applied), 1)          # it reports the parse failure
        self.assertIn("not parsed", applied[0])
        self.assertEqual((self.agent / "MEMORY.md").read_text(encoding="utf-8"), before)

    def test_tool_cap_is_enforced_through_the_loop(self):
        for i in range(review.ptools.MAX_TOOL_FILES):
            (self.agent / "tools" / f"old{i}.py").write_text("print(1)\n", encoding="utf-8")
        reply = ('{"summary": "x", "edits": ['
                 '{"path": "tools/extra.py", "content": "print(2)\\n"}]}')
        review.apply_edits(reply)
        self.assertFalse((self.agent / "tools" / "extra.py").exists())


if __name__ == "__main__":
    unittest.main()
