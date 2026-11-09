"""100 live integration tests: the hardest Lichess puzzles with exactly one solution, played by a real model.

Live and metered, so gated: RUN_LIVE_TESTS=1. One test per puzzle, easiest first. The suite stops at
the FIRST failed puzzle (every later test is never run), whatever runner starts it.

    $env:RUN_LIVE_TESTS=1; python -m unittest tests.test_lichess_puzzles_live -v

Settings (environment):
  PUZZLE_MODEL       default z-ai/glm-5.3-flash
  PUZZLE_PROVIDER    default openrouter-chat (metered OpenRouter credit)
  PUZZLE_EFFORT      default high
  PUZZLE_TOOLS_DIR   the model's own tools folder; unset = baseline, no tools at all
  PUZZLE_BUDGET_USD  default 2: a test fails, and the suite stops, once this much was spent
  PUZZLE_OUT         where the context files and results go (default out/puzzles/live-tests)

Every move goes through the real engine (engines/llm-chess-engine); the harness never picks a move.
"""
import json
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "engines" / "llm-chess-engine"))

import lichess_puzzles  # noqa: E402
import puzzle_gauntlet as pg  # noqa: E402

LIVE = os.environ.get("RUN_LIVE_TESTS") == "1"
PUZZLES = lichess_puzzles.load() if lichess_puzzles.FIXTURE.is_file() else []


class FailFastCase(unittest.TestCase):
    """Stop the whole run at the first failure or error, also without `-f` / `pytest -x`.

    unittest: the result is told to stop, so no later test starts. A runner that ignores that
    (pytest without -x) still never reaches the model again: every later test skips itself."""

    stopped_by: str | None = None

    def setUp(self):
        if FailFastCase.stopped_by:
            self.skipTest(FailFastCase.stopped_by)

    def run(self, result=None):
        result = result if result is not None else self.defaultTestResult()
        seen = len(getattr(result, "failures", [])) + len(getattr(result, "errors", []))
        outcome = super().run(result)
        now = len(getattr(result, "failures", [])) + len(getattr(result, "errors", []))
        if now > seen and not FailFastCase.stopped_by:
            FailFastCase.stopped_by = f"stopped after the first failure ({self._testMethodName})"
        if FailFastCase.stopped_by and hasattr(result, "stop"):
            result.stop()
        return outcome

    def fail_fast(self, why: str):
        """Fail this test and mark the run stopped, whatever the runner does with results."""
        FailFastCase.stopped_by = f"stopped after the first failure ({self._testMethodName})"
        self.fail(why)


@unittest.skipUnless(LIVE, "live model test: set RUN_LIVE_TESTS=1")
class HardestLichessPuzzles(FailFastCase):
    engine = None
    spend = None
    results: list = []

    @classmethod
    def setUpClass(cls):
        cls.out = Path(os.environ.get("PUZZLE_OUT") or ROOT / "out" / "puzzles" / "live-tests")
        cls.out.mkdir(parents=True, exist_ok=True)
        provider = os.environ.get("PUZZLE_PROVIDER", pg.DEFAULT_PROVIDER)
        tools_dir = os.environ.get("PUZZLE_TOOLS_DIR") or None
        cls.budget = float(os.environ.get("PUZZLE_BUDGET_USD", "2"))
        cls.spend = pg.Spend() if provider == "openrouter-chat" else None
        cls.engine = pg.open_engine(pg.player_config(os.environ.get("PUZZLE_MODEL", pg.DEFAULT_MODEL), provider,
                                                     os.environ.get("PUZZLE_EFFORT", "high"), tools_dir,
                                                     shared_tools=False), pg.engine_cfg())

    @classmethod
    def tearDownClass(cls):
        if cls.engine is not None:
            cls.engine.close()
        (cls.out / "results.json").write_text(json.dumps({
            "passed": sum(r["passed"] for r in cls.results), "played": len(cls.results),
            "usd": round(cls.spend.used(), 4) if cls.spend else None, "results": cls.results}, indent=1),
            encoding="utf-8")

    def check(self, puzzle):
        if self.spend is not None:
            used = self.spend.used()
            if used >= self.budget:
                self.fail_fast(f"budget of {self.budget} USD used up ({used:.4f})")
        result = pg.solve(self.engine, puzzle, self.out / "context", log=lambda m: print(m, flush=True))
        from dataclasses import asdict

        type(self).results.append(asdict(result))
        if not result.passed:
            self.fail_fast(f"puzzle {puzzle['number']} {puzzle['url']} (rating {puzzle['rating']}): "
                           f"{result.reason} at ply {result.failed_ply}")


def _make(puzzle):
    def test(self):
        self.check(puzzle)
    test.__doc__ = f"Lichess {puzzle['id']} rated {puzzle['rating']} ({', '.join(puzzle['themes'][:3])})"
    return test


for _p in PUZZLES:
    setattr(HardestLichessPuzzles, f"test_p{_p['number']:03d}_{_p['id']}", _make(_p))


@unittest.skipUnless(LIVE, "live model test: set RUN_LIVE_TESTS=1")
class FixturePresent(unittest.TestCase):
    def test_fixture_has_100_puzzles(self):
        self.assertEqual(len(PUZZLES), 100, "run tools/lichess_puzzles.py build first")


if __name__ == "__main__":
    unittest.main(failfast=True)
