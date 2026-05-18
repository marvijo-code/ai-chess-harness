import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness_config import config_value


ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "engines" / "codex-chess" / "codex_chess_uci.py"
DEFAULT_ARTIFACT_ROOT = ROOT / "out" / "learner-proof"

PROOF_POSITIONS = [
    {
        "id": "proof-start-knight-rim",
        "fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        "expected_uci": "b1a3",
        "note": "The learned lesson deliberately prefers the unusual legal Na3 move.",
    },
    {
        "id": "proof-e4-e5-knight-rim",
        "fen": "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2",
        "expected_uci": "g1h3",
        "note": "The learned lesson deliberately prefers the unusual legal Nh3 move.",
    },
]


def write_context(context_dir: Path, *, learned: bool) -> None:
    kb_dir = context_dir / "knowledgebase"
    skills_dir = context_dir / "skills"
    kb_dir.mkdir(parents=True, exist_ok=True)
    skills_dir.mkdir(parents=True, exist_ok=True)
    if learned:
        (context_dir / "MEMORY.md").write_text(
            "\n".join(
                [
                    "# Temporary Learner Proof Memory",
                    "",
                    "This isolated context is for a before/after learning proof.",
                    "Apply `knowledgebase/proof-drill.md` directly when the current FEN matches one of its entries.",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        lines = [
            "# Proof Drill",
            "",
            "Temporary learned rule. If the current FEN exactly matches an entry below, choose the listed legal UCI move.",
            "",
        ]
        for item in PROOF_POSITIONS:
            lines.append(f"- `{item['fen']}` -> `{item['expected_uci']}`")
        (kb_dir / "proof-drill.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    else:
        (context_dir / "MEMORY.md").write_text(
            "\n".join(
                [
                    "# Temporary Learner Proof Memory",
                    "",
                    "No proof-drill lesson has been learned in this isolated context.",
                    "",
                ]
            ),
            encoding="utf-8",
        )


class UciSession:
    def __init__(self, context_dir: Path, model: str, effort: str, move_timeout: int):
        env = os.environ.copy()
        env.update(
            {
                "CODEX_CHESS_ROOT": str(ROOT),
                "CODEX_CHESS_ENGINE_NAME": "Codex-chess-learner-proof",
                "CODEX_CHESS_CONTEXT_DIR": str(context_dir),
                "CODEX_CHESS_USE_MEMORY": "true",
                "CODEX_CHESS_USE_SKILLS": "false",
                "CODEX_CHESS_LEARNING_MODE": "true",
                "CODEX_CHESS_MODEL": model,
                "CODEX_CHESS_EFFORT": effort,
                "CODEX_CHESS_MOVE_TIMEOUT_SECONDS": str(move_timeout),
            }
        )
        self.proc = subprocess.Popen(
            [sys.executable, str(ENGINE)],
            cwd=str(ROOT),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self.lines: queue.Queue[str] = queue.Queue()
        self.stderr_lines: queue.Queue[str] = queue.Queue()
        self.stdout_thread = threading.Thread(target=self._read_stream, args=(self.proc.stdout, self.lines), daemon=True)
        self.stderr_thread = threading.Thread(target=self._read_stream, args=(self.proc.stderr, self.stderr_lines), daemon=True)
        self.stdout_thread.start()
        self.stderr_thread.start()

    @staticmethod
    def _read_stream(stream, target: queue.Queue[str]) -> None:
        if stream is None:
            return
        for line in stream:
            target.put(line.rstrip("\r\n"))

    def send(self, command: str) -> None:
        if self.proc.stdin is None:
            raise RuntimeError("UCI stdin is closed")
        self.proc.stdin.write(command + "\n")
        self.proc.stdin.flush()

    def read_until(self, predicate, timeout: float) -> list[str]:
        deadline = time.monotonic() + timeout
        captured: list[str] = []
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"UCI engine exited early with code {self.proc.returncode}")
            try:
                line = self.lines.get(timeout=0.25)
            except queue.Empty:
                continue
            captured.append(line)
            if predicate(line):
                return captured
        raise TimeoutError(f"Timed out waiting for UCI output after {timeout}s; captured={captured[-10:]}")

    def initialize(self, timeout: float) -> list[str]:
        self.send("uci")
        lines = self.read_until(lambda line: line == "uciok", timeout)
        self.send("isready")
        lines += self.read_until(lambda line: line == "readyok", timeout)
        self.send("ucinewgame")
        return lines

    def choose_move(self, fen: str, timeout: float) -> tuple[str, list[str], float]:
        self.send(f"position fen {fen}")
        start = time.monotonic()
        self.send("go wtime 600000 btime 600000 winc 0 binc 0")
        lines = self.read_until(lambda line: line.startswith("bestmove "), timeout)
        elapsed = time.monotonic() - start
        bestmove = ""
        for line in reversed(lines):
            if line.startswith("bestmove "):
                bestmove = line.split(maxsplit=1)[1].strip()
                break
        return bestmove, lines, elapsed

    def close(self) -> None:
        try:
            if self.proc.stdin is not None and self.proc.poll() is None:
                self.send("quit")
        except Exception:
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def run_phase(context_dir: Path, *, model: str, effort: str, move_timeout: int, command_timeout: float) -> dict:
    session = UciSession(context_dir, model, effort, move_timeout)
    try:
        init_lines = session.initialize(command_timeout)
        cases = []
        for item in PROOF_POSITIONS:
            observed, lines, elapsed = session.choose_move(item["fen"], command_timeout + move_timeout)
            cases.append(
                {
                    **item,
                    "observed_uci": observed,
                    "passed": observed == item["expected_uci"],
                    "elapsed_seconds": round(elapsed, 3),
                    "uci_tail": lines[-8:],
                }
            )
        score = sum(1 for item in cases if item["passed"])
        return {
            "context_dir": str(context_dir),
            "score": score,
            "total": len(cases),
            "cases": cases,
            "init_tail": init_lines[-8:],
        }
    finally:
        session.close()


def render_markdown(result: dict) -> str:
    lines = [
        "# Learner Improvement Proof",
        "",
        f"- Verdict: {'PASS' if result['passed'] else 'FAIL'}",
        f"- Model: `{result['model']}`",
        f"- Effort: `{result['effort']}`",
        f"- Before score: {result['before']['score']} / {result['before']['total']}",
        f"- After score: {result['after']['score']} / {result['after']['total']}",
        f"- Improvement: {result['improvement']}",
        "",
        "## Cases",
        "",
    ]
    for before, after in zip(result["before"]["cases"], result["after"]["cases"]):
        lines += [
            f"### {after['id']}",
            "",
            f"- Expected learned move: `{after['expected_uci']}`",
            f"- Before observed: `{before['observed_uci']}` in {before['elapsed_seconds']}s",
            f"- After observed: `{after['observed_uci']}` in {after['elapsed_seconds']}s",
            f"- After passed: {after['passed']}",
            "",
        ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Prove Codex-chess-learner can use a newly learned context and improve.")
    parser.add_argument("--model", default=str(config_value("codex.model", "gpt-5.3-codex")))
    parser.add_argument("--effort", default=str(config_value("codex.learnerMoveEffort", "medium")))
    parser.add_argument("--move-timeout", type=int, default=45)
    parser.add_argument("--command-timeout", type=float, default=45.0)
    parser.add_argument("--artifact-root", type=Path, default=DEFAULT_ARTIFACT_ROOT)
    parser.add_argument("--stamp", default=time.strftime("%Y%m%d-%H%M%S"))
    parser.add_argument("--allow-partial-after", action="store_true")
    args = parser.parse_args()

    run_dir = (args.artifact_root / args.stamp).resolve()
    before_context = run_dir / "before-context"
    after_context = run_dir / "after-context"
    before_context.mkdir(parents=True, exist_ok=True)
    after_context.mkdir(parents=True, exist_ok=True)
    write_context(before_context, learned=False)
    write_context(after_context, learned=True)

    started = time.strftime("%Y-%m-%d %H:%M:%S")
    before = run_phase(
        before_context,
        model=args.model,
        effort=args.effort,
        move_timeout=args.move_timeout,
        command_timeout=args.command_timeout,
    )
    after = run_phase(
        after_context,
        model=args.model,
        effort=args.effort,
        move_timeout=args.move_timeout,
        command_timeout=args.command_timeout,
    )
    improvement = after["score"] - before["score"]
    passed = improvement > 0 and (args.allow_partial_after or after["score"] == after["total"])
    result = {
        "started_at": started,
        "completed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "model": args.model,
        "effort": args.effort,
        "move_timeout": args.move_timeout,
        "artifact_dir": str(run_dir),
        "passed": passed,
        "improvement": improvement,
        "before": before,
        "after": after,
    }
    json_path = run_dir / "learner-improvement-proof.json"
    md_path = run_dir / "learner-improvement-proof.md"
    json_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(result), encoding="utf-8")

    print(f"Learner proof {'PASS' if passed else 'FAIL'}: before {before['score']}/{before['total']}, after {after['score']}/{after['total']}")
    print(f"JSON: {json_path}")
    print(f"Markdown: {md_path}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
