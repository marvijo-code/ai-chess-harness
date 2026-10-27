"""Player-authored verification tools for chess agents.

An agent writes small python helpers (python-chess only) into its own
`tools/` folder. This module is the single policy and execution surface for
them, shared by every harness path that gives an agent tools:

  - the memory repo validator (which reflection edits are allowed),
  - the LLM move path (running the tools before the agent's reply),
  - any future engine that wants the same contract.

Design notes
------------
Single responsibility: policy (which files, how big, what may they import),
execution (run one tool against one position, bounded), and presentation
(render output for a prompt) are separate small functions with no engine,
provider or tournament knowledge.

Dependency inversion: callers pass a directory and a position; nothing here
imports the tournament, the providers or any specific engine.

No cheating: tools are python-chess utilities. Chess engines, tablebases and
network access are rejected at validation and again at run time. Tool output
is advisory; the agent always chooses its own move from the legal list.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import chess

# --- policy -----------------------------------------------------------------

TOOL_PATH = re.compile(r"^tools/[a-z0-9][a-z0-9-]{0,47}\.py$")
# One shared folder, versioned with the harness, that every player may use.
# A player's own additions live in its memory-repo folder (agents/<folder>/tools).
SHARED_DIR = Path(__file__).resolve().parent / "player-tools"
TOOL_MAX_BYTES = 8192
MAX_TOOL_FILES = 6
RUN_TIMEOUT_SECONDS = 8.0
MAX_OUTPUT_CHARS = 1200

# Imports and calls that would turn a "verification tool" into an engine or a
# network client. Matched against the source text, so an obfuscated bypass is
# still the agent's own doing; the run-time re-check is the backstop.
BANNED_PATTERNS = (
    ("chess.engine", "no chess engine (python-chess only)"),
    ("import stockfish", "no Stockfish"),
    ("lc0", "no Leela"),
    ("import urllib", "no network"),
    ("import requests", "no network"),
    ("import socket", "no network"),
    ("import http", "no network"),
    ("import subprocess", "no subprocesses"),
    ("import multiprocessing", "no subprocesses"),
    ("os.system", "no shell"),
    ("os.popen", "no shell"),
    ("__import__", "no dynamic imports"),
    ("eval(", "no eval"),
    ("exec(", "no exec"),
    ("open(", "no file access"),
    ("syzygy", "no tablebases"),
)

TOOL_CONTRACT = (
    'A tool is a python file in tools/ named tools/<lowercase-name>.py using only python-chess. '
    'It reads ONE JSON object on stdin: {"fen": <FEN>, "moves": <UCI list>, "side_to_move": "white"|"black", '
    '"clocks": {"you_ms": n, "opponent_ms": n}}. It prints its findings as plain text (at most a few lines). '
    'The harness runs it before your move and shows you the output. '
    'No chess engine, no tablebase, no network, no files, no subprocesses.'
)


@dataclass
class ToolResult:
    name: str
    ok: bool
    output: str = ""
    error: str = ""
    elapsed_ms: int = 0


@dataclass
class ToolsBlock:
    """Everything the prompt needs to know about the agent's tools this move."""

    results: list[ToolResult] = field(default_factory=list)
    available: list[str] = field(default_factory=list)

    @property
    def any_ran(self) -> bool:
        return bool(self.results)


def tool_name(path: str) -> str:
    return Path(path).stem


def check_source(text: str) -> str | None:
    """Reason the source is not allowed, or None when it is fine."""
    lowered = text.lower()
    for needle, why in BANNED_PATTERNS:
        if needle in lowered:
            return why
    return None


def validate_tool_edit(path: object, content: object, lint: bool = True) -> tuple[bool, str]:
    """Is this reflection edit an acceptable tool? (ok, reason when not).

    Checks the name, the size, the banned imports, that it compiles, and -
    when `lint` is on - that it actually runs on a test position.
    """
    if not isinstance(path, str) or not isinstance(content, str):
        return False, "tool edit needs a string path and string content"
    if not TOOL_PATH.match(path):
        return False, (f"{path[:60]!r} is not tools/<lowercase-name>.py "
                       "(letters, digits, hyphens; .py at the end)")
    size = len(content.encode("utf-8"))
    if size > TOOL_MAX_BYTES:
        return False, f"{path} is {size} bytes, over the {TOOL_MAX_BYTES}-byte tool cap"
    problem = check_source(content)
    if problem:
        return False, f"{path} is not allowed: {problem}"
    try:
        compile(content, path, "exec")
    except SyntaxError as exc:
        return False, f"{path} does not compile: {exc.msg} (line {exc.lineno})"
    if lint:
        ok, reason = lint_tool_content(Path(path).stem, content)
        if not ok:
            return False, reason
    return True, ""


def discover(own_dir: Path | None = None, include_shared: bool = True) -> dict[str, Path]:
    """Tool files by name: the shared folder first, then the player's own (own wins a clash)."""
    found: dict[str, Path] = {}
    folders = ([SHARED_DIR] if include_shared else []) + ([Path(own_dir)] if own_dir else [])
    for folder in folders:
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.py")):
            if TOOL_PATH.match(f"tools/{path.name}"):
                found[path.stem] = path
    return dict(list(found.items())[:MAX_TOOL_FILES])


SMOKE_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


