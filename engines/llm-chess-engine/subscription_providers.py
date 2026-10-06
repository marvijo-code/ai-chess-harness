"""Subscription-backed LLM move providers for llm-chess-engine.

`codex` runs `codex exec` on the local ChatGPT/Codex login and `claude` runs
`claude -p` on the local Claude login, so a model-vs-model match spends no
metered API credit. Every call is isolated from agent context: empty working
directory, no user config, no AGENTS.md/CLAUDE.md, no tools, no hooks, no
saved sessions.

The no-fallback contract matches the OpenRouter client: up to MaxAttempts
replies per move, each retry tells the model why its last reply was rejected,
and the engine forfeits with `0000` after the last failed attempt. The harness
never picks a move for the model.
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Callable

import chess

# CLI routes run a local subscription CLI; HTTP routes post the SAME prompt to an
# OpenAI-compatible chat endpoint, so every player in a tournament sees one prompt.
CLI_PROVIDERS = ("codex", "claude")
HTTP_ROUTES = {
    # Metered OpenRouter credit (only when the owner names this route).
    "openrouter-chat": {"url": "https://openrouter.ai/api/v1/chat/completions", "key": "OPENROUTER_API_KEY", "style": "openrouter"},
    # OpenCode Go subscription (/zen/go/ is the subscription path, /zen/v1 is pay-per-use).
    "opencode-go": {"url": "https://opencode.ai/zen/go/v1/chat/completions", "key": "OPENCODE_GO_API_KEY", "style": "effort"},
    # Z.ai GLM Coding Plan subscription endpoint (the generic /api/paas/v4 bills pay-as-you-go).
    "zai": {"url": "https://api.z.ai/api/coding/paas/v4/chat/completions", "key": "ZAI_API_KEY", "style": "thinking"},
}
PROVIDERS = CLI_PROVIDERS + tuple(HTTP_ROUTES)
DEFAULT_MODELS = {"codex": "gpt-6-sol", "claude": "claude-sonnet-5-5", "openrouter-chat": "x-ai/grok-4.7",
                  "opencode-go": "deepseek-v4.1-flash", "zai": "glm-5.3"}
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"
HTTP_MAX_TOKENS = 32000
# A streaming reply that sends no bytes for this long is a gateway stall, not thinking
# (GLM 5.3 streams reasoning at ~60 chunks/s with gaps under 2 s, measured 2026-10-05).
STALL_SECONDS = 60
# Codex features that give the model a tool (shell, browser, sub-agents, images...).
# The model must pick its move by thinking, never by running code or an engine.
CODEX_DISABLED_FEATURES = (
    "shell_tool", "unified_exec", "unified_exec_tty", "apps", "browser_use", "browser_use_external", "computer_use",
    "in_app_browser", "multi_agent", "image_generation", "view_image", "memories", "plugins", "remote_plugin",
    "code_mode_host", "sleep_tool", "skill_search", "tool_suggest", "goals", "workspace_dependencies", "hooks",
)
# JSONL item types from `codex exec --json` that mean the model used a tool.
CODEX_TOOL_ITEMS = ("command_execution", "file_change", "mcp_tool_call", "web_search", "patch_apply", "tool_call")
DEFAULT_EFFORT = "high"
DEFAULT_ATTEMPT_TIMEOUT_SECONDS = 300
# A crashed CLI process is infrastructure, not a bad model answer: retry it this
# many times per attempt before it spends one of the MaxAttempts chances.
CLI_CRASH_RETRIES = 2
NPM_ROOT = Path(os.environ.get("APPDATA", "")) / "npm" / "node_modules"
CODEX_EXE = NPM_ROOT / "@openai" / "codex" / "node_modules" / "@openai" / "codex-win32-x64" / "vendor" / "x86_64-pc-windows-msvc" / "bin" / "codex.exe"
CLAUDE_EXE = NPM_ROOT / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
# Metered-credit routes must never be picked up silently from the parent shell.
STRIPPED_ENV = {
    "codex": ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL"),
    "claude": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"),
}
SYSTEM_PROMPT = (
    "You are a strong chess player in a game against another AI. You get the position and must answer "
    'with only one JSON object: {"move": "<a move from the legal list, SAN or UCI>", "comment": "<one or two short sentences about your idea>"}. '
    "Choose the move with your own reasoning: do not run code, call tools, or write or use a chess engine."
)
# After the move cap the model gets its own thinking back and answers: first at the configured effort,
# then (only if that brings no move) at the lowest effort the route allows. Measured 2026-10-06 with the
# thinking cut halfway: DeepSeek answered in 6 s and GLM Flash in 2 s at High; Grok, whose visible
# thinking is only a short summary, re-thought for 49 s at High and answered in 2 s at low.
ANSWER_WITH_THOUGHTS_SECONDS = 20.0  # upper bound per answer step; the real limit is a share of the move cap
# Share of the move cap per step: thinking, the same-effort answer, the lowest-effort answer. The whole move
# fits inside the cap (2026-10-06 run 5: cap 30 s + 20 s + 8 s made Grok's moves ~55 s and it flagged).
THINK_SHARE, SAME_SHARE, LOWEST_SHARE = 0.6, 0.2, 0.2
LOWEST_MIN_SECONDS = 5.0
THOUGHTS_HEAD_CHARS = 20_000
THOUGHTS_TAIL_CHARS = 100_000
# The note the referee plays when a model is still thinking at its move cap (its own latest choice).
BEST_SO_FAR = re.compile(r"BEST\s+SO\s+FAR\s*[:=\-]?\s*[*`\"']*\s*(?:\d+\s*\.+\s*)?([A-Za-z0-9=+#\-]{2,8})", re.I)
MARKER_UNSAFE = re.compile(r"[\s\[\]{};]+")
# A plan limit, empty balance or lost login is the provider being unavailable, not a bad move:
# the game is voided and replayed later, never forfeited (2026-10-06: OpenCode Go hit its monthly limit).
OPENROUTER_QUANTIZATIONS = ("fp8", "fp16", "bf16", "fp32", "unknown")
UNAVAILABLE_MARKERS = ("usagelimit", "usage limit", "usage_limit", "insufficient balance", "insufficient_quota",
                       "exceeded your current quota", "credit balance", "payment required", "not logged in",
                       "please run /login", "invalid api key", "unauthorized", "http 401", "http 402", "http 403",
                       "no allowed providers")


class ProviderError(RuntimeError):
    pass


class CliCrash(ProviderError):
    """The CLI exited non-zero without producing an answer."""


def resolve_binary(provider: str) -> str:
    override = os.environ.get(f"LLM_{provider.upper()}_BIN")
    if override:
        return override
    native = CODEX_EXE if provider == "codex" else CLAUDE_EXE
    if native.exists():
        return str(native)
    found = shutil.which(provider)
    if not found:
        raise ProviderError(f"{provider} CLI not found; install it or set LLM_{provider.upper()}_BIN")
    return found


def isolated_workdir() -> Path:
    path = Path(tempfile.gettempdir()) / "llm-chess-isolated"
    path.mkdir(parents=True, exist_ok=True)
    return path


def build_command(provider: str, model: str, effort: str, binary: str, workdir: Path) -> tuple[list[str], Path | None]:
    """Return argv plus the file the final message is written to (codex only)."""
    if provider == "codex":
        last_message = workdir / f"codex-last-{os.getpid()}-{time.time_ns()}.txt"
        argv = [
            binary, "exec",
            "--skip-git-repo-check", "--ignore-user-config", "--ephemeral",
            "--sandbox", "read-only",
            "-m", model,
            "-c", f"model_reasoning_effort={effort}",
            "-c", "project_doc_max_bytes=0",
            "-c", "web_search=disabled",
        ]
        for feature in CODEX_DISABLED_FEATURES:
            argv += ["--disable", feature]
        argv += ["--json", "--color", "never", "-o", str(last_message), "-"]
        return argv, last_message
    if provider == "claude":
        settings = workdir / "claude-settings.json"
        if not settings.exists():
            settings.write_text(json.dumps({"disableAllHooks": True}), encoding="utf-8")
        argv = [
            binary, "-p",
            "--model", model,
            "--effort", effort,
            "--tools", "",
            "--strict-mcp-config",
            "--no-session-persistence",
            "--setting-sources", "",
            "--settings", str(settings),
            "--system-prompt", SYSTEM_PROMPT,
            "--output-format", "stream-json", "--verbose",
        ]
        return argv, None
    raise ProviderError(f"unknown provider {provider!r}; expected one of {PROVIDERS}")


def fmt_clock(ms: object) -> str:
    try:
        total = max(0, int(ms)) // 1000  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "?"
    minutes, seconds = divmod(total, 60)
    return f"{minutes}:{seconds:02d}"


def san_history(history: list[str]) -> str:
    board = chess.Board()
    parts = []
    for uci in history:
        move = chess.Move.from_uci(uci)
        if board.turn == chess.WHITE:
            parts.append(f"{board.fullmove_number}.")
        parts.append(board.san(move))
        board.push(move)
    return " ".join(parts) if parts else "(none, this is the first move)"


def move_budget_seconds(board: chess.Board, remaining_ms: object, increment_ms: object = 0) -> float:
    """Suggested thinking time for this move: the clock spread over the moves still expected."""
    try:
        remaining = max(0.0, float(remaining_ms) / 1000)  # type: ignore[arg-type]
        increment = max(0.0, float(increment_ms or 0) / 1000)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 30.0
    moves_left = max(15, 45 - board.fullmove_number)
    return max(1.0, remaining / moves_left + increment * 0.8)


def arbiter_cutoff_seconds(board: chess.Board, remaining_ms: object, increment_ms: object = 0) -> float:
    """Whole-move cap for streaming models: 1.5x the clock share plus 0.9x the increment (8-60 s),
    never past 25% of its clock. A model cut on every move then still gains time from the increment.

    Past the cap the thinking stops and the model answers from its own full thinking (owner 2026-10-06:
    "give models their original thoughts so they can make informed decisions"). Without a cap High effort
    flagged Grok and DeepSeek in run 4 (Grok averaged 41 s a move, up to 134 s; DeepSeek ran into its
    token limit three times in one move)."""
    try:
        remaining = max(0.0, float(remaining_ms) / 1000)  # type: ignore[arg-type]
        increment = max(0.0, float(increment_ms or 0) / 1000)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 30.0
    moves_left = max(15, 45 - board.fullmove_number)
    cutoff = min(60.0, max(8.0, 1.5 * remaining / moves_left + 0.9 * increment))
    try:
        cutoff = min(cutoff, max(4.0, 0.25 * float(remaining_ms) / 1000))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        pass
    return cutoff


def provider_unavailable(error: str) -> bool:
    lowered = (error or "").lower()
    return any(marker in lowered for marker in UNAVAILABLE_MARKERS)


def overrun_note(think_ms: int | None, cap_seconds: float | None) -> str | None:
    """Kaggle Game Arena style nudge for the next prompt after a move ran past its cap."""
    if not think_ms or cap_seconds is None or think_ms / 1000 <= cap_seconds:
        return None
    return (f"Your previous move took {think_ms / 1000:.0f} seconds, past its {cap_seconds:.0f}-second cap. "
            "Decide faster this move.")


def latest_note(text: str, board: chess.Board) -> chess.Move | None:
    """The model's latest `BEST SO FAR` note that names a legal move. Words that are not moves
    (DeepSeek wrote "write BEST SO FAR lines" while reading the rules) are skipped."""
    for raw in reversed(BEST_SO_FAR.findall(text or "")):
        move = move_from_text(raw.rstrip(".,;"), board)
        if move is not None:
            return move
    return None


def board_diagram(board: chess.Board) -> str:
    """The FEN as an 8x8 diagram with file letters and rank numbers (White uppercase, Black lowercase)."""
    rows = ["  a b c d e f g h"]
    for rank in range(7, -1, -1):
        cells = []
        for file in range(8):
            piece = board.piece_at(chess.square(file, rank))
            cells.append(piece.symbol() if piece else ".")
        rows.append(f"{rank + 1} {' '.join(cells)} {rank + 1}")
    rows.append("  a b c d e f g h")
    return "\n".join(rows)


BOARD_FONT = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "seguisym.ttf"
GLYPHS = {chess.KING: "\u265a", chess.QUEEN: "\u265b", chess.ROOK: "\u265c", chess.BISHOP: "\u265d",
          chess.KNIGHT: "\u265e", chess.PAWN: "\u265f"}


def board_png(board: chess.Board) -> bytes:
    """A 560x560 PNG of the position: White at the bottom, coordinates on all sides, last move highlighted."""
    from PIL import Image, ImageDraw, ImageFont

    sq, margin = 64, 24
    size = sq * 8 + margin * 2
    image = Image.new("RGB", (size, size), (40, 40, 40))
    draw = ImageDraw.Draw(image)
    piece_font = ImageFont.truetype(str(BOARD_FONT), 52)
    label_font = ImageFont.truetype(str(BOARD_FONT), 16)
    last = board.peek() if board.move_stack else None
    for rank in range(8):
        for file in range(8):
            square = chess.square(file, rank)
            x, y = margin + file * sq, margin + (7 - rank) * sq
            light = (file + rank) % 2 == 1
            colour = (240, 217, 181) if light else (181, 136, 99)
            if last and square in (last.from_square, last.to_square):
                colour = (246, 246, 105) if light else (186, 202, 43)
            draw.rectangle([x, y, x + sq - 1, y + sq - 1], fill=colour)
            piece = board.piece_at(square)
            if piece:
                white = piece.color == chess.WHITE
                draw.text((x + sq / 2, y + sq / 2 + 2), GLYPHS[piece.piece_type], font=piece_font, anchor="mm",
                          fill=(255, 255, 255) if white else (0, 0, 0), stroke_width=2,
                          stroke_fill=(0, 0, 0) if white else (255, 255, 255))
    for i in range(8):
        letter, number = "abcdefgh"[i], str(8 - i)
        for y in (margin / 2, size - margin / 2):
            draw.text((margin + i * sq + sq / 2, y), letter, font=label_font, anchor="mm", fill=(230, 230, 230))
        for x in (margin / 2, size - margin / 2):
            draw.text((x, margin + i * sq + sq / 2), number, font=label_font, anchor="mm", fill=(230, 230, 230))
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def build_prompt(board: chess.Board, go_args: dict, history: list[str], rejections: list[str], show_legal: bool = True,
                 cap_seconds: float | None = None, nudge: str | None = None, image: bool = True) -> str:
    side = "White" if board.turn == chess.WHITE else "Black"
    own, opp = ("wtime", "btime") if board.turn == chess.WHITE else ("btime", "wtime")
    # Stable, append-only text first (side, then the game so far) so input caching reuses the
    # longest prefix from the previous move; the parts that change every move come after it.
    lines = [
        f"You are playing {side}.",
        f"Moves so far: {san_history(history)}",
        "It is your move.",
        f"FEN: {board.fen()}",
        "Board diagram from the FEN (White pieces uppercase, Black lowercase, White plays up the board):",
        board_diagram(board),
    ]
    if image:
        lines.append("An image of the same position is attached: White at the bottom, the last move highlighted.")
    if go_args.get(own) is not None:
        inc = go_args.get("winc" if side == "White" else "binc", 0) or 0
        lines.append(f"Clocks: you {fmt_clock(go_args.get(own))}, opponent {fmt_clock(go_args.get(opp))}, +{int(inc) // 1000}s per move.")
        budget = move_budget_seconds(board, go_args.get(own), inc)
        lines.append(
            f"Time budget for this move: about {budget:.0f} seconds of thinking. Your clock only counts your thinking time; "
            "if it reaches 0:00 you lose on time."
        )
        if cap_seconds is not None:
            lines.append(f"If you are still thinking after about {cap_seconds:.0f} seconds, your thinking is stopped, "
                         "you are shown all of it, and you must give your move at once.")
    if nudge:
        lines.append(nudge)
    if board.is_check():
        lines.append("You are in check.")
    if show_legal:
        legal = list(board.legal_moves)
        lines.append("Legal moves (SAN): " + " ".join(board.san(move) for move in legal))
        lines.append("Legal moves (UCI): " + " ".join(move.uci() for move in legal))
    for index, reason in enumerate(rejections, start=1):
        lines.append(f"Attempt {index} was rejected: {reason}. Choose again.")
    lines.append(
        'Reply with ONLY one JSON object: {"move": "<SAN or UCI>", "comment": "<one or two short sentences>"}'
    )
    return "\n".join(lines)


def parse_reply(text: str, board: chess.Board) -> tuple[chess.Move, str, str]:
    """Return (move, comment, raw_move). Raise ValueError with a reason the model can read."""
    text = (text or "").strip()
    if not text:
        raise ValueError("the reply was empty")
    raw_move = ""
    comment = ""
    match = re.search(r"\{.*\}", text, flags=re.S)
    if match:
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            raw_move = str(data.get("move") or data.get("uci") or data.get("san") or "").strip()
            comment = str(data.get("comment") or "").strip()
    if not raw_move:
        bare = text.strip().strip("`\"' .")
        if re.fullmatch(r"[A-Za-z0-9=+#\-]{2,8}", bare):
            raw_move = bare
    if not raw_move:
        raise ValueError(f"the reply was not the requested JSON object: {text[:120]!r}")
    move = move_from_text(raw_move, board)
    if move is None:
        raise ValueError(f"{raw_move!r} is not a legal move in this position")
    return move, comment, raw_move


def move_from_text(raw: str, board: chess.Board) -> chess.Move | None:
    candidate = raw.strip().strip(".")
    try:
        move = chess.Move.from_uci(candidate.lower())
        if move in board.legal_moves:
            return move
    except ValueError:
        pass
    try:
        # parse_san only accepts a SAN string that names exactly one legal move.
        return board.parse_san(candidate)
    except ValueError:
        return None


def marker_text(value: str) -> str:
    return MARKER_UNSAFE.sub("_", value)[:24] or "_"


class SubscriptionChessClient:
    def __init__(self, provider: str, logger: Callable[[str], None]) -> None:
        if provider not in PROVIDERS:
            raise ProviderError(f"unknown provider {provider!r}")
        self.provider = provider
        self.log = logger
        self.model = os.environ.get("LLM_MODEL") or DEFAULT_MODELS[provider]
        self.effort = os.environ.get("LLM_EFFORT") or DEFAULT_EFFORT
        self.max_attempts = _int_env("LLM_MAX_ATTEMPTS", 3, 1, 9)
        self.timeout_seconds = _int_env("LLM_ATTEMPT_TIMEOUT_SECONDS", DEFAULT_ATTEMPT_TIMEOUT_SECONDS, 10, 1800)
        self.show_legal = os.environ.get("LLM_SHOW_LEGAL_MOVES", "true").strip().lower() not in {"0", "false", "no", "off"}
        # Text-only models (Mercury 2.5) get the FEN and the diagram but no picture.
        self.board_image = os.environ.get("LLM_BOARD_IMAGE", "true").strip().lower() not in {"0", "false", "no", "off"}
        self.invalid_model_moves = 0
        self.last_report: dict = {}
        self.runner: Callable[[list[str], str, int, Path | None], str] = self._run_cli
        self.http_post: Callable[[str, dict, dict, int], dict] = self._http_post
        self.http_stream: Callable[[str, dict, dict, int, float | None], dict] = self._http_stream
        self._cutoff_s: float | None = None
        self._board: chess.Board | None = None
        self._image: bytes | None = None
        self._image_board: str | None = None
        self._nudge: str | None = None
        self._infra_ms = 0
        self.on_clock_start: Callable[[int], None] | None = None
        self.session_id = str(uuid.uuid4())
        self.usage_log: list[dict] = []
        self._line_times: list[tuple[float, str]] = []
        self._attempt_think_ms: int | None = None

    def set_option(self, name: str, value: str) -> None:
        lowered = name.lower()
        if lowered in {"model", "openrouter_model"} and value:
            self.model = value.strip()
        elif lowered in {"reasoning", "reasoningeffort", "reasoning_effort", "effort"} and value:
            self.effort = value.strip()
        elif lowered in {"maxattempts", "max_attempts"}:
            try:
                self.max_attempts = max(1, min(9, int(value)))
            except ValueError:
                self.log(f"invalid MaxAttempts option: {value!r}")
        elif lowered in {"timeout", "timeoutseconds", "timeout_seconds"}:
            try:
                self.timeout_seconds = max(10, int(value))
            except ValueError:
                self.log(f"invalid Timeout option: {value!r}")
        elif lowered in {"showlegalmoves", "show_legal_moves"}:
            self.show_legal = value.strip().lower() in {"1", "true", "yes", "on"}

    def new_game(self) -> None:
        self.invalid_model_moves = 0
        self._nudge = None

    def choose_move(self, board: chess.Board, go_args: dict, history: list[str]) -> tuple[str, str]:
        self.last_report = {"tries": 0, "illegal": []}
        if not any(board.legal_moves):
            return "0000", "no legal moves"
        remaining = go_args.get("wtime") if board.turn == chess.WHITE else go_args.get("btime")
        if remaining is not None and remaining <= 0:
            return "0000", f"clock expired; forfeiting without calling {self.provider}"
        rejections: list[str] = []
        last_error = ""
        # Model thinking time for this move, summed over attempts. CLI start-up is not
        # thinking: claude reports its API time and codex brackets the turn with events.
        self.last_report["think_ms"] = 0
        for attempt in range(1, self.max_attempts + 1):
            self.last_report["tries"] = attempt
            left = None if remaining is None else remaining - self.last_report["think_ms"]
            if left is not None and left <= 0:
                # Out of clock: report the time used so the arbiter flags it; never a forfeit.
                return "0000", f"{self.provider} {self.model} ran out of clock while thinking"
            timeout = self._attempt_timeout(left)
            inc = go_args.get("winc" if board.turn == chess.WHITE else "binc", 0)
            self._cutoff_s = arbiter_cutoff_seconds(board, left, inc) if left is not None else None
            self._board = board
            if not self.board_image:
                self._image = None
            elif self._image_board != board.fen():
                self._image = board_png(board)
                self._image_board = board.fen()
            # Only streaming routes show their thinking live, so only they get the BEST SO FAR cap.
            cap = self._cutoff_s if self.provider in HTTP_ROUTES else None
            prompt = build_prompt(board, go_args, history, rejections, self.show_legal, cap, self._nudge, self.board_image)
            started = time.monotonic()
            self._attempt_think_ms = None
            self._infra_ms = 0
            try:
                text = self._ask_with_crash_retries(prompt, timeout)
                self._add_think(started)
                move, comment, _raw = parse_reply(text, board)
                self.invalid_model_moves = 0
                usage = ""
                if self.provider in HTTP_ROUTES and self.usage_log:
                    usage = " usage=" + json.dumps(self.usage_log[-1], separators=(",", ":"))[:300]
                self.log(
                    f"{self.provider} {self.model} attempt {attempt}/{self.max_attempts} ok "
                    f"move={move.uci()} secs={time.monotonic() - started:.1f} think_ms={self.last_report['think_ms']}{usage}"
                )
                self._nudge = overrun_note(self.last_report["think_ms"], cap)
                return move.uci(), comment
            except ValueError as exc:
                self._add_think(started)
                last_error = str(exc)
                rejections.append(last_error)
                bad = re.search(r"'([^']{1,24})' is not a legal move", last_error)
                self.last_report["illegal"].append(marker_text(bad.group(1)) if bad else "invalid")
            except Exception as exc:  # timeouts and CLI failures spend an attempt too
                if isinstance(exc, ProviderError) and provider_unavailable(str(exc)):
                    self.log(f"{self.provider} {self.model} provider unavailable: {str(exc)[:300]}")
                    return "0000", f"provider unavailable: {str(exc)[:200]}"
                self._attempt_think_ms = None  # a timed-out or failed call is charged in full
                self._add_think(started)
                last_error = f"{type(exc).__name__}: {exc}"
                rejections.append("no answer arrived in time" if isinstance(exc, subprocess.TimeoutExpired) else "the reply failed")
                self.last_report["illegal"].append("timeout" if isinstance(exc, subprocess.TimeoutExpired) else "error")
            self.invalid_model_moves += 1
            self.log(
                f"{self.provider} {self.model} attempt {attempt}/{self.max_attempts} rejected after "
                f"{time.monotonic() - started:.1f}s: {last_error[:400]} (invalid_count={self.invalid_model_moves})"
            )
        return "0000", f"{self.provider} {self.model} failed after {self.max_attempts} attempts; forfeiting ({last_error[:200]})"

    def _clock_start(self) -> None:
        """The model has started thinking (CLI start-up over / request sent). Display-only signal."""
        if self.on_clock_start is not None:
            try:
                self.on_clock_start(int(self.last_report.get("think_ms", 0)))
            except Exception:
                pass

    def _add_think(self, started: float) -> None:
        wall_ms = max(0, int((time.monotonic() - started) * 1000) - self._infra_ms)
        self._infra_ms = 0
        measured = self._attempt_think_ms
        think = wall_ms if measured is None else max(0, min(wall_ms, int(measured)))
        self.last_report["think_ms"] = self.last_report.get("think_ms", 0) + think

    def _attempt_timeout(self, remaining: int | None) -> int:
        timeout = self.timeout_seconds
        if remaining is not None:
            timeout = min(timeout, max(10, int(remaining / 1000)))
        return timeout

    def _ask_with_crash_retries(self, prompt: str, timeout: int) -> str:
        for crash in range(CLI_CRASH_RETRIES + 1):
            call_started = time.monotonic()
            try:
                return self._ask(prompt, timeout)
            except CliCrash as exc:
                # Crashes, gateway errors and stalls are infrastructure: not charged to the chess clock.
                self._infra_ms += int((time.monotonic() - call_started) * 1000)
                if crash == CLI_CRASH_RETRIES:
                    raise
                self.log(f"{self.provider} CLI crashed ({exc}); infrastructure retry {crash + 1}/{CLI_CRASH_RETRIES}")
        raise AssertionError("unreachable")

    def _ask(self, prompt: str, timeout: int) -> str:
        if self.provider in HTTP_ROUTES:
            return self._ask_http(prompt, timeout)
        workdir = isolated_workdir()
        argv, last_message = build_command(self.provider, self.model, self.effort, resolve_binary(self.provider), workdir)
        image_file = None
        if self.provider == "codex":
            prompt = SYSTEM_PROMPT + "\n\n" + prompt
            if self._image:
                image_file = workdir / f"board-{os.getpid()}-{time.time_ns()}.png"
                image_file.write_bytes(self._image)
                argv = argv[:-1] + ["-i", str(image_file), "--", "-"]
        elif self.provider == "claude" and self._image:
            argv = argv + ["--input-format", "stream-json"]
            prompt = json.dumps({"type": "user", "message": {"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                             "data": base64.b64encode(self._image).decode("ascii")}},
                {"type": "text", "text": prompt}]}}) + "\n"
        try:
            self._line_times = []
            output = self.runner(argv, prompt, timeout, workdir)
            self._attempt_think_ms = cli_think_ms(self.provider, output, self._line_times)
            if self.provider == "codex":
                used = codex_tool_items(output)
                if used:
                    # A tool call is the model not playing the move itself: it spends an attempt.
                    raise ValueError(f"you used a tool ({', '.join(sorted(used))}); choose the move by reasoning only, with no code or tools")
                if last_message and last_message.exists():
                    return last_message.read_text(encoding="utf-8", errors="replace")
                raise ProviderError(f"codex wrote no final message; output tail: {output[-300:]!r}")
            return claude_result_text(output)
        finally:
            if last_message and last_message.exists():
                last_message.unlink(missing_ok=True)
            if image_file is not None:
                image_file.unlink(missing_ok=True)

    def _ask_http(self, prompt: str, timeout: int) -> str:
        route = HTTP_ROUTES[self.provider]
        api_key = os.environ.get(route["key"]) or _user_env(route["key"])
        if not api_key:
            raise ProviderError(f"{route['key']} is not set")
        user: object = prompt
        if self._image:
            user = [{"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(self._image).decode("ascii")}}]
        payload: dict = {
            "model": self.model,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}],
            "max_tokens": HTTP_MAX_TOKENS,
        }
        effort = (self.effort or "").strip().lower()
        style = route["style"]
        if style == "effort" and self.model.lower().startswith("glm"):
            # GLM has no effort levels: `thinking: enabled` is its full-reasoning switch
            # (measured 2026-10-05: reasoning_effort=high gave GLM 5.3 ~400 reasoning tokens, thinking ~2,500).
            style = "thinking"
        if style == "openrouter":
            payload["reasoning"] = {"effort": effort or "high"}
            # Input cache: one session per engine keeps OpenRouter's sticky routing on the same
            # provider (cache reads bill at 0.25x input for Grok; only a provider `order` turns it off).
            # Full-quality weights only (fp4 hosts are cheapest, so they win by default) on the fastest host:
            # the clock counts wall time (2026-10-06: DeepSeek V4.1 Flash took 78-113 s per move unsorted).
            payload["provider"] = {"sort": "throughput", "quantizations": list(OPENROUTER_QUANTIZATIONS), "require_parameters": True}
            # Per-player price ceiling (USD per million tokens) keeps "fastest" off 2x priority tiers (Gemini, Grok).
            max_price = os.environ.get("LLM_MAX_PRICE")
            if max_price:
                payload["provider"]["max_price"] = json.loads(max_price)
            payload["session_id"] = self.session_id
            payload["prompt_cache_key"] = self.session_id
        elif style == "effort":
            payload["reasoning_effort"] = effort or "high"
        elif style == "thinking":
            payload["thinking"] = {"type": "enabled"}
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "User-Agent": BROWSER_UA}
        if self.provider == "opencode-go":
            headers["x-opencode-session"] = self.session_id
        if self.provider == "openrouter-chat":
            headers["X-Title"] = "ai-chess-harness"
            headers["x-session-id"] = self.session_id
        started = time.monotonic()
        self._clock_start()
        think_cap = None if self._cutoff_s is None else THINK_SHARE * self._cutoff_s
        streamed = self.http_stream(route["url"], {**payload, "stream": True, "stream_options": {"include_usage": True}},
                                    headers, timeout, think_cap)
        self.usage_log.append(streamed.get("usage") or {})
        content = streamed.get("content") or ""
        # Still thinking at the cap, or out of output tokens before any answer (DeepSeek, run 4).
        if not content.strip() and (streamed.get("cut") or streamed.get("finish") == "length"):
            return self._answer_with_thoughts(route["url"], payload, headers, style, streamed, started)
        if not content.strip():
            raise ValueError(f"the reply was empty (finish_reason={streamed.get('finish')}, "
                             f"reasoning_chars={len(streamed.get('reasoning') or '')})")
        return content

    def _answer_with_thoughts(self, url: str, payload: dict, headers: dict, style: str, streamed: dict,
                              started: float) -> str:
        """The model's thinking was stopped: give it all of that thinking back as its own earlier turn and
        ask for the move. Same effort first; the lowest effort the route allows only if that brings no move."""
        thoughts = streamed.get("reasoning") or ""
        if len(thoughts) > THOUGHTS_HEAD_CHARS + THOUGHTS_TAIL_CHARS:
            thoughts = (thoughts[:THOUGHTS_HEAD_CHARS] + "\n[... middle of the thinking omitted for length ...]\n"
                        + thoughts[-THOUGHTS_TAIL_CHARS:])
        total = self._cutoff_s or 30.0
        why = "ran out of output space" if streamed.get("finish") == "length" else f"reached {THINK_SHARE * total:.0f}s of its {total:.0f}s move cap"
        self.log(f"{self.provider} {self.model} thinking stopped after {time.monotonic() - started:.1f}s ({why}); "
                 f"returning its {len(thoughts)} chars of thinking and asking for the move")
        self.last_report["hurried"] = self.last_report.get("hurried", 0) + 1
        messages = payload["messages"] + [
            {"role": "assistant", "content": "My thinking on this move so far:\n" + (thoughts or "(no visible thinking)")},
            {"role": "user", "content": "Your time for this move is up. Your full thinking so far is above. "
                                        "Reply now with only the JSON object for your move."}]
        limits = {"same": min(ANSWER_WITH_THOUGHTS_SECONDS, SAME_SHARE * total),
                  "lowest": min(ANSWER_WITH_THOUGHTS_SECONDS, max(LOWEST_MIN_SECONDS, LOWEST_SHARE * total))}
        for level in ("same", "lowest"):
            follow = {**payload, "messages": messages, "max_tokens": 6000, "stream": True,
                      "stream_options": {"include_usage": True}}
            if level == "lowest":
                if style == "openrouter":
                    follow["reasoning"] = {"effort": "low"}  # Grok and GLM refuse reasoning off on OpenRouter
                elif style == "effort":
                    follow.pop("reasoning_effort", None)
                    follow["thinking"] = {"type": "disabled"}
            answer = self.http_stream(url, follow, headers, int(limits[level]) + 10, limits[level])
            self.usage_log.append(answer.get("usage") or {})
            if (answer.get("content") or "").strip():
                if level == "lowest":
                    self.log(f"{self.provider} {self.model} answered at the lowest effort after a "
                             f"{limits['same']:.0f}s answer at its own effort brought no move")
                return answer["content"]
        note = latest_note(streamed.get("reasoning") or "", self._board or chess.Board())
        if note is not None:
            return json.dumps({"move": note.uci(), "comment": "Its own last stated choice from its thinking."})
        raise ValueError("time was up and no move came back after its thinking was returned to it")

    def _http_stream(self, url: str, payload: dict, headers: dict, timeout: int, cutoff: float | None) -> dict:
        """Stream a chat completion. Past the cutoff, with no answer text yet, the stream is `cut` and the
        thinking so far is returned. No bytes for STALL_SECONDS = gateway stall (infrastructure)."""
        body = json.dumps(payload).encode("utf-8")
        state = {"content": "", "reasoning": "", "usage": {}, "finish": None, "done": False, "error": None,
                 "last": time.monotonic(), "resp": None}

        def reader() -> None:
            request = urllib.request.Request(url, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    state["resp"] = response
                    for raw in response:
                        state["last"] = time.monotonic()
                        line = raw.decode("utf-8", errors="replace").strip()
                        if not line.startswith("data:"):
                            continue
                        chunk = line[5:].strip()
                        if chunk == "[DONE]":
                            break
                        try:
                            data = json.loads(chunk)
                        except json.JSONDecodeError:
                            continue
                        if data.get("usage"):
                            state["usage"] = data["usage"]
                        for choice in data.get("choices") or []:
                            delta = choice.get("delta") or {}
                            state["reasoning"] += str(delta.get("reasoning_content") or delta.get("reasoning") or "")
                            state["content"] += str(delta.get("content") or "")
                            if choice.get("finish_reason"):
                                state["finish"] = choice["finish_reason"]
            except urllib.error.HTTPError as exc:
                state["error"] = ("http", exc.code, exc.read().decode("utf-8", errors="replace")[:400])
            except Exception as exc:  # socket errors; ignored once the stream was cut on purpose
                state["error"] = ("exc", exc)
            finally:
                state["done"] = True

        worker = threading.Thread(target=reader, daemon=True)
        started = time.monotonic()
        worker.start()
        while not state["done"]:
            now = time.monotonic()
            if cutoff is not None and now - started > cutoff and not state["content"].strip():
                _close_quietly(state.get("resp"))
                return {**state, "cut": True}
            if now - state["last"] > STALL_SECONDS:
                _close_quietly(state.get("resp"))
                raise CliCrash(f"stream stalled: no bytes for {STALL_SECONDS}s")
            if now - started > timeout + 2:
                _close_quietly(state.get("resp"))
                raise subprocess.TimeoutExpired(url, timeout)
            time.sleep(0.2)
        error = state["error"]
        if error and error[0] == "http":
            code, text = error[1], error[2]
            if code in {408, 429, 500, 502, 503, 504, 520, 522, 524} or "Upstream request failed" in text:
                time.sleep(3)
                raise CliCrash(f"HTTP {code}: {text[:200]}")
            raise ProviderError(f"HTTP {code}: {text}")
        if error and not state["content"]:
            exc = error[1]
            if isinstance(exc, TimeoutError) or "timed out" in str(exc):
                raise subprocess.TimeoutExpired(url, timeout)
            raise CliCrash(f"{type(exc).__name__}: {exc}")
        return {**state, "cut": False}

    def _http_post(self, url: str, payload: dict, headers: dict, timeout: int) -> dict:
        """POST with a hard wall-clock deadline. Gateway 429/5xx is infrastructure: CliCrash retries it."""
        body = json.dumps(payload).encode("utf-8")
        result: dict = {}

        def call() -> None:
            request = urllib.request.Request(url, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    result["data"] = json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                result["http"] = (exc.code, exc.read().decode("utf-8", errors="replace")[:400])
            except Exception as exc:  # socket errors, bad JSON
                result["error"] = exc

        worker = threading.Thread(target=call, daemon=True)
        worker.start()
        worker.join(timeout + 2)
        if worker.is_alive():
            raise subprocess.TimeoutExpired(url, timeout)
        if "http" in result:
            code, text = result["http"]
            if code in {408, 429, 500, 502, 503, 504, 520, 522, 524} or "Upstream request failed" in text:
                time.sleep(3)
                raise CliCrash(f"HTTP {code}: {text[:200]}")
            raise ProviderError(f"HTTP {code}: {text}")
        if "error" in result:
            exc = result["error"]
            if isinstance(exc, TimeoutError) or "timed out" in str(exc):
                raise subprocess.TimeoutExpired(url, timeout)
            raise CliCrash(f"{type(exc).__name__}: {exc}")
        return result["data"]

    def _run_cli(self, argv: list[str], prompt: str, timeout: int, workdir: Path | None) -> str:
        env = os.environ.copy()
        for key in STRIPPED_ENV[self.provider]:
            env.pop(key, None)
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=str(workdir) if workdir else None,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        # Stream stdout with arrival times so codex turn events can bracket the thinking time.
        lines: list[str] = []
        times = self._line_times = []

        started_signal = False

        def pump() -> None:
            nonlocal started_signal
            assert proc.stdout is not None
            for line in proc.stdout:
                times.append((time.monotonic(), line))
                lines.append(line)
                # The model starts thinking here (CLI start-up is over): tell the viewer clock.
                if not started_signal and ('"turn.started"' in line or '"subtype":"init"' in line):
                    started_signal = True
                    self._clock_start()

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        try:
            assert proc.stdin is not None
            proc.stdin.write(prompt)
            proc.stdin.close()
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_tree(proc)
            raise
        reader.join(10)
        output = "".join(lines)
        if proc.returncode != 0:
            if not output.strip():
                raise CliCrash(f"{self.provider} exited {proc.returncode} with no output")
            raise ProviderError(f"{self.provider} exited {proc.returncode}: {output[-400:]!r}")
        return output


def claude_result_event(output: str) -> dict | None:
    """The result envelope from `--output-format json` (one object) or `stream-json` (last result line)."""
    start = output.find("{")
    if start >= 0:
        try:
            data = json.loads(output[start:])
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    for line in reversed(output.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and (data.get("type") == "result" or "duration_api_ms" in data):
            return data
    return None


def claude_result_text(output: str) -> str:
    data = claude_result_event(output)
    if data is None:
        raise ProviderError(f"claude returned no JSON envelope: {output[-300:]!r}")
    if data.get("is_error"):
        raise ProviderError(f"claude error: {str(data.get('result'))[:300]}")
    return str(data.get("result") or "")


def cli_think_ms(provider: str, output: str, line_times: list[tuple[float, str]]) -> int | None:
    """Model time inside one CLI call, without the CLI's own start-up. None = unknown (charge wall time)."""
    if provider == "claude":
        data = claude_result_event(output) or {}
        try:
            value = data.get("duration_api_ms")
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None
    if provider == "codex":
        began = ended = None
        for stamp, line in line_times:
            if '"turn.started"' in line and began is None:
                began = stamp
            elif '"turn.completed"' in line or '"turn.failed"' in line:
                ended = stamp
        if began is not None and ended is not None and ended >= began:
            return int((ended - began) * 1000)
    return None


def _close_quietly(response: object) -> None:
    try:
        response.close()  # type: ignore[union-attr]
    except Exception:
        pass


def codex_tool_items(jsonl: str) -> set[str]:
    """Item types in `codex exec --json` output that show the model ran a tool."""
    used: set[str] = set()
    for line in (jsonl or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = event.get("item") if isinstance(event, dict) else None
        kind = str(item.get("type") or item.get("item_type") or "") if isinstance(item, dict) else ""
        if kind in CODEX_TOOL_ITEMS:
            used.add(kind)
    return used


def _user_env(name: str) -> str | None:
    """Read a User/Machine scope variable a long-running shell may not have inherited."""
    if os.name != "nt":
        return None
    import winreg

    for hive, path in ((winreg.HKEY_CURRENT_USER, "Environment"),
                       (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
        try:
            with winreg.OpenKey(hive, path) as key:
                return str(winreg.QueryValueEx(key, name)[0])
        except OSError:
            continue
    return None


def kill_tree(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:
        proc.kill()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def _int_env(name: str, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(os.environ.get(name, default))))
    except (TypeError, ValueError):
        return default
