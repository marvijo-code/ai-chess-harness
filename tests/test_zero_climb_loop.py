import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_loop_module():
    path = ROOT / "tools" / "run_zero_climb_loop.py"
    spec = importlib.util.spec_from_file_location("zero_climb_loop_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def load_ensure_module():
    path = ROOT / "tools" / "ensure_zero_climb_loop.py"
    spec = importlib.util.spec_from_file_location("zero_climb_ensure_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ZeroClimbLoopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.loop = load_loop_module()
        cls.ensure = load_ensure_module()

    def test_build_round_command_uses_profile_and_forwards_args(self):
        command = self.loop.build_round_command(
            "python",
            Path("tools/run_zero_climb.py"),
            "gm-sprint",
            ["--", "--cycles", "1", "--zero-visits", "8"],
        )

        self.assertEqual(command[:4], ["python", "tools\\run_zero_climb.py" if sys.platform == "win32" else "tools/run_zero_climb.py", "--profile", "gm-sprint"])
        self.assertEqual(command[-4:], ["--cycles", "1", "--zero-visits", "8"])

    def test_lock_refuses_overlapping_loop_instances(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "loop.lock"
            with self.loop.LoopLock(lock):
                with self.assertRaises(RuntimeError):
                    with self.loop.LoopLock(lock):
                        pass
            self.assertFalse(lock.exists())

    def test_loop_state_and_log_are_json_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            log = Path(tmp) / "log.jsonl"
            self.loop.write_state(state, {"status": "running", "round": 1})
            self.loop.append_jsonl(log, {"event": "round_started", "round": 1})

            saved_state = json.loads(state.read_text(encoding="utf-8"))
            saved_log = json.loads(log.read_text(encoding="utf-8"))

        self.assertEqual(saved_state["status"], "running")
        self.assertEqual(saved_log["event"], "round_started")

    def test_loop_stops_at_round_and_failure_limits(self):
        args = type("Args", (), {"max_rounds": 2, "max_consecutive_failures": 3})()

        self.assertTrue(self.loop.should_continue(args, rounds_completed=1, consecutive_failures=0))
        self.assertFalse(self.loop.should_continue(args, rounds_completed=2, consecutive_failures=0))
        self.assertFalse(self.loop.should_continue(args, rounds_completed=1, consecutive_failures=3))

    def test_ensure_detects_existing_loop_process(self):
        rows = [
            {"ProcessId": 123, "CommandLine": "python tools/run_zero_climb_loop.py --profile gm-sprint"},
            {"ProcessId": 456, "CommandLine": "python tools/ensure_zero_climb_loop.py"},
        ]

        found = self.ensure.list_loop_processes(rows)

        self.assertEqual(found, [{"pid": 123, "command": rows[0]["CommandLine"]}])

    def test_ensure_builds_loop_command_with_stale_lock_repair(self):
        command = self.ensure.build_loop_command(
            "python",
            Path("tools/zero_loop.py"),
            "gm-sprint",
            ["--", "--cycles", "1"],
            force_stale_lock=True,
        )

        self.assertEqual(command[:4], ["python", "tools\\zero_loop.py" if sys.platform == "win32" else "tools/zero_loop.py", "--profile", "gm-sprint"])
        self.assertIn("--force-stale-lock", command)
        self.assertEqual(command[-2:], ["--cycles", "1"])

    def test_ensure_dry_run_does_not_start_training(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = type(
                "Args",
                (),
                {
                    "python": "python",
                    "loop_runner": Path("tools/run_zero_climb_loop.py"),
                    "profile": "gm-sprint",
                    "repair_stale_lock": True,
                    "dry_run": True,
                    "wait_seconds": 0,
                    "lock": Path(tmp) / "loop.lock",
                    "log": Path(tmp) / "ensure.jsonl",
                },
            )()
            original = self.ensure.list_loop_processes
            self.ensure.list_loop_processes = lambda process_rows=None: []
            try:
                result = self.ensure.ensure_loop(args, ["--cycles", "1"])
            finally:
                self.ensure.list_loop_processes = original

        self.assertEqual(result["status"], "missing")
        self.assertTrue(result["dry_run"])
        self.assertIn("--cycles", result["command"])


if __name__ == "__main__":
    unittest.main()
