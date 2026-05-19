from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOOP_RUNNER_PATH = ROOT / "tools" / "run_zero_climb_loop.py"
CLIMB_DIR = ROOT / "engines" / "codex-chess-zero" / "research" / "climb"
LOOP_LOCK_PATH = CLIMB_DIR / "zero-climb-loop.lock"
ENSURE_LOG_PATH = CLIMB_DIR / "zero-climb-loop-ensure-log.jsonl"
PROCESS_LOG_PATH = CLIMB_DIR / "zero-climb-loop-process.log"


def now_stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")


def normalize_forwarded_args(args: list[str]) -> list[str]:
    return args[1:] if args and args[0] == "--" else args


def build_loop_command(
    python_exe: str,
    loop_runner: Path,
    profile: str,
    round_args: list[str],
    force_stale_lock: bool = False,
) -> list[str]:
    command = [python_exe, str(loop_runner), "--profile", profile]
    if force_stale_lock:
        command.append("--force-stale-lock")
    command.extend(normalize_forwarded_args(round_args))
    return command


def parse_process_rows(raw: str) -> list[dict]:
    raw = raw.strip()
    if not raw:
        return []
    data = json.loads(raw)
    if isinstance(data, dict):
        return [data]
    return [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []


def _windows_python_processes() -> list[dict]:
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if shell is None:
        return []
    command = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -like 'python*' -or $_.Name -like 'py.exe' } | "
        "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
    )
    completed = subprocess.run(
        [shell, "-NoProfile", "-Command", command],
        cwd=str(ROOT),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if completed.returncode != 0:
        return []
    return parse_process_rows(completed.stdout)


def _posix_processes() -> list[dict]:
    completed = subprocess.run(
        ["ps", "-eo", "pid=,args="],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    rows = []
    for line in completed.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        pid, _, command = stripped.partition(" ")
        if pid.isdigit():
            rows.append({"ProcessId": int(pid), "CommandLine": command})
    return rows


def list_loop_processes(process_rows: list[dict] | None = None) -> list[dict]:
    rows = process_rows if process_rows is not None else (_windows_python_processes() if os.name == "nt" else _posix_processes())
    matches = []
    self_pid = os.getpid()
    for row in rows:
        command = str(row.get("CommandLine") or "")
        pid = int(row.get("ProcessId") or row.get("pid") or 0)
        normalized = command.replace("\\", "/")
        if pid == self_pid:
            continue
        if "run_zero_climb_loop.py" in normalized and "ensure_zero_climb_loop.py" not in normalized:
            matches.append({"pid": pid, "command": command})
    return matches


def start_loop(command: list[str]) -> int:
    CLIMB_DIR.mkdir(parents=True, exist_ok=True)
    stdout = PROCESS_LOG_PATH.open("a", encoding="utf-8")
    creationflags = 0
    start_new_session = False
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        start_new_session = True
    proc = subprocess.Popen(
        command,
        cwd=str(ROOT),
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=subprocess.STDOUT,
        creationflags=creationflags,
        start_new_session=start_new_session,
    )
    stdout.close()
    return int(proc.pid)


def ensure_loop(args: argparse.Namespace, round_args: list[str]) -> dict:
    existing = list_loop_processes()
    command = build_loop_command(
        args.python,
        args.loop_runner,
        args.profile,
        round_args,
        force_stale_lock=args.repair_stale_lock and not existing,
    )
    result = {
        "checked_at": now_stamp(),
        "status": "running" if existing else "missing",
        "existing": existing,
        "started_pid": None,
        "command": command,
        "dry_run": args.dry_run,
        "lock_path": str(args.lock),
    }
    if existing or args.dry_run:
        append_jsonl(args.log, result)
        return result

    started_pid = start_loop(command)
    result["started_pid"] = started_pid
    deadline = time.time() + max(0.0, args.wait_seconds)
    verified = []
    while time.time() <= deadline:
        verified = list_loop_processes()
        if any(row["pid"] == started_pid for row in verified) or verified:
            break
        time.sleep(0.5)
    result["existing"] = verified
    result["status"] = "started" if verified else "start_unverified"
    append_jsonl(args.log, result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Ensure the repo-local Zero climb loop is running, then exit.")
    parser.add_argument("--profile", default="gm-sprint")
    parser.add_argument("--wait-seconds", type=float, default=20.0)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--loop-runner", type=Path, default=LOOP_RUNNER_PATH)
    parser.add_argument("--lock", type=Path, default=LOOP_LOCK_PATH)
    parser.add_argument("--log", type=Path, default=ENSURE_LOG_PATH)
    parser.add_argument("--no-repair-stale-lock", dest="repair_stale_lock", action="store_false")
    parser.add_argument("--dry-run", action="store_true")
    parser.set_defaults(repair_stale_lock=True)
    args, round_args = parser.parse_known_args()
    result = ensure_loop(args, round_args)
    print(json.dumps(result, indent=2, sort_keys=True))
    raise SystemExit(0 if result["status"] in {"running", "started"} else 1)


if __name__ == "__main__":
    main()
