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
# The limit is on the player's OWN tools; the shared folder never counts against it.
MAX_TOOL_FILES = 8
RUN_TIMEOUT_SECONDS = 8.0
MAX_OUTPUT_CHARS = 1200     # shown to the player
RAW_OUTPUT_CHARS = 20000    # read by the tool gate, so an AVOID line past the cut still counts

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
    f'Only the first {MAX_OUTPUT_CHARS} characters of its output are shown, so print the answer first. '
    'To have a move refused once when you pick it, print one line per losing move: '
    '`AVOID: <uci> - <why that move loses>` (several moves may share a line when they lose for the same reason). '
    'No chess engine, no tablebase, no network, no files, no subprocesses.'
)


@dataclass
class ToolResult:
    name: str
    ok: bool
    output: str = ""
    error: str = ""
    elapsed_ms: int = 0
    raw: str = ""       # the whole output before the prompt cut; the gate reads this


@dataclass
class ToolsBlock:
    """Everything the prompt needs to know about the agent's tools this move."""

    results: list[ToolResult] = field(default_factory=list)
    available: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)     # own tools over the limit, not run

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


def _tool_files(folder: Path | None) -> dict[str, Path]:
    if folder is None or not Path(folder).is_dir():
        return {}
    return {path.stem: path for path in sorted(Path(folder).glob("*.py"))
            if TOOL_PATH.match(f"tools/{path.name}")}


def discover_with_skipped(own_dir: Path | None = None,
                          include_shared: bool = True) -> tuple[dict[str, Path], list[str]]:
    """(tools to run by name, own tool names over the limit).

    Every shared tool runs; the player's own tools are capped at MAX_TOOL_FILES in
    name order, and the shared ones never take one of those places. An own tool with
    the same name as a shared one replaces it (own wins a clash).
    """
    shared = _tool_files(SHARED_DIR) if include_shared else {}
    own = _tool_files(own_dir)
    names = list(own)
    kept, skipped = names[:MAX_TOOL_FILES], names[MAX_TOOL_FILES:]
    found = {name: path for name, path in shared.items() if name not in own}
    found.update((name, own[name]) for name in kept)
    return found, skipped


def discover(own_dir: Path | None = None, include_shared: bool = True) -> dict[str, Path]:
    """Tool files by name: every shared tool, then at most MAX_TOOL_FILES of the player's own."""
    return discover_with_skipped(own_dir, include_shared)[0]


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
    # SYSTEMDRIVE matters on Windows: without it Python writes a folder literally named
    # "%SystemDrive%" next to the tool. Windows env names are case-insensitive, so compare upper case.
    keep = ("PATH", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "TEMP", "TMP", "PYTHONIOENCODING", "LOCALAPPDATA")
    env = {k: v for k, v in os.environ.items() if k.upper() in keep}
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
    return ToolResult(name, True, output=cut_output(out), elapsed_ms=elapsed, raw=out[:RAW_OUTPUT_CHARS])


