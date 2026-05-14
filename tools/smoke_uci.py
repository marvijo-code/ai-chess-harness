import argparse
import subprocess
import sys
import time
from pathlib import Path


def read_until(proc: subprocess.Popen, marker: str, timeout: float) -> list[str]:
    if proc.stdout is None:
        raise RuntimeError("stdout is closed")
    deadline = time.time() + timeout
    lines: list[str] = []
    while time.time() < deadline:
        line = proc.stdout.readline()
        if line == "":
            raise RuntimeError("engine exited unexpectedly")
        line = line.rstrip("\n")
        lines.append(line)
        if marker in line:
            return lines
    raise TimeoutError(f"timed out waiting for {marker!r}; saw {lines!r}")


def send(proc: subprocess.Popen, command: str) -> None:
    if proc.stdin is None:
        raise RuntimeError("stdin is closed")
    proc.stdin.write(command + "\n")
    proc.stdin.flush()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("engine", type=Path)
    parser.add_argument("--movetime-ms", type=int, default=30000)
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()

    proc = subprocess.Popen(
        [str(args.engine)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    try:
        send(proc, "uci")
        print("\n".join(read_until(proc, "uciok", args.timeout)))
        send(proc, "isready")
        print("\n".join(read_until(proc, "readyok", args.timeout)))
        send(proc, "ucinewgame")
        send(proc, "position startpos")
        send(proc, f"go movetime {args.movetime_ms}")
        bestmove_lines = read_until(proc, "bestmove", args.timeout)
        print("\n".join(bestmove_lines))
        if not any(line.startswith("bestmove ") for line in bestmove_lines):
            raise RuntimeError("engine did not return bestmove")
    finally:
        try:
            send(proc, "quit")
        except Exception:
            pass
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"smoke failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