def lint_tool_content(name: str, content: str, *, timeout: float = RUN_TIMEOUT_SECONDS,
                      python: str | None = None) -> tuple[bool, str]:
    """Run a proposed tool once on the start position before it is registered.

    Compiling is not enough: a tool that imports fine can still crash, print
    nothing, or hang, and the player would only find out mid-game. A tool that
    cannot answer the start position is not accepted.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"{name}.py"
        path.write_text(content, encoding="utf-8")
        result = run_tool(path, {"fen": SMOKE_FEN, "moves": [], "side_to_move": "white",
                                 "clocks": {"you_ms": 600000, "opponent_ms": 600000}},
                          timeout=timeout, python=python)
    if not result.ok:
        return False, f"{name} did not run on a test position: {result.error}"
    if not result.output.strip():
        return False, f"{name} printed nothing on a test position"
    return True, ""


def existing_tools(tools_dir: Path | None) -> list[Path]:
    """Backwards-compatible list form."""
    return list(discover(tools_dir).values())


def position_payload(board: chess.Board, history: list[str], go_args: dict | None = None) -> dict:
    go_args = go_args or {}
    own = "wtime" if board.turn == chess.WHITE else "btime"
    other = "btime" if board.turn == chess.WHITE else "wtime"
    return {
        "fen": board.fen(),
        "moves": list(history),
        "side_to_move": "white" if board.turn == chess.WHITE else "black",
        "clocks": {"you_ms": go_args.get(own), "opponent_ms": go_args.get(other)},
    }


def _restricted_env() -> dict:
    """A tool sees no credentials and no PYTHONPATH surprises."""
    keep = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PYTHONIOENCODING", "LOCALAPPDATA")
    env = {k: v for k, v in os.environ.items() if k in keep}
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def run_tool(path: Path, payload: dict, *, timeout: float = RUN_TIMEOUT_SECONDS,
             python: str | None = None) -> ToolResult:
    """Run one agent tool against one position. Never raises."""
    name = tool_name(str(path))
    started = time.monotonic()
    path = Path(path).resolve()   # cwd becomes the tool's folder, so the path must be absolute first
    try:
        source = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return ToolResult(name, False, error=f"unreadable: {exc}")
    problem = check_source(source)
    if problem:
        return ToolResult(name, False, error=f"blocked: {problem}")
    try:
        proc = subprocess.run(
            [python or sys.executable, str(path)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=str(Path(path).parent),
            env=_restricted_env(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return ToolResult(name, False, error=f"no answer within {timeout:.0f}s",
                          elapsed_ms=int((time.monotonic() - started) * 1000))
    except OSError as exc:
        return ToolResult(name, False, error=f"could not start: {exc}")
    elapsed = int((time.monotonic() - started) * 1000)
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip().splitlines()
    if proc.returncode != 0:
        return ToolResult(name, False, error=(err[-1] if err else f"exit {proc.returncode}")[:200],
                          elapsed_ms=elapsed)
    if not out:
        return ToolResult(name, False, error="printed nothing", elapsed_ms=elapsed)
    return ToolResult(name, True, output=out[:MAX_OUTPUT_CHARS], elapsed_ms=elapsed)


def run_tools(tools_dir: Path | None, board: chess.Board, history: list[str],
              go_args: dict | None = None, *, timeout: float = RUN_TIMEOUT_SECONDS,
              python: str | None = None, include_shared: bool = True) -> ToolsBlock:
    """Run the shared tools and the agent's own on this position, in name order."""
    found = discover(tools_dir, include_shared=include_shared)
    if not found:
        return ToolsBlock()
    payload = position_payload(board, history, go_args)
    results = [run_tool(path, payload, timeout=timeout, python=python) for path in found.values()]
    return ToolsBlock(results=results, available=list(found.keys()))


AVOID_LINE = re.compile(r"^\s*AVOID:\s*(.+)$", re.M)
UCI_MOVE = re.compile(r"\b([a-h][1-8][a-h][1-8][qrbn]?)\b")


def flagged_moves(results: list[ToolResult]) -> dict[str, str]:
    """Moves a tool says would lose material, with the sentence that says it.

    A tool states its verdict on a line beginning `AVOID:` followed by UCI moves.
    The sentence above that line is kept as the reason the engine gives back.
    Any tool may do this; the engine only reads, it never invents a flag.
    """
    flagged: dict[str, str] = {}
    for result in results:
        if not result.ok or not result.output:
            continue
        lines = result.output.splitlines()
        reasons = [line.strip() for line in lines if not line.strip().startswith("AVOID:")]
        for index, line in enumerate(lines):
            match = AVOID_LINE.match(line)
            if not match:
                continue
            reason = next((r for r in reversed(reasons[:index]) if r), "")
            reason = re.sub(r"^[-*\s]+", "", reason)[:200]
            for uci in UCI_MOVE.findall(match.group(1)):
                flagged.setdefault(uci, reason or f"{result.name} says this move loses material")
    return flagged


def render_block(block: ToolsBlock, board: chess.Board, history: list[str]) -> str:
    """Prompt text for tool output. Empty when there is nothing to show."""
    if not block.any_ran:
        return ""
    lines = ["Your verification tools have already run on this position. What they found:"
             if block.results else ""]
    for result in block.results:
        if result.ok:
            lines.append(f"[{result.name}] {result.output}")
        else:
            lines.append(f"[{result.name}] could not run: {result.error}")
    lines.append("These are your own checks, not orders: you still choose the move.")
    return "\n".join(line for line in lines if line)


def describe_for_prompt(tools_dir: Path | None) -> str:
    """One line telling an agent how to get tools (used in the reflection prompt)."""
    names = [p.stem for p in existing_tools(tools_dir)]
    have = f"You already have: {', '.join(names)}. " if names else ""
    return (f"You may write verification tools in your own tools/ folder (at most {MAX_TOOL_FILES} files, "
            f"{TOOL_MAX_BYTES} bytes each). {have}{TOOL_CONTRACT} They are yours: write one when you keep "
            "missing the same kind of check, and sharpen it instead of adding a new one.")
