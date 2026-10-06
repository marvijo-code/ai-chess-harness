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
    "Choose the move with your own reasoning: do not run code, call tools, or write or use a chess engine. "
    "While you think, write a line `BEST SO FAR: <move>` each time your preferred move changes."
)
# The note the referee plays when a model is still thinking at its move cap (its own latest choice).
BEST_SO_FAR = re.compile(r"BEST\s+SO\s+FAR\s*[:=\-]?\s*[*`\"']*\s*(?:\d+\s*\.+\s*)?([A-Za-z0-9=+#\-]{2,8})", re.I)
MARKER_UNSAFE = re.compile(r"[\s\[\]{};]+")
# A plan limit, empty balance or lost login is the provider being unavailable, not a bad move:
# the game is voided and replayed later, never forfeited (2026-10-06: OpenCode Go hit its monthly limit).
UNAVAILABLE_MARKERS = ("usagelimit", "usage limit", "usage_limit", "insufficient balance", "insufficient_quota",
                       "exceeded your current quota", "credit balance", "payment required", "not logged in",
                       "please run /login", "invalid api key", "unauthorized", "http 401", "http 402", "http 403")


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
    """Move cap for streaming models: 2x the budget (20-90 s), never past 25% of its clock.

    Past the cap the referee plays the model's own latest `BEST SO FAR` note; with no note yet the
    model keeps thinking at full effort on its own clock (2026-10-06: the old low-effort "answer now"
    retry with a 6-8 s limit decided 5 of 10 games by forfeit, owner: "it doesn't look fair")."""
    budget = move_budget_seconds(board, remaining_ms, increment_ms)
    cutoff = min(90.0, max(20.0, 2.0 * budget))
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
    """The model's latest `BEST SO FAR` note when it names a legal move (an illegal latest note = keep thinking)."""
    notes = BEST_SO_FAR.findall(text or "")
    return move_from_text(notes[-1].rstrip(".,;"), board) if notes else None


def build_prompt(board: chess.Board, go_args: dict, history: list[str], rejections: list[str], show_legal: bool = True,
                 cap_seconds: float | None = None, nudge: str | None = None) -> str:
    side = "White" if board.turn == chess.WHITE else "Black"
    own, opp = ("wtime", "btime") if board.turn == chess.WHITE else ("btime", "wtime")
    # Stable, append-only text first (side, then the game so far) so input caching reuses the
    # longest prefix from the previous move; the parts that change every move come after it.
    lines = [
        f"You are playing {side}.",
        f"Moves so far: {san_history(history)}",
        "It is your move.",
        f"FEN: {board.fen()}",
        "Board (White pieces are uppercase, White plays up the board):",
        str(board),
    ]
    if go_args.get(own) is not None:
        inc = go_args.get("winc" if side == "White" else "binc", 0) or 0
        lines.append(f"Clocks: you {fmt_clock(go_args.get(own))}, opponent {fmt_clock(go_args.get(opp))}, +{int(inc) // 1000}s per move.")
        budget = move_budget_seconds(board, go_args.get(own), inc)
        lines.append(
            f"Time budget for this move: about {budget:.0f} seconds of thinking. Your clock only counts your thinking time; "
            "if it reaches 0:00 you lose on time."
        )
        if cap_seconds is not None:
            lines.append(f"If you are still thinking after about {cap_seconds:.0f} seconds, the referee plays your latest "
                         "BEST SO FAR move. So in the first lines of your thinking write `BEST SO FAR: <move>` for your "
                         "first instinct, then write a new BEST SO FAR line after each candidate you check.")
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
        self.invalid_model_moves = 0
        self.last_report: dict = {}
        self.runner: Callable[[list[str], str, int, Path | None], str] = self._run_cli
        self.http_post: Callable[[str, dict, dict, int], dict] = self._http_post
        self.http_stream: Callable[[str, dict, dict, int, float | None], dict] = self._http_stream
        self._cutoff_s: float | None = None
        self._board: chess.Board | None = None
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
            # Only streaming routes show their thinking live, so only they get the BEST SO FAR cap.
            cap = self._cutoff_s if self.provider in HTTP_ROUTES else None
            prompt = build_prompt(board, go_args, history, rejections, self.show_legal, cap, self._nudge)
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
        if self.provider == "codex":
            prompt = SYSTEM_PROMPT + "\n\n" + prompt
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

    def _ask_http(self, prompt: str, timeout: int) -> str:
        route = HTTP_ROUTES[self.provider]
        api_key = os.environ.get(route["key"]) or _user_env(route["key"])
        if not api_key:
            raise ProviderError(f"{route['key']} is not set")
        payload: dict = {
            "model": self.model,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}],
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
            # provider (cache reads bill at 0.25x input for Grok). No provider order/sort: an order
            # turns sticky routing off.
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
        streamed = self.http_stream(route["url"], {**payload, "stream": True, "stream_options": {"include_usage": True}},
                                    headers, timeout, self._cutoff_s)
        self.usage_log.append(streamed.get("usage") or {})
        content = streamed.get("content") or ""
        if streamed.get("cut") and not content.strip():
            # Referee: still thinking at the move cap, so its own latest BEST SO FAR note is played.
            board = self._board or chess.Board()
            note = latest_note((streamed.get("reasoning") or "") + "\n" + content, board)
            if note is None:
                raise ProviderError("cut at the move cap without a legal BEST SO FAR note")
            self.log(f"{self.provider} {self.model} referee: still thinking after {time.monotonic() - started:.1f}s "
                     f"(cap {self._cutoff_s:.0f}s); playing its latest BEST SO FAR note {note.uci()}")
            self.last_report["hurried"] = self.last_report.get("hurried", 0) + 1
            return json.dumps({"move": note.uci(), "comment": "Still thinking at the move cap; the referee played its latest BEST SO FAR move."})
        if not content.strip():
            raise ValueError(f"the reply was empty (finish_reason={streamed.get('finish')}, "
                             f"reasoning_chars={len(streamed.get('reasoning') or '')})")
        return content

    def _http_stream(self, url: str, payload: dict, headers: dict, timeout: int, cutoff: float | None) -> dict:
        """Stream a chat completion at full effort. Past the cutoff, with no answer text yet, the stream is
        `cut` only once the thinking holds a legal BEST SO FAR note; without one the model keeps thinking
        on its own clock. No bytes for STALL_SECONDS = gateway stall (infrastructure)."""
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
            if (cutoff is not None and now - started > cutoff and not state["content"].strip()
                    and latest_note(state["reasoning"], self._board or chess.Board()) is not None):
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