def cut_output(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    """Text up to the limit; a cut text says so, so the player learns to print its answer first."""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n[output cut at {limit} characters - put your answer first]"


def run_tools(tools_dir: Path | None, board: chess.Board, history: list[str],
              go_args: dict | None = None, *, timeout: float = RUN_TIMEOUT_SECONDS,
              python: str | None = None, include_shared: bool = True) -> ToolsBlock:
    """Run the shared tools and the agent's own on this position, in name order."""
    found, skipped = discover_with_skipped(tools_dir, include_shared=include_shared)
    if not found:
        return ToolsBlock()
    payload = position_payload(board, history, go_args)
    results = [run_tool(path, payload, timeout=timeout, python=python) for path in found.values()]
    return ToolsBlock(results=results, available=list(found.keys()), skipped=skipped)


AVOID_LINE = re.compile(r"^\s*AVOID:\s*(.*)$")
UCI_MOVE = re.compile(r"\b([a-h][1-8][a-h][1-8][qrbn]?)\b")
# The UCI moves at the start of an AVOID line, then whatever follows them.
AVOID_BODY = re.compile(r"^((?:[a-h][1-8][a-h][1-8][qrbn]?\b[\s,]*)+)(.*)$")
LEAD_TOKEN = re.compile(r"([^\s:,;()]+)[\s,]*")


def _clean(line: str) -> str:
    """A line without its leading bullet or spaces."""
    return re.sub(r"^[\s*-]+", "", line).strip()


def _move_keys(ucis: list[str], board: chess.Board | None) -> dict[str, str]:
    """{text a reason line may start with: uci}: the UCI, plus the SAN without +/# when the board is known."""
    keys: dict[str, str] = {}
    for uci in ucis:
        keys[uci] = uci
        if board is None:
            continue
        try:
            move = chess.Move.from_uci(uci)
        except ValueError:
            continue
        if move in board.legal_moves:
            keys.setdefault(board.san(move).rstrip("+#"), uci)
    return keys


def _leading_moves(line: str, keys: dict[str, str]) -> list[str]:
    """The listed moves a line starts with ("Ng7: ..." or "Nd4, Ng7 and Nh6: ..."), as UCI."""
    led: list[str] = []
    rest = line
    while True:
        match = LEAD_TOKEN.match(rest)
        if not match:
            break
        token = match.group(1).rstrip("+#!?.")
        if token in keys:
            led.append(keys[token])
        elif not (token == "and" and led):
            break
        rest = rest[match.end():]
    return led


def flagged_moves(results: list[ToolResult], board: chess.Board | None = None) -> dict[str, str]:
    """Moves a tool says would lose material, with the tool's reason for THAT move.

    A tool flags a move in one of two ways:
      - `AVOID: e2e4 - reason` (or `AVOID: e2e4: reason`): the reason is that text;
        several UCI moves may share one reason on one line.
      - `AVOID: e2e4 d2d4` plus, elsewhere in the same output, a line that starts with
        the move's SAN (without +/#) or its UCI: that line is the reason.
    A listed move with no reason of its own is not flagged: the old guess (the nearest
    line above the list) refused good moves and named the wrong move (2026-10-30:
    83 refusals for Sonnet, 5 of them really bad moves). Without a board only the
    first form and UCI-led lines count. The engine only reads; it never invents a flag.
    """
    flagged: dict[str, str] = {}
    for result in results:
        text = result.raw or result.output
        if not result.ok or not text:
            continue
        lines = text.splitlines()
        plain = [_clean(line) for line in lines if not AVOID_LINE.match(line)]
        for line in lines:
            match = AVOID_LINE.match(line)
            if not match:
                continue
            body = AVOID_BODY.match(match.group(1).strip())
            if not body:
                continue
            ucis = UCI_MOVE.findall(body.group(1))
            rest = body.group(2).strip()
            if rest[:1] in ("-", ":") and rest[1:].strip():
                reason = rest[1:].strip()[:200]
                for uci in ucis:
                    flagged.setdefault(uci, reason)
                continue
            keys = _move_keys(ucis, board)
            for text in plain:
                for uci in _leading_moves(text, keys):
                    flagged.setdefault(uci, text[:200])
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
    if block.skipped:
        lines.append(f"Not run: {', '.join(block.skipped)} - only {MAX_TOOL_FILES} of your own tools run "
                     "(shared tools do not count); delete or merge tools to bring the others back.")
    lines.append("These are your own checks, not orders: you still choose the move.")
    return "\n".join(line for line in lines if line)


def describe_for_prompt(tools_dir: Path | None) -> str:
    """One line telling an agent how to get tools (used in the reflection prompt)."""
    names = [p.stem for p in existing_tools(tools_dir)]
    have = f"You already have: {', '.join(names)}. " if names else ""
    return (f"You may write verification tools in your own tools/ folder (at most {MAX_TOOL_FILES} files of your "
            "own; shared tools do not count, "
            f"{TOOL_MAX_BYTES} bytes each). {have}{TOOL_CONTRACT} They are yours: write one when you keep "
            "missing the same kind of check, and sharpen it instead of adding a new one.")
