from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = ROOT / "tools" / "run_zero_climb.py"
CLIMB_DIR = ROOT / "engines" / "codex-chess-zero" / "research" / "climb"
LOOP_STATE_PATH = CLIMB_DIR / "zero-climb-loop-state.json"
LOOP_LOG_PATH = CLIMB_DIR / "zero-climb-loop-log.jsonl"
LOOP_LOCK_PATH = CLIMB_DIR / "zero-climb-loop.lock"


def now_stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")


def write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def normalize_forwarded_args(args: list[str]) -> list[str]:
    return args[1:] if args and args[0] == "--" else args


def build_round_command(python_exe: str, runner_path: Path, profile: str, round_args: list[str]) -> list[str]:
    return [python_exe, str(runner_path), "--profile", profile, *normalize_forwarded_args(round_args)]


class LoopLock:
    def __init__(self, path: Path, force_stale_lock: bool = False):
        self.path = path
        self.force_stale_lock = force_stale_lock
        self.fd: int | None = None

    def __enter__(self) -> "LoopLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.force_stale_lock and self.path.exists():
            self.path.unlink()
        payload = {
            "pid": os.getpid(),
            "created_at": now_stamp(),
            "cwd": str(ROOT),
            "command": sys.argv,
        }
        try:
            self.fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            existing = self.path.read_text(encoding="utf-8", errors="replace") if self.path.exists() else ""
            raise RuntimeError(f"Zero climb loop lock already exists: {self.path}\n{existing}") from exc
        os.write(self.fd, json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"))
        os.write(self.fd, b"\n")
        return self

    def __exit__(self, *args) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        with contextlib.suppress(FileNotFoundError, PermissionError):
            self.path.unlink()


def run_loop(args: argparse.Namespace, round_args: list[str]) -> int:
    command = build_round_command(args.python, args.runner, args.profile, round_args)
    consecutive_failures = 0
    rounds_completed = 0
    with LoopLock(args.lock, force_stale_lock=args.force_stale_lock):
        while True:
            round_index = rounds_completed + 1
            started = time.time()
            row = {
                "event": "round_started",
                "round": round_index,
                "started_at": now_stamp(),
                "command": command,
            }
            append_jsonl(args.log, row)
            write_state(
                args.state,
                {
                    "status": "running",
                    "updated_at": now_stamp(),
                    "round": round_index,
                    "profile": args.profile,
                    "command": command,
                    "pid": os.getpid(),
                    "consecutive_failures": consecutive_failures,
                },
            )
            completed = subprocess.run(command, cwd=str(ROOT), check=False)
            duration = round(time.time() - started, 3)
            rounds_completed += 1
            if completed.returncode == 0:
                consecutive_failures = 0
            else:
                consecutive_failures += 1
            finished = {
                "event": "round_finished",
                "round": round_index,
                "finished_at": now_stamp(),
                "duration_seconds": duration,
                "returncode": completed.returncode,
                "consecutive_failures": consecutive_failures,
            }
            append_jsonl(args.log, finished)
            write_state(
                args.state,
                {
                    "status": "sleeping" if should_continue(args, rounds_completed, consecutive_failures) else "stopped",
                    "updated_at": now_stamp(),
                    "round": round_index,
                    "profile": args.profile,
                    "last_returncode": completed.returncode,
                    "last_duration_seconds": duration,
                    "rounds_completed": rounds_completed,
                    "consecutive_failures": consecutive_failures,
                    "command": command,
                    "pid": os.getpid(),
                },
            )
            if not should_continue(args, rounds_completed, consecutive_failures):
                return completed.returncode
            sleep_seconds = args.sleep_seconds if completed.returncode == 0 else args.failure_sleep_seconds
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)


def should_continue(args: argparse.Namespace, rounds_completed: int, consecutive_failures: int) -> bool:
    if args.max_rounds > 0 and rounds_completed >= args.max_rounds:
        return False
    if args.max_consecutive_failures > 0 and consecutive_failures >= args.max_consecutive_failures:
        return False
    return True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Continuously run Codex-chess-zero climb rounds back-to-back from this repo."
    )
    parser.add_argument("--profile", default="gm-sprint")
    parser.add_argument("--max-rounds", type=int, default=0, help="0 means keep looping until interrupted.")
    parser.add_argument("--sleep-seconds", type=float, default=0.0)
    parser.add_argument("--failure-sleep-seconds", type=float, default=60.0)
    parser.add_argument("--max-consecutive-failures", type=int, default=3)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--runner", type=Path, default=RUNNER_PATH)
    parser.add_argument("--state", type=Path, default=LOOP_STATE_PATH)
    parser.add_argument("--log", type=Path, default=LOOP_LOG_PATH)
    parser.add_argument("--lock", type=Path, default=LOOP_LOCK_PATH)
    parser.add_argument("--force-stale-lock", action="store_true")
    args, round_args = parser.parse_known_args()
    try:
        raise SystemExit(run_loop(args, round_args))
    except KeyboardInterrupt:
        print("Zero climb loop interrupted.", file=sys.stderr)
        raise SystemExit(130)


if __name__ == "__main__":
    main()
