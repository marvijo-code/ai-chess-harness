import tempfile
import unittest
from pathlib import Path

from tools.prove_learner_improvement import PROOF_POSITIONS, render_markdown, write_context


class LearnerImprovementProofTests(unittest.TestCase):
    def test_learned_context_writes_proof_lesson_without_touching_real_learner(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            context = Path(temp_dir) / "after-context"
            write_context(context, learned=True)

            lesson = (context / "knowledgebase" / "proof-drill.md").read_text(encoding="utf-8")
            memory = (context / "MEMORY.md").read_text(encoding="utf-8")

        self.assertIn("Temporary learned rule", lesson)
        self.assertIn("proof-drill.md", memory)
        for item in PROOF_POSITIONS:
            self.assertIn(item["fen"], lesson)
            self.assertIn(item["expected_uci"], lesson)

    def test_render_markdown_reports_before_after_delta(self):
        result = {
            "passed": True,
            "model": "gpt-test",
            "effort": "medium",
            "improvement": 2,
            "before": {
                "score": 0,
                "total": 1,
                "cases": [{"id": "case", "observed_uci": "e2e4", "elapsed_seconds": 1.0}],
            },
            "after": {
                "score": 1,
                "total": 1,
                "cases": [{"id": "case", "expected_uci": "b1a3", "observed_uci": "b1a3", "elapsed_seconds": 1.0, "passed": True}],
            },
        }

        markdown = render_markdown(result)

        self.assertIn("Verdict: PASS", markdown)
        self.assertIn("Before score: 0 / 1", markdown)
        self.assertIn("After score: 1 / 1", markdown)
        self.assertIn("Expected learned move: `b1a3`", markdown)


if __name__ == "__main__":
    unittest.main()
