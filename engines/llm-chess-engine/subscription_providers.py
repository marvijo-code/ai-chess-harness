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
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Callable

import chess

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import player_tools as player_tools_mod  # noqa: E402  (shared policy: validate, run, render)

# CLI routes run a local subscription CLI; HTTP routes post the SAME prompt to an
# OpenAI-compatible chat endpoint, so every player in a tournament sees one prompt.
CLI_PROVIDERS = ("codex", "claude")
HTTP_ROUTES = {
    # Metered OpenRouter credit (only when the owner names this route).
    "openrouter-chat": {"url": "https://openrouter.ai/api/v1/chat/completions", "key": "OPENROUTER_API_KEY", "style": "openrouter"},
    # OpenCode Go subscription (/zen/go/ is the subscription path, /zen/v1 is pay-per-use).
    "opencode-go": {"url": "https://opencode.ai/zen/go/v1/chat/completions", "key": "OPENCODE_GO_API_KEY", "style": "effort"},
    # OpenCode Zen free tier (owner 2026-10-25): deepseek-v4.1-flash-free is the 1017 snapshot and
    # glm-5.3-flash-free is the same GLM, and both cost nothing, so they are a player's first fallback.
    # A second OpenCode Go subscription carries its own free quota: OPENCODE_GO_API_KEY_2 is asked first.
    "opencode-zen": {"url": "https://opencode.ai/zen/v1/chat/completions", "key": "OPENCODE_GO_API_KEY_2",
                     "alt_keys": ["OPENCODE_GO_API_KEY"], "style": "effort"},
    # Z.ai GLM Coding Plan subscription endpoint (the generic /api/paas/v4 bills pay-as-you-go).
    "zai": {"url": "https://api.z.ai/api/coding/paas/v4/chat/completions", "key": "ZAI_API_KEY", "style": "thinking"},
    # Alibaba (Bailian) Token Plan subscription: Anthropic Messages wire only (the OpenAI path is 404).
    # Measured 2026-10-08: deepseek-v4.1-flash and glm-5.3 answer; prefix caching is automatic and reported as
    # cache_read_input_tokens (input_tokens is then only the uncached part).
    "alibaba": {"url": "https://token-plan.ap-southeast-1.maas.aliyuncs.com/apps/anthropic/v1/messages",
                "key": "ALIBABA_TOKEN_PLAN_API_KEY", "style": "anthropic"},
}
# Anthropic-wire thinking budget per effort; max_tokens stays HTTP_MAX_TOKENS, so at least half of the output
# room is left for the answer after the longest thinking (DeepSeek can otherwise spend it all on reasoning).
ANTHROPIC_THINKING_BUDGET = {"low": 4000, "medium": 8000, "high": 16000, "xhigh": 16000, "max": 16000}
ANTHROPIC_VERSION = "2023-06-01"
PROVIDERS = CLI_PROVIDERS + tuple(HTTP_ROUTES)
DEFAULT_MODELS = {"codex": "gpt-6-sol", "claude": "claude-sonnet-5-5", "openrouter-chat": "x-ai/grok-4.7",
                  "opencode-go": "deepseek-v4.1-flash", "opencode-zen": "deepseek-v4.1-flash-free",
                  "zai": "glm-5.3", "alibaba": "deepseek-v4.1-flash"}
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"
HTTP_MAX_TOKENS = 32000
# A streaming reply that sends no bytes for this long is a gateway stall, not thinking
# (GLM 5.3 streams reasoning at ~60 chunks/s with gaps under 2 s, measured 2026-10-05).
STALL_SECONDS = 60
# Loop guard (owner 2026-10-16): stop a reasoning stream that repeats one line, then ask for the move from its
# thinking up to the repetition. DeepSeek V4.1 Flash repeated "What about 10.Bxh7+? Kxh7." for 384,000 tokens
# (1,379 s of its clock, tournament #43). A loop = one 8-word phrase 20+ times in the newest 8,192 characters;
# no other stored thinking file of any model reached 20 even across its whole text. LLM_LOOP_GUARD=0 turns it off.
LOOP_WINDOW_CHARS = 8192
LOOP_PHRASE_WORDS = 8
LOOP_REPEATS = 20
LOOP_CHECK_EVERY_CHARS = 1024
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
    "Choose the move with your own reasoning. You may not call tools yourself: write your own verification "
    "tools in your tools folder between games, and the harness runs them for you and shows the output."
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
# The last step (lowest effort, own thinking returned) is NOT cut at its share: it runs until the model answers
# or its clock runs out. Run 6 (2026-10-06): a ~6 s hard limit there turned slow first tokens into "invalid
# replies" and forfeited Muse, Qwen, MiMo and Grok in round 1. Out of clock is a time loss, never invalid.
ANSWER_MAX_TOKENS = 16000
# CLI routes (Codex, Claude) have no same-effort answer step: they are stopped at 80% of the cap and asked
# again at low effort with whatever thinking they revealed (run 7: GPT averaged 30 s a move with no cap and flagged).
CLI_CUT_SHARE = THINK_SHARE + SAME_SHARE
THOUGHTS_HEAD_CHARS = 20_000
THOUGHTS_TAIL_CHARS = 100_000
# Claude CLI thinking display. The default is "omitted": every thinking block arrives with empty text and only a
# signature (measured 2026-10-16, Claude Code 2.1.296), so the viewer showed no thinking for Sonnet at all.
# "summarized" returns the API-side thinking summaries. Empty LLM_CLAUDE_THINKING_DISPLAY leaves the flag out.
CLAUDE_THINKING_DISPLAY = "summarized"
# Keep reasoning across moves (owner 2026-10-16): the HTTP routes drop a reasoning_content passed back in the
# history (OpenCode Go DeepSeek answered NONE to a planted word, 2026-10-16), so the engine carries the model's
# own previous thinking forward as text in the next turn, at most this many characters (the newest part).
CARRY_THOUGHTS_MAX_CHARS = 24_000
# Compaction (owner 2026-10-16): when one game's conversation reaches this many input tokens, a helper model
# summarises the player's own thinking and the game goes on in a fresh session that starts with that summary.
# It runs in the background right after the player's move, while the opponent thinks.
COMPACT_TIMEOUT_SECONDS = 300
COMPACT_INPUT_MAX_CHARS = 300_000
COMPACT_THOUGHTS_PER_MOVE_CHARS = 12_000
COMPACT_MAX_OUTPUT_TOKENS = 16_000
COMPACT_SUMMARY_WORDS = 700
COMPACT_GIVE_UP_AFTER = 3
COMPACT_SYSTEM = (
    "You compress one chess player's own working notes from a game that is still being played, so the player can "
    "go on in a fresh conversation. Write to the player in the second person ('you'). Keep only what the player "
    "itself thought or decided: its plans, the threats and tactics it was watching, what it expects from the "
    "opponent, how the opponent has played so far, how it has used its clock, and anything it promised itself. "
    "Do not judge the quality of any move, do not add analysis, evaluations or advice of your own, and do not "
    "invent facts. Do not list the pieces or describe the position: the player gets the exact current position "
    "separately. Plain text, no JSON."
)
# A CLI reply that ran out of output tokens (Claude: "Claude's response exceeded the 32000 output token maximum";
# OpenAI Responses: "max_output_tokens"). The model answers again at low effort from the thinking it showed.
OUTPUT_LIMIT_MARKERS = ("output token maximum", "max_output_tokens", "maximum output tokens", "max output tokens")
# The note the referee plays when a model is still thinking at its move cap (its own latest choice).
BEST_SO_FAR = re.compile(r"BEST\s+SO\s+FAR\s*[:=\-]?\s*[*`\"']*\s*(?:\d+\s*\.+\s*)?([A-Za-z0-9=+#\-]{2,8})", re.I)
MARKER_UNSAFE = re.compile(r"[\s\[\]{};]+")
# A plan limit, empty balance or lost login is the provider being unavailable, not a bad move:
# the game is voided and replayed later, never forfeited (2026-10-06: OpenCode Go hit its monthly limit).
OPENROUTER_QUANTIZATIONS = ("fp8", "fp16", "bf16", "fp32", "unknown")
UNAVAILABLE_MARKERS = ("usagelimit", "usage limit", "usage_limit", "insufficient balance", "insufficient_quota",
                       "accessdenied.unpurchased", "throttling.allocationquota",
                       "exceeded your current quota", "credit balance", "payment required", "not logged in",
                       "please run /login", "invalid api key", "unauthorized", "http 401", "http 402", "http 403",
                       "no allowed providers")
# A subscription usage or rate limit (forever tournament, LLM_LIMIT_WAIT=1): the engine waits and asks the
# SAME move again once the limit resets. Never a forfeit, never a fallback move, never charged to the clock.
LIMIT_MARKERS = ("usagelimit", "usage limit", "usage_limit", "rate limit", "rate_limit", "ratelimit", "too many requests",
                 "throttling", "allocationquota", "unpurchased",
                 "hit your limit", "limit reached", "limit exceeded", "quota", "try again at", "try again in",
                 "overloaded", "capacity")
LIMIT_429 = re.compile(r"(?:http|status|code)[^0-9]{0,4}429\b|\b429 too many", re.I)
LIMIT_WAIT_FIRST_SECONDS = 60.0
LIMIT_WAIT_MAX_SECONDS = 450.0   # was 900: a plan that resets is noticed within 7.5 minutes
CLAUDE_RESET = re.compile(r"usage limit reached\|(\d{9,11})", re.I)
ISO_TIME = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?")
NOTE_MAX_CHARS = 200
# Static rules for the cache-friendly prompt (forever tournament). Byte-identical for every player, game and
# move: nothing in it may depend on the player, the game, the clock or the time of day.
RULES_TEXT = """AI CHESS TOURNAMENT RULES (the same text for every player, every game and every move)
1. You play one game of chess against another AI player or against the Stockfish engine.
2. On every turn you get the moves played since your last turn, then the current position: the FEN, a board diagram (White pieces uppercase, Black lowercase, White plays up the board), the legal moves in SAN and in UCI, and both clocks.
3. Reply with ONLY one JSON object: {"move": "<one legal move, SAN or UCI>", "comment": "<one or two short sentences about your idea>", "note": "<optional private note to yourself, at most 200 characters>"}
4. The note is optional. Use it to remember a plan, a threat or a lesson from this game. Your notes are saved with the game, you see them again after the game when you update your memory, and they are public after the game.
5. Choose the move with your own reasoning. Do not run code, call tools, or use a chess engine.
6. A reply that is not the JSON object, or that names an illegal move, is rejected and you get another try with the reason. After the last allowed try (see the game header) the referee plays your latest legal BEST SO FAR move (rule 11); with none, you forfeit the game.
7. Your clock runs only while you think. If it reaches 0:00 you lose on time. You get the increment after every move you make.
8. A game is drawn by stalemate, insufficient material, threefold repetition, the 50-move rule, or at the game's ply cap.
9. Before you move, look at every check, capture and threat for both sides, and do not leave a piece undefended by accident.
10. Your memory below holds what you wrote after your earlier games. It does not change during a game; you update it after the game.
11. While you think, write a line `BEST SO FAR: <move>` (for example `BEST SO FAR: Nf3`) as soon as you have a first choice, and a new one each time your choice changes. If your thinking ends without an answer (for example because it keeps repeating itself) or no valid reply comes, the referee plays your latest legal BEST SO FAR move."""


class ProviderError(RuntimeError):
    pass


class CliCrash(ProviderError):
    """The CLI exited non-zero without producing an answer."""


class CliCut(Exception):
    """The CLI was still thinking at its move cap and was stopped (not an error, not a rejected reply)."""


class OutputLimit(CliCut):
    """The CLI reply ran out of output tokens before it gave an answer (answered like a cut: from its thinking)."""


def output_limit_error(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in OUTPUT_LIMIT_MARKERS)


def resolve_binary(provider: str) -> str:
    """The CLI executable. Windows: the native npm exe first (a .cmd shim breaks argument quoting).
    Linux and macOS: PATH first, then the usual install places (~/.local/bin/claude, /usr/bin/codex),
    because a service or a non-login ssh shell often has a short PATH."""
    override = os.environ.get(f"LLM_{provider.upper()}_BIN")
    if override:
        return override
    if os.name == "nt":
        native = CODEX_EXE if provider == "codex" else CLAUDE_EXE
        if native.exists():
            return str(native)
        found = shutil.which(provider)
    else:
        found = shutil.which(provider)
        if not found:
            for folder in (Path.home() / ".local" / "bin", Path("/usr/local/bin"), Path("/usr/bin"),
                           Path.home() / ".npm-global" / "bin"):
                candidate = folder / provider
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    found = str(candidate)
                    break
    if not found:
        raise ProviderError(f"{provider} CLI not found; install it or set LLM_{provider.upper()}_BIN")
    return found


def isolated_workdir() -> Path:
    path = Path(tempfile.gettempdir()) / "llm-chess-isolated"
    path.mkdir(parents=True, exist_ok=True)
    return path


def build_command(provider: str, model: str, effort: str, binary: str, workdir: Path,
                  system_prompt: str = SYSTEM_PROMPT, session: dict | None = None) -> tuple[list[str], Path | None]:
    """Return argv plus the file the final message is written to (codex only).

    `session` turns on session continuation (one provider session per game, so each move sends only what
    changed and the rest is a cached prefix): {"mode": "new", "id": uuid} starts it (claude needs the id,
    codex reports its own thread id), {"mode": "resume", "id": id} continues it."""
    if provider == "codex":
        last_message = workdir / f"codex-last-{os.getpid()}-{time.time_ns()}.txt"
        if session and session.get("mode") == "resume":
            argv = [binary, "exec", "resume", "--skip-git-repo-check", "--ignore-user-config",
                    "-c", 'sandbox_mode="read-only"', "-m", model]
        else:
            argv = [binary, "exec", "--skip-git-repo-check", "--ignore-user-config"]
            if not session:
                argv.append("--ephemeral")
            argv += ["--sandbox", "read-only", "-m", model]
        argv += [
            "-c", f"model_reasoning_effort={effort}",
            # Reasoning summaries in the JSON stream, for the viewer's live Thinking panel.
            "-c", "model_reasoning_summary=detailed",
            "-c", "project_doc_max_bytes=0",
            "-c", "web_search=disabled",
        ]
        for feature in CODEX_DISABLED_FEATURES:
            argv += ["--disable", feature]
        if session and session.get("mode") == "resume":
            argv += ["--json", "-o", str(last_message), str(session["id"]), "-"]
        else:
            argv += ["--json", "--color", "never", "-o", str(last_message), "-"]
        return argv, last_message
    if provider == "claude":
        settings = workdir / "claude-settings.json"
        if not settings.exists():
            settings.write_text(json.dumps({"disableAllHooks": True}), encoding="utf-8")
        display = os.environ.get("LLM_CLAUDE_THINKING_DISPLAY", CLAUDE_THINKING_DISPLAY).strip()
        argv = [
            binary, "-p",
            "--model", model,
            "--effort", effort,
            *(["--thinking-display", display] if display else []),
            "--tools", "",
            "--strict-mcp-config",
            "--no-session-persistence",
            "--setting-sources", "",
            "--settings", str(settings),
            "--system-prompt", system_prompt,
            "--output-format", "stream-json", "--verbose",
        ]
        if session:
            argv.remove("--no-session-persistence")
            argv += ["--resume" if session.get("mode") == "resume" else "--session-id", str(session["id"])]
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
# Linux has no Segoe UI Symbol: DejaVu Sans carries the chess glyphs (U+265A to U+265F).
BOARD_FONTS = (BOARD_FONT, Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
               Path("/usr/share/fonts/dejavu/DejaVuSans.ttf"), Path("/usr/share/fonts/TTF/DejaVuSans.ttf"))


def board_font_path() -> Path:
    for path in BOARD_FONTS:
        if path.is_file():
            return path
    raise ProviderError("no font with chess glyphs for the board image; set \"image\": false for this player")
GLYPHS = {chess.KING: "\u265a", chess.QUEEN: "\u265b", chess.ROOK: "\u265c", chess.BISHOP: "\u265d",
          chess.KNIGHT: "\u265e", chess.PAWN: "\u265f"}


def board_png(board: chess.Board) -> bytes:
    """A 560x560 PNG of the position: White at the bottom, coordinates on all sides, last move highlighted."""
    from PIL import Image, ImageDraw, ImageFont

    sq, margin = 64, 24
    size = sq * 8 + margin * 2
    image = Image.new("RGB", (size, size), (40, 40, 40))
    draw = ImageDraw.Draw(image)
    font = str(board_font_path())
    piece_font = ImageFont.truetype(font, 52)
    label_font = ImageFont.truetype(font, 16)
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
                 cap_seconds: float | None = None, nudge: str | None = None, image: bool = True,
                 tools_block: "player_tools_mod.ToolsBlock | None" = None) -> str:
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
        if tactics_facts_enabled():
            lines.append("Computed tactics facts (exact, from the rules - trust these over eyeballing the board):")
            lines.append(tactics_facts(board))
    if tools_block is not None:
        tool_text = player_tools_mod.render_block(tools_block, board, history)
        if tool_text:
            lines.append(tool_text)
    for index, reason in enumerate(rejections, start=1):
        lines.append(f"Attempt {index} was rejected: {reason}. Choose again.")
    lines.append(
        'Reply with ONLY one JSON object: {"move": "<SAN or UCI>", "comment": "<one or two short sentences>"}'
    )
    if show_legal and tactics_facts_enabled():
        lines.append("Verify your chosen move against the tactics facts above (never leave a piece hanging) before replying.")
    return "\n".join(lines)


# ---------------------------------------------------------------- cache-friendly prompts (forever tournament)
#
# Order of every request: [RULES_TEXT: static] + [memory: frozen for the game] + [game header + moves so far:
# append-only] + [position block: FEN, board, legal moves, clocks, at the very END]. Nothing that changes
# from move to move (clocks, budget, legal moves) may appear before the position block.


def san_moves_since(history: list[str], start: int) -> str:
    """SAN with move numbers for plies `start`.. of `history` (a black first move gets 'N...')."""
    board = chess.Board()
    parts = []
    for index, uci in enumerate(history):
        move = chess.Move.from_uci(uci)
        if index >= start:
            if board.turn == chess.WHITE:
                parts.append(f"{board.fullmove_number}.")
            elif index == start:
                parts.append(f"{board.fullmove_number}...")
            parts.append(board.san(move))
        board.push(move)
    return " ".join(parts)


def memory_section(context: dict) -> str:
    memory = (context.get("memory") or "").strip()
    return ("YOUR MEMORY (your own MEMORY.md from earlier games; frozen for this game)\n"
            + (memory if memory else "(empty: you have not written any memory yet)"))


def game_header_section(context: dict) -> str:
    return "GAME\n" + (context.get("header") or "").strip()


def position_block(board: chess.Board, go_args: dict, show_legal: bool, cap_seconds: float | None = None,
                   nudge: str | None = None, image: bool = False,
                   tools_text: str = "") -> str:
    """The only part of a turn that changes on every move. Always the last part of a request."""
    side = "White" if board.turn == chess.WHITE else "Black"
    own, opp = ("wtime", "btime") if board.turn == chess.WHITE else ("btime", "wtime")
    lines = [f"POSITION (move {board.fullmove_number}, {side} to move, you play {side})",
             f"FEN: {board.fen()}",
             "Board:",
             board_diagram(board)]
    if image:
        lines.append("An image of the same position is attached: White at the bottom, the last move highlighted.")
    if board.is_check():
        lines.append("You are in check.")
    if show_legal:
        legal = list(board.legal_moves)
        lines.append("Legal moves (SAN): " + " ".join(board.san(move) for move in legal))
        lines.append("Legal moves (UCI): " + " ".join(move.uci() for move in legal))
    if go_args.get(own) is not None:
        inc = go_args.get("winc" if side == "White" else "binc", 0) or 0
        lines.append(f"Clocks: you {fmt_clock(go_args.get(own))}, opponent {fmt_clock(go_args.get(opp))}, +{int(inc) // 1000}s per move.")
        budget = move_budget_seconds(board, go_args.get(own), inc)
        lines.append(f"Time budget for this move: about {budget:.0f} seconds of thinking.")
        if cap_seconds is not None:
            lines.append(f"If you are still thinking after about {cap_seconds:.0f} seconds, your thinking is stopped, "
                         "you are shown all of it, and you must give your move at once.")
    if nudge:
        lines.append(nudge)
    if tools_text:
        lines.append(tools_text)
    lines.append("Reply with ONLY the JSON object.")
    return "\n".join(lines)


def history_section(history: list[str], notes: list[dict] | None = None) -> str:
    text = "MOVES SO FAR\n" + (san_moves_since(history, 0) or "(none, the game starts now)")
    own = [n for n in (notes or []) if n.get("note")]
    if own:
        text += "\nYOUR NOTES IN THIS GAME\n" + "\n".join(f"after ply {n.get('ply')}: {n['note']}" for n in own)
    return text


def summary_section(summary: dict | None) -> str:
    """The compacted thinking of this game: the player's own thoughts, summarised by a helper model."""
    if not summary or not (summary.get("text") or "").strip():
        return ""
    return (f"YOUR EARLIER THINKING IN THIS GAME (your own thoughts up to ply {summary.get('plies')}, summarised by a "
            "helper model because your conversation grew long; it is a summary of your thinking, not advice)\n"
            + summary["text"].strip())


def carried_thoughts_section(thoughts: str) -> str:
    """The model's own thinking on its previous move, carried into the next turn (routes that drop it)."""
    thoughts = (thoughts or "").strip()
    if not thoughts:
        return ""
    if len(thoughts) > CARRY_THOUGHTS_MAX_CHARS:
        thoughts = "[... start omitted ...]\n" + thoughts[-CARRY_THOUGHTS_MAX_CHARS:]
    return "YOUR OWN THINKING ON YOUR PREVIOUS MOVE (carried forward so you keep your reasoning)\n" + thoughts


def build_full_turn(context: dict, board: chess.Board, go_args: dict, history: list[str], show_legal: bool = True,
                    cap_seconds: float | None = None, nudge: str | None = None, image: bool = False,
                    notes: list[dict] | None = None, summary: dict | None = None, tools_text: str = "") -> str:
    """The whole request text for a fresh session: rules, memory, game header, moves so far, the summary of
    this game's earlier thinking (after a compaction), position."""
    parts = [RULES_TEXT, memory_section(context), game_header_section(context), history_section(history, notes)]
    compacted = summary_section(summary)
    if compacted:
        parts.append(compacted)
    parts.append(position_block(board, go_args, show_legal, cap_seconds, nudge, image, tools_text))
    return "\n\n".join(parts)


def build_delta_turn(board: chess.Board, go_args: dict, history: list[str], seen_plies: int, show_legal: bool = True,
                     cap_seconds: float | None = None, nudge: str | None = None, image: bool = False,
                     carried: str = "", tools_text: str = "") -> str:
    """A later turn of the same session: the carried thinking (if any), the moves since the last turn, the position."""
    since = san_moves_since(history, seen_plies)
    head = f"MOVES SINCE YOUR LAST TURN\n{since}" if since else "MOVES SINCE YOUR LAST TURN\n(none)"
    carried_text = carried_thoughts_section(carried)
    if carried_text:
        head = carried_text + "\n\n" + head
    return head + "\n\n" + position_block(board, go_args, show_legal, cap_seconds, nudge, image, tools_text)


def compact_prompt(header: str, history: list[str], turns: list[dict], previous: dict | None) -> str:
    """The compactor's request: the game so far plus the player's own thinking and replies, newest kept."""
    parts = [f"GAME\n{(header or '').strip() or '(no header)'}",
             "MOVES SO FAR\n" + (san_moves_since(history, 0) or "(none)")]
    if previous and (previous.get("text") or "").strip():
        parts.append(f"YOUR SUMMARY FROM AN EARLIER COMPACTION (up to ply {previous.get('plies')})\n{previous['text'].strip()}")
    blocks = []
    for turn in turns:
        thinking = (turn.get("thinking") or "").strip()
        if len(thinking) > COMPACT_THOUGHTS_PER_MOVE_CHARS:
            thinking = "[... start omitted ...]\n" + thinking[-COMPACT_THOUGHTS_PER_MOVE_CHARS:]
        lines = [f"--- the player's move at ply {turn.get('ply')}: {turn.get('san')} ---",
                 "thinking: " + (thinking or "(not visible)")]
        if turn.get("comment"):
            lines.append("comment: " + str(turn["comment"]))
        if turn.get("note"):
            lines.append("note to itself: " + str(turn["note"]))
        blocks.append("\n".join(lines))
    body = "\n\n".join(blocks)
    if len(body) > COMPACT_INPUT_MAX_CHARS:
        body = "[... older moves omitted ...]\n" + body[-COMPACT_INPUT_MAX_CHARS:]
    parts.append("THE PLAYER'S OWN THINKING AND REPLIES, MOVE BY MOVE (oldest first)\n" + (body or "(none yet)"))
    parts.append(f"Write the summary for the player in at most {COMPACT_SUMMARY_WORDS} words.")
    return "\n\n".join(parts)


def parse_note(text: str) -> str:
    """The optional "note" of a move reply, whitespace folded, at most NOTE_MAX_CHARS characters."""
    match = re.search(r"\{.*\}", text or "", flags=re.S)
    if not match:
        return ""
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return ""
    note = data.get("note") if isinstance(data, dict) else None
    if not isinstance(note, str):
        return ""
    return " ".join(note.split())[:NOTE_MAX_CHARS]


def limit_error(text: str) -> bool:
    """A usage or rate limit (wait and ask again), as opposed to a bad answer or a lost login."""
    lowered = (text or "").lower()
    return any(marker in lowered for marker in LIMIT_MARKERS) or bool(LIMIT_429.search(text or ""))


def limit_reset_epoch(text: str) -> float | None:
    """A reset time named in a limit error: Claude's `usage limit reached|<epoch>` or an ISO timestamp
    (Alibaba Throttling.AllocationQuota, OpenCode Go resetsAt)."""
    match = CLAUDE_RESET.search(text or "")
    if match:
        return float(match.group(1))
    iso = ISO_TIME.search(text or "")
    if iso:
        import datetime as _dt
        try:
            stamp = _dt.datetime.fromisoformat(iso.group(0).replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=_dt.timezone.utc)
            return stamp.timestamp()
        except ValueError:
            return None
    return None


def limit_wait_seconds(text: str, attempt: int, now: float | None = None) -> float:
    """Seconds to wait before asking again: the reset time when the error states it, else 60 s doubling to 15 min."""
    until = limit_reset_epoch(text)
    if until is not None:
        left = until - (time.time() if now is None else now)
        if 0 < left < 7 * 24 * 3600:
            return min(max(left + 5, LIMIT_WAIT_FIRST_SECONDS), 6 * 3600)
    return min(LIMIT_WAIT_MAX_SECONDS, LIMIT_WAIT_FIRST_SECONDS * (2 ** max(0, attempt)))


def next_check_label(seconds: float, now: float | None = None) -> str:
    """When the next check happens, as `22:25 SAST (20:25 UTC)` (SAST = UTC+2, no DST)."""
    import datetime as _dt
    at = _dt.datetime.fromtimestamp((time.time() if now is None else now) + seconds, _dt.timezone.utc)
    sast = at + _dt.timedelta(hours=2)
    return f"{sast:%H:%M} SAST ({at:%H:%M} UTC)"


def http_usage(usage: dict) -> dict:
    """OpenAI-compatible usage -> {input, cached, output}. DeepSeek reports prompt_cache_hit_tokens.
    Anthropic wire (no prompt_tokens): input_tokens is only the uncached part, so the total adds the cache
    reads and writes."""
    usage = usage or {}
    if "prompt_tokens" not in usage and ("cache_read_input_tokens" in usage or "cache_creation_input_tokens" in usage):
        read = int(usage.get("cache_read_input_tokens") or 0)
        write = int(usage.get("cache_creation_input_tokens") or 0)
        return {"input": int(usage.get("input_tokens") or 0) + read + write, "cached": read,
                "output": int(usage.get("output_tokens") or 0)}
    total = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
    cached = details.get("cached_tokens") if isinstance(details, dict) else None
    if cached is None:
        cached = usage.get("prompt_cache_hit_tokens")
    if cached is None:
        cached = usage.get("cached_tokens") or usage.get("cache_read_input_tokens") or 0
    return {"input": total, "cached": int(cached or 0), "output": int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)}


def claude_usage(output: str) -> dict | None:
    """Claude result usage -> {input, cached, output}: input counts uncached + cache writes + cache reads."""
    data = claude_result_event(output) or {}
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else None
    if not usage:
        return None
    read = int(usage.get("cache_read_input_tokens") or 0)
    write = int(usage.get("cache_creation_input_tokens") or 0)
    plain = int(usage.get("input_tokens") or 0)
    return {"input": plain + write + read, "cached": read, "output": int(usage.get("output_tokens") or 0),
            "cache_write": write}


def codex_usage(output: str) -> dict | None:
    """The last turn.completed usage of `codex exec --json` (cumulative for a resumed session)."""
    found = None
    for line in (output or "").splitlines():
        line = line.strip()
        if '"turn.completed"' not in line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        usage = event.get("usage") if isinstance(event, dict) else None
        if isinstance(usage, dict):
            found = {"input": int(usage.get("input_tokens") or 0), "cached": int(usage.get("cached_input_tokens") or 0),
                     "output": int(usage.get("output_tokens") or 0)}
    return found


def codex_thread_id(output: str) -> str | None:
    for line in (output or "").splitlines():
        if '"thread.started"' in line:
            try:
                return json.loads(line.strip()).get("thread_id")
            except (json.JSONDecodeError, AttributeError):
                continue
    return None


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
        # A reply cut off after the move was written ('{"move": "Bxc5", "comment": "I recap...') still states it.
        cut = re.search(r'"move"\s*:\s*"([A-Za-z0-9=+#\-]{2,8})"', text)
        if cut:
            raw_move = cut.group(1)
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


PIECE_VALUES = {chess.PAWN: 100, chess.KNIGHT: 300, chess.BISHOP: 300,
                chess.ROOK: 500, chess.QUEEN: 900, chess.KING: 0}
PIECE_LETTERS = {chess.PAWN: "P", chess.KNIGHT: "N", chess.BISHOP: "B",
                 chess.ROOK: "R", chess.QUEEN: "Q", chess.KING: "K"}


def tactics_facts_enabled() -> bool:
    """Harness-computed tactics facts for the move prompt. Kill switch: LLM_TACTICS_FACTS=0."""
    return os.environ.get("LLM_TACTICS_FACTS", "true").strip().lower() not in {"0", "false", "no", "off"}


def _piece_tag(piece: chess.Piece | None) -> str:
    if piece is None:
        return "-"
    return ("w" if piece.color == chess.WHITE else "b") + PIECE_LETTERS[piece.piece_type]


def _square_pressure(board: chess.Board, square: chess.Square, victim_color: chess.Color) -> tuple[int, int, str, str]:
    """(attackers, defenders, lowest-attacker tag, lowest-defender tag) for a square."""
    attackers = [board.piece_at(s) for s in board.attackers(not victim_color, square)]
    defenders = [board.piece_at(s) for s in board.attackers(victim_color, square)]
    attackers = [p for p in attackers if p is not None]
    defenders = [p for p in defenders if p is not None]
    low_att = min(attackers, key=lambda p: PIECE_VALUES[p.piece_type]) if attackers else None
    low_def = min(defenders, key=lambda p: PIECE_VALUES[p.piece_type]) if defenders else None
    return len(attackers), len(defenders), _piece_tag(low_att), _piece_tag(low_def)


def tactics_facts(board: chess.Board) -> str:
    """Computed, exact position facts: hanging pieces plus per-legal-move safety.

    Facts only - no scores, no marks, no advice. The model verifies its reply
    against these instead of eyeballing the FEN. Cheap 1-ply reads; a full
    static exchange evaluation is deliberately out of scope.
    """
    own = board.turn
    hanging, contested, quiet_hanging_pawns = [], [], 0
    for square in chess.SQUARES:
        piece = board.piece_at(square)
        if piece is None or piece.color != own or piece.piece_type == chess.KING:
            continue
        n_att, n_def, low_att, low_def = _square_pressure(board, square, own)
        if not n_att:
            continue
        name = f"{_piece_tag(piece)}{chess.square_name(square)}"
        detail = f"{name} attacked by {n_att} (lowest {low_att}), defended by {n_def} (lowest {low_def})"
        if n_def == 0:
            if piece.piece_type == chess.PAWN:
                quiet_hanging_pawns += 1
            else:
                hanging.append(detail + " -> HANGING")
        elif PIECE_VALUES[piece.piece_type] >= 300:
            contested.append(detail)
    move_lines = []
    for move in board.legal_moves:
        san = board.san(move)
        captured = board.piece_at(move.to_square)
        if board.is_en_passant(move):
            captured = chess.Piece(chess.PAWN, not own)
        check = board.gives_check(move)
        board.push(move)
        n_att, n_def, low_att, low_def = _square_pressure(board, move.to_square, own)
        board.pop()
        bits = []
        if captured is not None:
            bits.append(f"takes {_piece_tag(captured)}")
        if check:
            bits.append("gives check")
        bits.append(f"lands attacked by {n_att} (lowest {low_att}), defended by {n_def} (lowest {low_def})")
        moved = board.piece_at(move.from_square)
        if moved is not None and moved.piece_type != chess.KING and n_att and not n_def:
            bits.append("-> HANGS")
        move_lines.append(f"{san} ({move.uci()}): " + ", ".join(bits))
    lines = []
    if hanging:
        lines.append("Hanging pieces (attacked, undefended - capturing them is free):")
        lines.extend(f"  {h}" for h in hanging)
    if contested:
        lines.append("Attacked but defended (check the lowest values before trading):")
        lines.extend(f"  {c}" for c in contested[:12])
        if len(contested) > 12:
            lines.append(f"  ... plus {len(contested) - 12} more contested pieces")
    if quiet_hanging_pawns:
        lines.append(f"  ... plus {quiet_hanging_pawns} hanging pawn(s)")
    lines.append(f"Legal-move landing safety ({len(move_lines)} moves):")
    lines.extend(f"  {m}" for m in move_lines)
    return "\n".join(lines)


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
        # The agent's own verification tools: a folder it writes into between games. Empty = none.
        tools_env = os.environ.get("LLM_TOOLS_DIR", "").strip()
        self.tools_dir: Path | None = Path(tools_env) if tools_env else None
        self._tools_cache: tuple[str, str] = ("", "")   # (fen, rendered text) for this move
        self.invalid_model_moves = 0
        self.last_report: dict = {}
        self.runner: Callable[[list[str], str, int, Path | None], str] = self._run_cli
        self.http_post: Callable[[str, dict, dict, int], dict] = self._http_post
        self.http_stream: Callable[[str, dict, dict, int, float | None], dict] = self._http_stream
        self._cutoff_s: float | None = None
        self._board: chess.Board | None = None
        self._left_ms: int | None = None
        self._cli_cut_s: float | None = None
        self._cli_thoughts: list[str] = []
        self._image: bytes | None = None
        # Live thinking for the viewer: the runner names one file per move (UCI option ThinkingFile).
        self.thinking_file: Path | None = None
        self._think_handle = None
        self._think_lock = threading.Lock()
        self._image_board: str | None = None
        self._nudge: str | None = None
        self._infra_ms = 0
        self.on_clock_start: Callable[[int], None] | None = None
        self.session_id = str(uuid.uuid4())
        self.usage_log: list[dict] = []
        self._line_times: list[tuple[float, str]] = []
        self._attempt_think_ms: int | None = None
        # Forever tournament: the game context (frozen memory + game header) turns on the cache-friendly
        # prompt; conversation mode keeps one provider session per game; limit wait never forfeits a limit.
        self.context: dict | None = None
        self.conversation = _flag_env("LLM_CONVERSATION", False)
        self.limit_wait = _flag_env("LLM_LIMIT_WAIT", False)
        self.on_limit_wait: Callable[[int, str], None] | None = None
        self._conv: dict | None = None
        self._notes: list[dict] = []
        self._move_usage: list[dict] = []
        self._oneshot = False
        self._system_prompt = SYSTEM_PROMPT
        # Owner 2026-10-16: no move cap (think until the move is made; only the chess clock limits a move),
        # keep the reasoning across moves, compaction by a helper model when a game's conversation grows long.
        self.move_cap = _flag_env("LLM_MOVE_CAP", True)
        self.keep_reasoning = _flag_env("LLM_KEEP_REASONING", False)
        self.loop_guard = _flag_env("LLM_LOOP_GUARD", True)
        self.max_output_tokens = _int_env("LLM_MAX_OUTPUT_TOKENS", HTTP_MAX_TOKENS, 1000, 1_000_000)
        self.compact_at = _int_env("LLM_COMPACT_AT_TOKENS", 0, 0, 10_000_000)  # 0 = no compaction
        self.compact_provider = (os.environ.get("LLM_COMPACT_PROVIDER") or "").strip()
        self.compact_model = (os.environ.get("LLM_COMPACT_MODEL") or "").strip()
        self.compact_effort = (os.environ.get("LLM_COMPACT_EFFORT") or "high").strip()
        self.on_infra_wait: Callable[[int, str], None] | None = None
        self.compactor_factory: Callable[[], "SubscriptionChessClient"] | None = None
        self._game_turns: list[dict] = []      # this game's turns since the last compaction (for the compactor)
        self._summary: dict | None = None       # the latest compaction summary of this game
        self._context_tokens = 0               # input tokens of the last call in this game's session
        self._compact_thread: threading.Thread | None = None
        self._compact_result: dict | None = None
        self._compact_failures = 0
        self._last_reasoning = ""              # HTTP routes: the visible reasoning of the last answered call

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
        elif lowered == "thinkingfile":
            self._think_close()
            self.thinking_file = Path(value.strip()) if value.strip() else None
        elif lowered in {"showlegalmoves", "show_legal_moves"}:
            self.show_legal = value.strip().lower() in {"1", "true", "yes", "on"}
        elif lowered == "gamecontextfile":
            self.load_context(value.strip())
        elif lowered == "conversation":
            self.conversation = value.strip().lower() in {"1", "true", "yes", "on"}
        elif lowered == "limitwait":
            self.limit_wait = value.strip().lower() in {"1", "true", "yes", "on"}
        elif lowered == "movecap":
            self.move_cap = value.strip().lower() in {"1", "true", "yes", "on"}
        elif lowered == "keepreasoning":
            self.keep_reasoning = value.strip().lower() in {"1", "true", "yes", "on"}

    def load_context(self, path: str) -> None:
        """Read the game context once (frozen for the whole game): {"memory": str, "header": str}."""
        if not path:
            self.context = None
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self.log(f"game context {path!r} unreadable: {exc}")
            return
        self.context = {"memory": str(data.get("memory") or ""), "header": str(data.get("header") or "")}
        self.log(f"game context loaded: memory {len(self.context['memory'])} chars, header {len(self.context['header'])} chars")

    def new_game(self) -> None:
        self.invalid_model_moves = 0
        self._nudge = None
        self._tools_cache = ("", "")
        self._wait_compaction(announce=False)
        self.end_session()
        self.context = None
        self._notes = []
        self._game_turns = []
        self._summary = None
        self._compact_result = None
        self._compact_failures = 0

    def new_conversation(self) -> dict:
        self._conv = {"id": None, "started": False, "seen": 0, "messages": [], "committed": 0, "codex_total": None}
        self._context_tokens = 0
        return self._conv

    # ----- compaction -------------------------------------------------------------------------------------------

    def _make_compactor(self) -> "SubscriptionChessClient":
        if self.compactor_factory is not None:
            return self.compactor_factory()
        helper = SubscriptionChessClient(self.compact_provider, self.log)
        helper.model = self.compact_model or DEFAULT_MODELS.get(self.compact_provider, helper.model)
        helper.effort = self.compact_effort
        helper.max_output_tokens = COMPACT_MAX_OUTPUT_TOKENS
        helper.limit_wait = False   # a compaction that cannot run now is tried again after the next move
        helper.conversation = False
        helper.board_image = False
        helper.move_cap = False
        helper.compact_at = 0
        return helper

    def _maybe_start_compaction(self, history: list[str]) -> None:
        """After a move: the session is past the threshold, so summarise this game's thinking in the background
        (the opponent's thinking time). The next move starts a fresh session with the summary."""
        if (not self.compact_at or self._conv is None or not self.compact_provider
                or self._context_tokens < self.compact_at or self._compact_thread is not None
                or self._compact_failures >= COMPACT_GIVE_UP_AFTER):
            return
        header = (self.context or {}).get("header", "")
        turns = [dict(t) for t in self._game_turns]
        previous = dict(self._summary) if self._summary else None
        plies = len(history)
        prompt = compact_prompt(header, list(history), turns, previous)
        context_tokens = self._context_tokens
        self.log(f"{self.provider} {self.model} COMPACT start: session at {context_tokens} input tokens "
                 f"(threshold {self.compact_at}), {len(turns)} turns, {len(prompt)} chars to "
                 f"{self.compact_provider} {self.compact_model}")

        def run() -> None:
            started = time.monotonic()
            try:
                helper = self._make_compactor()
                text = helper.ask_text(COMPACT_SYSTEM, prompt, timeout=COMPACT_TIMEOUT_SECONDS,
                                       effort=self.compact_effort)
                # Plain hyphens only (owner rule: no long dashes anywhere the harness writes).
                text = (text or "").replace(chr(0x2014), " - ").replace(chr(0x2013), "-").strip()
                if not text:
                    raise ProviderError("the compactor returned an empty summary")
                self._compact_result = {"text": text, "plies": plies, "secs": round(time.monotonic() - started, 1),
                                        "from_tokens": context_tokens, "usage": helper.last_report.get("usage")}
            except Exception as exc:  # never breaks the game: the session simply stays as it is
                self._compact_result = {"error": f"{type(exc).__name__}: {str(exc)[:300]}",
                                        "secs": round(time.monotonic() - started, 1)}

        self._compact_result = None
        self._compact_thread = threading.Thread(target=run, daemon=True, name="compaction")
        self._compact_thread.start()

    def _wait_compaction(self, announce: bool = True) -> None:
        thread = self._compact_thread
        if thread is None:
            return
        if thread.is_alive():
            if announce and self.on_infra_wait is not None:
                # The runner moves its own deadline out by this much: the wait is never a flag.
                try:
                    self.on_infra_wait(COMPACT_TIMEOUT_SECONDS, "compaction of this game's conversation")
                except Exception:
                    pass
            self._think("\n[waiting for the summary of this game's earlier thinking; the clock is stopped]\n")
            thread.join(COMPACT_TIMEOUT_SECONDS + 30)
        self._compact_thread = None

    def _apply_compaction(self) -> None:
        """Before a move: finish a running compaction and, if it worked, start a fresh session with the summary."""
        if self._compact_thread is None:
            return
        self._wait_compaction()
        result, self._compact_result = self._compact_result, None
        if not result:
            return
        if result.get("error"):
            self._compact_failures += 1
            self.log(f"{self.provider} {self.model} COMPACT failed after {result.get('secs')}s "
                     f"({self._compact_failures}/{COMPACT_GIVE_UP_AFTER}): {result['error']}")
            if self._compact_failures >= COMPACT_GIVE_UP_AFTER and self._conv is not None:
                # The session would only keep growing: go on in a fresh session without a summary.
                self.log(f"{self.provider} {self.model} COMPACT gave up: fresh session without a summary")
                self.end_session()
                self.new_conversation()
                self._game_turns = []
            return
        self._summary = {"text": result["text"], "plies": result["plies"]}
        self._game_turns = []
        self._compact_failures = 0
        self.end_session()
        self.new_conversation()
        self.log(f"{self.provider} {self.model} COMPACT done in {result.get('secs')}s: {result.get('from_tokens')} "
                 f"input tokens -> fresh session with a {len(result['text'])}-char summary up to ply {result['plies']}")
        self.last_report["compacted"] = {"plies": result["plies"], "from_tokens": result.get("from_tokens"),
                                         "chars": len(result["text"]), "secs": result.get("secs")}

    def end_session(self) -> None:
        """Forget this game's provider session and delete its session file (CLI routes)."""
        conv, self._conv = self._conv, None
        if conv and self.provider in CLI_PROVIDERS:
            for sid in set(conv.get("ids") or []) | ({str(conv["id"])} if conv.get("id") else set()):
                for path in session_files(self.provider, sid):
                    try:
                        path.unlink()
                    except OSError:
                        pass
            cleanup_stale_sessions(self.provider)

    def choose_move(self, board: chess.Board, go_args: dict, history: list[str]) -> tuple[str, str]:
        self.last_report = {"tries": 0, "illegal": []}
        self._move_usage = []
        try:
            return self._choose_move(board, go_args, history)
        finally:
            if self._move_usage:
                self.last_report["usage"] = sum_usage(self._move_usage)

    def _turn_text(self, board: chess.Board, go_args: dict, history: list[str], rejections: list[str],
                   cap: float | None) -> str:
        """The request text for this attempt. The old single-prompt layout when no game context is set."""
        image = self.board_image and self._image is not None
        tools_text = self._tools_text(board, go_args, history)
        if self.context is None:
            return build_prompt(board, go_args, history, rejections, self.show_legal, cap, self._nudge, self.board_image,
                                tools_block=None)
        # Proof of read: the fingerprint of the memory that is inside this request (or its session).
        memory = self.context.get("memory") or ""
        self.last_report["memory"] = {"sha": hashlib.sha256(memory.encode("utf-8")).hexdigest()[:12],
                                      "bytes": len(memory.encode("utf-8"))}
        conv = self._conv if self.conversation else None
        if conv and conv.get("started"):
            if rejections and conv.get("turn_plies") == len(history):
                # Same move, previous reply rejected: the position is already in the session.
                return (f"Your last reply was rejected: {rejections[-1]}. Choose again from the legal moves above. "
                        "Reply with ONLY the JSON object.")
            # Routes that drop passed-back reasoning get their own previous thinking as text, once.
            carried = conv.pop("carry", "") if self.keep_reasoning else ""
            return build_delta_turn(board, go_args, history, conv.get("seen", 0), self.show_legal, cap, self._nudge,
                                    image, carried, tools_text)
        text = build_full_turn(self.context, board, go_args, history, self.show_legal, cap, self._nudge, image,
                               self._notes, self._summary, tools_text)
        for index, reason in enumerate(rejections, start=1):
            text += f"\nAttempt {index} was rejected: {reason}. Choose again."
        return text

    def _ask_waiting(self, prompt: str, timeout: int) -> str:
        """_ask_with_crash_retries, but a usage or rate limit waits and asks the same request again
        (LLM_LIMIT_WAIT=1). The wait is infrastructure time: never charged to the chess clock."""
        waits = 0
        while True:
            try:
                return self._ask_with_crash_retries(prompt, timeout)
            except ProviderError as exc:
                text = str(exc)
                if not self.limit_wait or not (limit_error(text) or provider_unavailable(text)):
                    raise
                seconds = limit_wait_seconds(text, waits)
                waits += 1
                reason = " ".join(text.split())[:160]
                check = next_check_label(seconds)
                self.log(f"{self.provider} {self.model} LIMIT WAIT {waits}: sleeping {seconds:.0f}s, next check at "
                         f"{check}, then the same request again ({reason})")
                if self.on_limit_wait is not None:
                    try:
                        self.on_limit_wait(int(seconds), reason)
                    except Exception:
                        pass
                self._think(f"\n[waiting {seconds:.0f}s for the {self.provider} usage limit to reset; next check at "
                            f"{check}; the clock is stopped]\n")
                slept = time.monotonic()
                self.sleep(seconds)
                self._infra_ms += int((time.monotonic() - slept) * 1000)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def _choose_move(self, board: chess.Board, go_args: dict, history: list[str]) -> tuple[str, str]:
        if not any(board.legal_moves):
            return "0000", "no legal moves"
        remaining = go_args.get("wtime") if board.turn == chess.WHITE else go_args.get("btime")
        if remaining is not None and remaining <= 0:
            return "0000", f"clock expired; forfeiting without calling {self.provider}"
        rejections: list[str] = []
        last_error = ""
        # A compaction that ran while the opponent thought: finish it before the turn text is built.
        self._apply_compaction()
        # Model thinking time for this move, summed over attempts. CLI start-up is not
        # thinking: claude reports its API time and codex brackets the turn with events.
        self.last_report["think_ms"] = 0
        # Visible thinking of the failed tries: the referee's BEST SO FAR move comes from here (rule 11).
        failed_thoughts: list[str] = []
        for attempt in range(1, self.max_attempts + 1):
            self.last_report["tries"] = attempt
            self._last_reasoning, self._cli_thoughts = "", []  # never a note from an earlier move
            left = None if remaining is None else remaining - self.last_report["think_ms"]
            if left is not None and left <= 0:
                # Out of clock: report the time used so the arbiter flags it; never a forfeit.
                return "0000", f"{self.provider} {self.model} ran out of clock while thinking"
            timeout = self._attempt_timeout(left)
            inc = go_args.get("winc" if board.turn == chess.WHITE else "binc", 0)
            # No move cap (owner 2026-10-16): the model thinks until it moves; only its chess clock limits it.
            self._cutoff_s = arbiter_cutoff_seconds(board, left, inc) if (left is not None and self.move_cap) else None
            self._left_ms = left
            self._board = board
            if not self.board_image:
                self._image = None
            elif self._image_board != board.fen():
                self._image = board_png(board)
                self._image_board = board.fen()
            # Only streaming routes show their thinking live, so only they get the BEST SO FAR cap.
            cap = self._cutoff_s if self.provider in HTTP_ROUTES else None
            if self.conversation and self.context is not None and self._conv is None:
                self.new_conversation()
            prompt = self._turn_text(board, go_args, history, rejections, cap)
            started = time.monotonic()
            self._attempt_think_ms = None
            self._infra_ms = 0
            try:
                text = self._ask_waiting(prompt, timeout)
                self._add_think(started)
                if self._conv is not None:
                    self._conv["turn_plies"] = len(history)
                    self._conv["seen"] = len(history)
                move, comment, _raw = parse_reply(text, board)
                note = parse_note(text)
                if note:
                    self.last_report["note"] = note
                    self._notes.append({"ply": len(history) + 1, "note": note})
                self.invalid_model_moves = 0
                usage = ""
                if self.provider in HTTP_ROUTES and self.usage_log:
                    usage = " usage=" + json.dumps(self.usage_log[-1], separators=(",", ":"))[:300]
                self.log(
                    f"{self.provider} {self.model} attempt {attempt}/{self.max_attempts} ok "
                    f"move={move.uci()} secs={time.monotonic() - started:.1f} think_ms={self.last_report['think_ms']}{usage}"
                )
                self._nudge = overrun_note(self.last_report["think_ms"], cap)
                self._record_turn(board, move, comment, note, history)
                self._think(f"\n[move] {board.san(move)}" + (f" - {comment}" if comment else "") + "\n")
                self._think_close()
                self._maybe_start_compaction(history + [move.uci()])
                return move.uci(), comment
            except ValueError as exc:
                self._add_think(started)
                last_error = str(exc)
                rejections.append(last_error)
                bad = re.search(r"'([^']{1,24})' is not a legal move", last_error)
                self.last_report["illegal"].append(marker_text(bad.group(1)) if bad else "invalid")
            except Exception as exc:  # timeouts and CLI failures spend an attempt too
                if self._conv is not None:
                    # The session may hold a half-written turn: the next attempt starts a fresh session with
                    # the whole game in its prompt (one cache miss, never a confused conversation).
                    self.end_session()
                    self.new_conversation()
                if isinstance(exc, ProviderError) and provider_unavailable(str(exc)):
                    self.log(f"{self.provider} {self.model} provider unavailable: {str(exc)[:300]}")
                    return "0000", f"provider unavailable: {str(exc)[:200]}"
                self._attempt_think_ms = None  # a timed-out or failed call is charged in full
                self._add_think(started)
                last_error = f"{type(exc).__name__}: {exc}"
                rejections.append("no answer arrived in time" if isinstance(exc, subprocess.TimeoutExpired) else "the reply failed")
                self.last_report["illegal"].append("timeout" if isinstance(exc, subprocess.TimeoutExpired) else "error")
            failed_thoughts.append(self._visible_thoughts())
            self.invalid_model_moves += 1
            self._think(f"\n[reply {attempt} rejected: {last_error[:160]}]\n")
            self.log(
                f"{self.provider} {self.model} attempt {attempt}/{self.max_attempts} rejected after "
                f"{time.monotonic() - started:.1f}s: {last_error[:400]} (invalid_count={self.invalid_model_moves})"
            )
        note = latest_note("\n".join(failed_thoughts), board)
        if note is not None:
            comment = "No valid reply came; the referee played its latest BEST SO FAR move."
            self.log(f"{self.provider} {self.model} failed after {self.max_attempts} attempts ({last_error[:200]}); "
                     f"the referee plays its latest BEST SO FAR move {note.uci()}")
            self.last_report["referee"] = "best so far"
            if self._conv is not None:
                self._conv["seen"] = len(history)
            self._record_turn(board, note, comment, "", history)
            self._think(f"\n[move] {board.san(note)} - {comment}\n")
            self._think_close()
            return note.uci(), comment
        return "0000", f"{self.provider} {self.model} failed after {self.max_attempts} attempts; forfeiting ({last_error[:200]})"

    def _visible_thoughts(self) -> str:
        if self.provider in HTTP_ROUTES:
            return self._last_reasoning or ""
        return "\n".join(t for t in self._cli_thoughts if t.strip())

    def _record_turn(self, board: chess.Board, move: chess.Move, comment: str, note: str, history: list[str]) -> None:
        """Keep this move's own thinking for the compactor, and (HTTP routes) for the next turn's text."""
        thoughts = self._visible_thoughts()
        if self.compact_at:
            self._game_turns.append({"ply": len(history) + 1, "san": board.san(move), "thinking": thoughts,
                                     "comment": comment, "note": note})
        if self.keep_reasoning and self._conv is not None and self.provider in HTTP_ROUTES and thoughts.strip():
            self._conv["carry"] = thoughts
        self.last_report["context_tokens"] = self._context_tokens

    def _think(self, text: str) -> None:
        """Append visible thinking to this move's file (what the model reveals; never fabricated)."""
        if not text or self.thinking_file is None:
            return
        with self._think_lock:
            try:
                if self._think_handle is None:
                    self.thinking_file.parent.mkdir(parents=True, exist_ok=True)
                    self._think_handle = open(self.thinking_file, "a", encoding="utf-8")
                self._think_handle.write(text)
                self._think_handle.flush()
            except OSError:
                pass

    def _think_close(self) -> None:
        with self._think_lock:
            if self._think_handle is not None:
                try:
                    self._think_handle.close()
                except OSError:
                    pass
                self._think_handle = None

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
        if not self.move_cap and remaining is not None:
            # No move cap: one reply may use the whole clock (running out is a loss on time, never a rejection).
            return max(10, int(remaining / 1000) + 5)
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
        started = time.monotonic()
        self._cli_cut_s = None if self._cutoff_s is None else CLI_CUT_SHARE * self._cutoff_s
        try:
            return self._ask_cli(prompt, timeout, self.effort)
        except (CliCut, ProviderError) as exc:
            if isinstance(exc, ProviderError) and not output_limit_error(str(exc)):
                raise
            limit = not isinstance(exc, CliCut) or isinstance(exc, OutputLimit)
            thoughts = "\n".join(t for t in self._cli_thoughts if t.strip())[-THOUGHTS_TAIL_CHARS:]
            self.last_report["hurried"] = self.last_report.get("hurried", 0) + 1
            why = "ran out of output tokens" if limit else "was stopped at the move cap"
            answer_effort = self.effort if not self.move_cap else "low"
            self._think(f"\n[the reply {why} - answering at {answer_effort} effort from its revealed thinking]\n")
            self.log(f"{self.provider} {self.model} {why}"
                     + ("" if limit or self._cli_cut_s is None else f" ({self._cli_cut_s:.0f}s)")
                     + f"; asking at {answer_effort} effort with {len(thoughts)} chars of revealed thinking")
            stop = ("Your reply ran out of output space before it gave a move."
                    if limit else "Your thinking on the move below was stopped at the move cap.")
            ask = ("Reply now with only the JSON object for your move, with no further long reasoning."
                   if limit else "Your time for this move is up. Reply now with only the JSON object for your move.")
            if self._conv is not None and self._conv.get("started"):
                # The killed turn may or may not be in the session: repeat the position, then the thinking.
                board = self._board or chess.Board()
                follow = (stop + "\n\n" + position_block(board, {}, self.show_legal)
                          + "\n\nYour thinking on this move so far:\n" + (thoughts or "(none visible)") + "\n" + ask)
            else:
                follow = (prompt + "\n\n" + stop + "\nYour thinking on this move so far:\n"
                          + (thoughts or "(none visible)") + "\n" + ask)
            self._cli_cut_s = None
            left = timeout - (time.monotonic() - started)
            answer = self._ask_cli(follow, max(10, int(left)), answer_effort)
            if thoughts:
                self._cli_thoughts = [thoughts] + self._cli_thoughts
            self._attempt_think_ms = None  # two calls: charge the wall time (infrastructure crashes still excluded)
            return answer

    def _cli_session(self) -> dict | None:
        conv = self._conv
        if self._oneshot or conv is None or (self.provider == "claude" and not self.keep_reasoning):
            # Without keepReasoning Claude keeps its game as content blocks in one fresh request (see
            # _claude_flat_prompt): its thinking is dropped. Measured 2026-10-08: `claude -p --resume` gave no cache
            # reads; measured again 2026-10-16 (Claude Code 2.1.296): a resumed session reads its prefix from cache
            # and its input grows by the previous turn's whole output, thinking included.
            return None
        if conv.get("started") and conv.get("id"):
            return {"mode": "resume", "id": conv["id"]}
        if self.provider == "claude":
            conv["id"] = str(uuid.uuid4())
            conv.setdefault("ids", []).append(conv["id"])
        return {"mode": "new", "id": conv.get("id")}

    def _ask_cli(self, prompt: str, timeout: int, effort: str) -> str:
        workdir = isolated_workdir()
        self._cli_thoughts = []
        session = self._cli_session()
        argv, last_message = build_command(self.provider, self.model, effort, resolve_binary(self.provider), workdir,
                                           self._system_prompt, session)
        image_file = None
        if self.provider == "codex":
            if not (session and session.get("mode") == "resume"):
                prompt = self._system_prompt + "\n\n" + prompt
            if self._image:
                image_file = workdir / f"board-{os.getpid()}-{time.time_ns()}.png"
                image_file.write_bytes(self._image)
                argv = argv[:-1] + ["-i", str(image_file), "--", "-"]
        elif self.provider == "claude" and self._conv is not None and not self._oneshot and not self.keep_reasoning:
            argv = argv + ["--input-format", "stream-json"]
            prompt = self._claude_flat_prompt(prompt)
        elif self.provider == "claude" and self._image:
            argv = argv + ["--input-format", "stream-json"]
            image = {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                 "data": base64.b64encode(self._image).decode("ascii")}}
            text = {"type": "text", "text": prompt}
            # With a game context the image belongs to the position block: after the text, never before it.
            content = [text, image] if self.context is not None else [image, text]
            prompt = json.dumps({"type": "user", "message": {"role": "user", "content": content}}) + "\n"
        try:
            self._line_times = []
            output = self.runner(argv, prompt, timeout, workdir)
            self._attempt_think_ms = cli_think_ms(self.provider, output, self._line_times)
            self._record_cli_usage(output, session)
            if self.provider == "codex":
                used = codex_tool_items(output)
                if used:
                    # A tool call is the model not playing the move itself: it spends an attempt.
                    raise ValueError(f"you used a tool ({', '.join(sorted(used))}); choose the move by reasoning only, with no code or tools")
                if last_message and last_message.exists():
                    answer = last_message.read_text(encoding="utf-8", errors="replace")
                    self._session_started(session, output)
                    return answer
                raise ProviderError(f"codex wrote no final message; output tail: {output[-300:]!r}")
            answer = claude_result_text(output)
            self._session_started(session, output)
            if self._conv is not None and not self._oneshot and not self.keep_reasoning and self._flat_turn is not None:
                # The turn and the reply become fixed blocks of the next request (never the thinking).
                self._conv.setdefault("blocks", []).extend([self._flat_turn, "YOUR REPLY\n" + answer.strip()])
                self._conv["started"] = True
                self._conv["id"] = self._conv.get("id") or self.session_id
            return answer
        finally:
            if last_message and last_message.exists():
                last_message.unlink(missing_ok=True)
            if image_file is not None:
                image_file.unlink(missing_ok=True)

    _flat_turn: str | None = None

    def _claude_flat_prompt(self, turn: str) -> str:
        """One fresh `claude -p` request that carries the whole game as content blocks: the first turn,
        then each later turn and reply as its own block, then this turn. The cache breakpoint sits on the
        last block, so the next request (same blocks plus two) reads everything up to this turn from cache.
        Four breakpoints at most: the CLI puts two on its system prompt and one on its closing system note."""
        self._flat_turn = turn
        blocks = [{"type": "text", "text": text} for text in (self._conv or {}).get("blocks", [])]
        blocks.append({"type": "text", "text": turn, "cache_control": {"type": "ephemeral", "ttl": "1h"}})
        if self._image:
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                       "data": base64.b64encode(self._image).decode("ascii")}})
        return json.dumps({"type": "user", "message": {"role": "user", "content": blocks}}) + "\n"

    def _session_started(self, session: dict | None, output: str) -> None:
        if session is None or self._conv is None:
            return
        if self.provider == "codex" and session.get("mode") == "new":
            self._conv["id"] = codex_thread_id(output) or self._conv.get("id")
        if self._conv.get("id"):
            self._conv["started"] = True

    def _record_cli_usage(self, output: str, session: dict | None) -> None:
        usage = claude_usage(output) if self.provider == "claude" else codex_usage(output)
        if not usage:
            return
        if self.provider == "codex" and session is not None and self._conv is not None:
            # A resumed codex session reports the session total: this call is the difference.
            total = dict(usage)
            before = self._conv.get("codex_total") if session.get("mode") == "resume" else None
            if before:
                usage = {key: max(0, total[key] - before.get(key, 0)) for key in ("input", "cached", "output")}
            self._conv["codex_total"] = total
        self._add_usage(usage)

    def _add_usage(self, usage: dict) -> None:
        entry = {"input": int(usage.get("input") or 0), "cached": int(usage.get("cached") or 0),
                 "output": int(usage.get("output") or 0), "oneshot": self._oneshot}
        self._move_usage.append(entry)
        if not self._oneshot:
            self._context_tokens = entry["input"]  # the session's size: what the compaction threshold watches
        self.log(f"{self.provider} {self.model} usage input={entry['input']} cached={entry['cached']} output={entry['output']}"
                 + (f" hit={entry['cached'] / entry['input']:.1%}" if entry["input"] else ""))

    def ask_text(self, system_prompt: str, prompt: str, timeout: int = 600, effort: str | None = None) -> str:
        """One fresh request on this route for a non-move task (the post-game memory reflection).
        Limit waits apply; no session, no image, no move cap. Usage goes to last_report["usage"]."""
        saved = (self._system_prompt, self._oneshot, self._image, self._cutoff_s, self._cli_cut_s, self.effort, self._board)
        self._system_prompt, self._oneshot, self._image, self._cutoff_s, self._cli_cut_s = system_prompt, True, None, None, None
        if effort:
            self.effort = effort
        self._move_usage = []
        self.last_report = {"tries": 1, "illegal": []}
        self._infra_ms = 0
        try:
            return self._ask_waiting(prompt, timeout)
        finally:
            if self._move_usage:
                self.last_report["usage"] = sum_usage(self._move_usage)
            (self._system_prompt, self._oneshot, self._image, self._cutoff_s, self._cli_cut_s, self.effort,
             self._board) = saved

    def _ask_http(self, prompt: str, timeout: int) -> str:
        route = HTTP_ROUTES[self.provider]
        api_key = ""
        for key_name in [route["key"]] + list(route.get("alt_keys") or []):
            api_key = os.environ.get(key_name) or _user_env(key_name)
            if api_key:
                break
        if not api_key:
            raise ProviderError(f"{route['key']} is not set")
        user: object = prompt
        if self._image and route["style"] == "anthropic":
            user = [{"type": "text", "text": prompt},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                 "data": base64.b64encode(self._image).decode("ascii")}}]
        elif self._image:
            user = [{"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(self._image).decode("ascii")}}]
        conv = None if self._oneshot else self._conv
        if conv is not None and conv.get("started"):
            # Client-side session: the earlier turns are a byte-identical prefix the provider caches.
            messages = list(conv["messages"]) + [{"role": "user", "content": user}]
        else:
            messages = [{"role": "system", "content": self._system_prompt}, {"role": "user", "content": user}]
        payload: dict = {
            "model": self.model,
            "messages": messages,
            # The model's real output limit from the config (LLM_MAX_OUTPUT_TOKENS), never more.
            "max_tokens": self.max_output_tokens,
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
        elif style == "anthropic":
            payload = anthropic_payload(self.model, messages, effort)
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "User-Agent": BROWSER_UA}
        if style == "anthropic":
            headers["anthropic-version"] = ANTHROPIC_VERSION
        if self.provider in ("opencode-go", "opencode-zen"):
            headers["x-opencode-session"] = self.session_id
        if self.provider == "openrouter-chat":
            headers["X-Title"] = "ai-chess-harness"
            headers["x-session-id"] = self.session_id
        started = time.monotonic()
        self._clock_start()
        think_cap = None if self._cutoff_s is None else THINK_SHARE * self._cutoff_s
        try:
            streamed = self.http_stream(route["url"], stream_payload(payload, style), headers, timeout, think_cap)
        except CliCrash as exc:
            # OpenCode Go sends GLM to more than one upstream; some refuse `thinking` (HTTP 400
            # "unknown field", 2026-10-16) while others take it. Ask again without it: never a lost move.
            if "thinking" not in payload or '"param":"thinking"' not in str(exc):
                raise
            payload.pop("thinking")
            self.log(f"{self.provider} {self.model}: this upstream refuses `thinking`; asking again without it")
            streamed = self.http_stream(route["url"], stream_payload(payload, style), headers, timeout, think_cap)
        self.usage_log.append(streamed.get("usage") or {})
        if streamed.get("usage"):
            self._add_usage(http_usage(streamed["usage"]))
        self._last_reasoning = streamed.get("reasoning") or ""
        content = streamed.get("content") or ""
        # Still thinking at the cap, or out of output tokens before any answer (DeepSeek, run 4).
        if not content.strip() and (streamed.get("cut") or streamed.get("finish") == "length"):
            content = self._answer_with_thoughts(route["url"], payload, headers, style, streamed, started)
        elif not content.strip():
            raise ValueError(f"the reply was empty (finish_reason={streamed.get('finish')}, "
                             f"reasoning_chars={len(streamed.get('reasoning') or '')})")
        if conv is not None:
            # Keep the turn and the final answer (never the reasoning) for the next request of this game.
            conv["messages"] = messages + [{"role": "assistant", "content": content}]
            conv["committed"] = len(conv["messages"])
            conv["started"] = True
            conv["id"] = conv.get("id") or self.session_id
        return content

    def _answer_with_thoughts(self, url: str, payload: dict, headers: dict, style: str, streamed: dict,
                              started: float) -> str:
        """The model's thinking was stopped: give it all of that thinking back as its own earlier turn and
        ask for the move. Same effort first; the lowest effort the route allows only if that brings no move."""
        thoughts = streamed.get("reasoning") or ""
        loop = streamed.get("loop")
        if loop:
            # Only the thinking before the repeats goes back, and on to the next turn: the repeats would invite more.
            thoughts = before_loop(thoughts, loop)
            self._last_reasoning = thoughts
        if len(thoughts) > THOUGHTS_HEAD_CHARS + THOUGHTS_TAIL_CHARS:
            thoughts = (thoughts[:THOUGHTS_HEAD_CHARS] + "\n[... middle of the thinking omitted for length ...]\n"
                        + thoughts[-THOUGHTS_TAIL_CHARS:])
        capped = self._cutoff_s is not None
        total = self._cutoff_s or 30.0
        why = (f"it repeated the same line {LOOP_REPEATS}+ times: \"{loop[:120]}\"" if loop
               else f"ran out of output space ({payload.get('max_tokens')} tokens)" if streamed.get("finish") == "length"
               else f"reached {THINK_SHARE * total:.0f}s of its {total:.0f}s move cap")
        if loop:
            self.last_report["loops"] = self.last_report.get("loops", 0) + 1
            opening = "Your previous reply kept repeating the same line, so it was stopped. Your thinking up to that point is above. "
        else:
            opening = (("Your time for this move is up. " if capped else "Your previous reply ran out of output space. ")
                       + "Your full thinking so far is above. ")
        self.log(f"{self.provider} {self.model} thinking stopped after {time.monotonic() - started:.1f}s ({why}); "
                 f"returning its {len(thoughts)} chars of thinking and asking for the move")
        self.last_report["hurried"] = self.last_report.get("hurried", 0) + 1
        self._think(f"\n[thinking stopped: {why} - answering from its own thoughts]\n")
        messages = plain_messages(payload["messages"]) + [
            {"role": "assistant", "content": "My thinking on this move so far:\n" + (thoughts or "(no visible thinking)")},
            {"role": "user", "content": opening + "Reply now with only the JSON object for your move."}]
        limits = {"same": min(ANSWER_WITH_THOUGHTS_SECONDS, SAME_SHARE * total),
                  "lowest": min(ANSWER_WITH_THOUGHTS_SECONDS, max(LOWEST_MIN_SECONDS, LOWEST_SHARE * total))}
        clock_left = None if self._left_ms is None else self._left_ms / 1000 - (time.monotonic() - started)
        if not capped:
            # No move cap: the same-effort answer is not squeezed into seconds; only the clock limits it.
            limits["same"] = max(10.0, clock_left if clock_left is not None else 600.0)
        for level in (("same", "lowest") if capped else ("same",)):
            if style == "anthropic":
                follow = stream_payload({**payload, "messages": anthropic_messages(messages)}, style)
                if level == "lowest":
                    follow["thinking"] = {"type": "disabled"}
            else:
                follow = {**payload, "messages": messages, "max_tokens": ANSWER_MAX_TOKENS, "stream": True,
                          "stream_options": {"include_usage": True}}
            if level == "lowest" and style != "anthropic":
                if style == "openrouter":
                    follow["reasoning"] = {"effort": "low"}  # Grok and GLM refuse reasoning off on OpenRouter
                elif style == "effort":
                    follow.pop("reasoning_effort", None)
                    follow["thinking"] = {"type": "disabled"}
            if level == "same":
                answer = self.http_stream(url, follow, headers, int(limits[level]) + 10, limits[level] if capped else None)
            else:
                # No share limit: wait for the answer until the clock is gone (then it is a loss on time).
                left = 600.0 if clock_left is None else max(5.0, clock_left - (time.monotonic() - started))
                answer = self.http_stream(url, follow, headers, int(left) + 5, None)
            self.usage_log.append(answer.get("usage") or {})
            if answer.get("usage"):
                self._add_usage(http_usage(answer["usage"]))
            if answer.get("reasoning"):
                self._last_reasoning = (self._last_reasoning + "\n" + answer["reasoning"]).strip()
            if (answer.get("content") or "").strip():
                if level == "lowest":
                    self.log(f"{self.provider} {self.model} answered at the lowest effort after a "
                             f"{limits['same']:.0f}s answer at its own effort brought no move")
                return answer["content"]
        source = before_loop(streamed.get("reasoning") or "", loop) if loop else streamed.get("reasoning") or ""
        note = latest_note(source, self._board or chess.Board())
        if note is not None:
            return json.dumps({"move": note.uci(), "comment": "Its own last stated choice from its thinking."})
        raise ValueError("time was up and no move came back after its thinking was returned to it")

    def _http_stream(self, url: str, payload: dict, headers: dict, timeout: int, cutoff: float | None) -> dict:
        """Stream a chat completion. Past the cutoff, with no answer text yet, the stream is `cut` and the
        thinking so far is returned. No bytes for STALL_SECONDS = gateway stall (infrastructure)."""
        body = json.dumps(payload).encode("utf-8")
        state = {"content": "", "reasoning": "", "usage": {}, "finish": None, "done": False, "error": None,
                 "last": time.monotonic(), "resp": None}
        # Set when this call stops listening (cut, stall, timeout). A response that arrives later is closed at
        # once, and no later event is written: tournament #5 (2026-10-08) cut a stream before its headers came,
        # and it then streamed into the next thinking file and kept spending tokens nobody read.
        stopped = threading.Event()
        guard = LoopGuard() if self.loop_guard else None

        def watch(piece: str) -> None:
            """Stop listening once the reasoning loops (only while no answer text has come)."""
            if guard is None or not piece or state["content"].strip():
                return
            phrase = guard.feed(piece)
            if phrase:
                state["loop"] = phrase
                stopped.set()  # the reader leaves the stream at its next event, which disconnects

        def stop_listening() -> None:
            stopped.set()
            _close_quietly(state.get("resp"))
        # Observe raw provider bytes before parsing. Evidence never stops or changes a reply.
        from provider_wire_audit import ProviderWireAudit
        audit = (ProviderWireAudit(url, payload, self.thinking_file, self.log, session_id=self.session_id)
                 if self.thinking_file else None)

        def observe(method, *args) -> None:
            if audit is not None:
                try:
                    getattr(audit, method)(*args)
                except Exception:
                    pass   # diagnostic failures must never change the model's reply

        def reader() -> None:
            request = urllib.request.Request(url, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    state["resp"] = response
                    if stopped.is_set():
                        return  # the caller gave up before the headers came; leaving `with` disconnects
                    for raw in response:
                        if stopped.is_set():
                            break
                        observe("raw_event", raw)
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
                        kind = data.get("type") if isinstance(data, dict) else None
                        if data.get("usage") and not kind:
                            state["usage"] = data["usage"]
                        if kind:  # Anthropic Messages events
                            if kind == "message_start":
                                state["usage"] = dict((data.get("message") or {}).get("usage") or {})
                            elif kind == "message_delta":
                                state["usage"] = {**state["usage"], **{k: v for k, v in (data.get("usage") or {}).items() if v is not None}}
                                stop = (data.get("delta") or {}).get("stop_reason")
                                if stop:
                                    state["finish"] = "length" if stop == "max_tokens" else stop
                            elif kind == "content_block_delta":
                                delta = data.get("delta") or {}
                                piece = str(delta.get("thinking") or "")
                                observe("reasoning", piece)
                                state["reasoning"] += piece
                                if not stopped.is_set():
                                    self._think(piece)
                                watch(piece)
                                state["content"] += str(delta.get("text") or "")
                            elif kind == "error":
                                err = data.get("error") or {}
                                state["error"] = ("http", 529 if "overloaded" in str(err).lower() else 400,
                                                  json.dumps(err)[:400])
                            continue
                        for choice in data.get("choices") or []:
                            delta = choice.get("delta") or {}
                            piece = str(delta.get("reasoning_content") or delta.get("reasoning") or "")
                            observe("reasoning", piece)
                            state["reasoning"] += piece
                            if not stopped.is_set():
                                self._think(piece)
                            watch(piece)
                            state["content"] += str(delta.get("content") or "")
                            if choice.get("finish_reason"):
                                state["finish"] = choice["finish_reason"]
            except urllib.error.HTTPError as exc:
                state["error"] = ("http", exc.code, exc.read().decode("utf-8", errors="replace")[:400])
            except Exception as exc:  # socket errors; ignored once the stream was cut on purpose
                state["error"] = ("exc", exc)
            finally:
                observe("finish", state)
                state["done"] = True

        worker = threading.Thread(target=reader, daemon=True)
        started = time.monotonic()
        worker.start()
        while not state["done"]:
            now = time.monotonic()
            if state.get("loop"):
                stop_listening()
                return {**state, "cut": True}
            if cutoff is not None and now - started > cutoff and not state["content"].strip():
                stop_listening()
                return {**state, "cut": True}
            if now - state["last"] > STALL_SECONDS:
                stop_listening()
                raise CliCrash(f"stream stalled: no bytes for {STALL_SECONDS}s")
            if now - started > timeout + 2:
                stop_listening()
                raise subprocess.TimeoutExpired(url, timeout)
            time.sleep(0.2)
        if state.get("loop"):
            return {**state, "cut": True}
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

    def _tools_text(self, board: chess.Board, go_args: dict, history: list[str]) -> str:
        """Run the agent's own tools on this position and render the output for the prompt.

        Once per position: a retry of the same move reuses the same output, so a tool
        never runs twice for one decision and never eats the clock twice.
        """
        if not self.tools_dir:
            return ""
        fen = board.fen()
        if self._tools_cache[0] == fen:
            return self._tools_cache[1]
        try:
            block = player_tools_mod.run_tools(self.tools_dir, board, history, go_args)
            text = player_tools_mod.render_block(block, board, history)
            self.last_report["tools"] = [{"name": r.name, "ok": r.ok, "ms": r.elapsed_ms,
                                          "error": r.error[:120]} for r in block.results]
        except Exception as exc:  # a broken tool folder must never cost the game
            self.log(f"tools: {type(exc).__name__}: {exc}")
            text = ""
        self._tools_cache = (fen, text)
        return text

    def _run_cli(self, argv: list[str], prompt: str, timeout: int, workdir: Path | None) -> str:
        env = os.environ.copy()
        for key in STRIPPED_ENV[self.provider]:
            env.pop(key, None)
        if self.provider == "claude" and os.environ.get("LLM_MAX_OUTPUT_TOKENS"):
            # The CLI's own output ceiling (it errors past it): the model's real output limit from the config.
            env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(self.max_output_tokens)
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
            start_new_session=os.name != "nt",
        )
        # Stream stdout with arrival times so codex turn events can bracket the thinking time.
        lines: list[str] = []
        times = self._line_times = []

        started_signal = False
        thinking_since = [None]

        def pump() -> None:
            nonlocal started_signal
            assert proc.stdout is not None
            for line in proc.stdout:
                times.append((time.monotonic(), line))
                lines.append(line)
                visible = cli_visible_thinking(line)
                if visible:
                    self._cli_thoughts.append(visible)
                    self._think(visible + "\n")
                # The model starts thinking here (CLI start-up is over): tell the viewer clock.
                if not started_signal and ('"turn.started"' in line or '"subtype":"init"' in line):
                    started_signal = True
                    thinking_since[0] = time.monotonic()
                    self._clock_start()

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        try:
            assert proc.stdin is not None
            proc.stdin.write(prompt)
            proc.stdin.close()
            deadline = time.monotonic() + timeout
            while proc.poll() is None:
                now = time.monotonic()
                if now > deadline:
                    raise subprocess.TimeoutExpired(argv[0], timeout)
                cut = self._cli_cut_s
                if cut is not None and thinking_since[0] is not None and now - thinking_since[0] > cut:
                    kill_tree(proc)
                    raise CliCut()
                time.sleep(0.2)
        except subprocess.TimeoutExpired:
            kill_tree(proc)
            raise
        reader.join(10)
        output = "".join(lines)
        if proc.returncode != 0:
            if not output.strip():
                raise CliCrash(f"{self.provider} exited {proc.returncode} with no output")
            raise ProviderError(f"{self.provider} exited {proc.returncode}: {cli_error_text(output)!r}")
        return output


def cli_visible_thinking(line: str) -> str:
    """Thinking a CLI reveals in its JSON stream: Codex reasoning summaries, Claude thinking blocks."""
    line = line.strip()
    if not line.startswith("{") or ("reasoning" not in line and "thinking" not in line):
        return ""
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return ""
    item = event.get("item") if isinstance(event.get("item"), dict) else None
    if item and item.get("type") == "reasoning" and event.get("type") == "item.completed":
        return str(item.get("text") or "")
    message = event.get("message") if event.get("type") == "assistant" else None
    if isinstance(message, dict):
        parts = [str(block.get("thinking") or "") for block in message.get("content") or []
                 if isinstance(block, dict) and block.get("type") == "thinking"]
        return "\n".join(p for p in parts if p)
    return ""


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


class LoopGuard:
    """Watches streamed reasoning for one phrase repeated LOOP_REPEATS times in the newest LOOP_WINDOW_CHARS."""

    def __init__(self) -> None:
        self.tail = ""
        self.unchecked = 0

    def feed(self, piece: str) -> str | None:
        """The repeated phrase once the reasoning loops, else None."""
        self.tail = (self.tail + piece)[-LOOP_WINDOW_CHARS:]
        self.unchecked += len(piece)
        if self.unchecked < LOOP_CHECK_EVERY_CHARS:
            return None
        self.unchecked = 0
        words = self.tail.split()
        n = LOOP_PHRASE_WORDS
        counts: dict[tuple[str, ...], int] = {}
        for i in range(len(words) - n + 1):
            key = tuple(words[i:i + n])
            counts[key] = counts.get(key, 0) + 1
        phrase, repeats = max(counts.items(), key=lambda item: item[1], default=((), 0))
        return " ".join(phrase) if repeats >= LOOP_REPEATS else None


def before_loop(thoughts: str, phrase: str) -> str:
    """The thinking up to and including the first copy of the repeated phrase."""
    match = re.search(r"\s+".join(re.escape(word) for word in phrase.split()), thoughts)
    if match is None:
        return thoughts
    return thoughts[:match.end()] + "\n[... the same line then repeated many times; the repeats are left out ...]"


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


ENV_FILE = Path(os.environ.get("AI_CHESS_ENV_FILE") or Path.home() / ".config" / "ai-chess" / "env")


def _env_file_value(name: str, path: Path = ENV_FILE) -> str | None:
    """KEY=VALUE lines of the private env file (Linux VPS: ~/.config/ai-chess/env, mode 600)."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip().removeprefix("export ").strip() == name:
            return value.strip().strip('"').strip("'") or None
    return None


def _user_env(name: str) -> str | None:
    """Read a User/Machine scope variable a long-running shell may not have inherited
    (Windows registry), or the private env file on Linux."""
    if os.name != "nt":
        return _env_file_value(name)
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
        try:  # the CLI runs in its own process group (start_new_session): stop the whole group
            os.killpg(proc.pid, 9)
        except OSError:
            proc.kill()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def _flag_env(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def plain_messages(messages: list[dict]) -> list[dict]:
    """Messages without cache markers (what is kept between turns)."""
    out = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            content = [{k: v for k, v in block.items() if k != "cache_control"} if isinstance(block, dict) else block
                       for block in content]
            if len(content) == 1 and isinstance(content[0], dict) and content[0].get("type") == "text":
                content = content[0]["text"]
        out.append({**message, "content": content})
    return out


def anthropic_messages(messages: list[dict]) -> list[dict]:
    """Anthropic wire: no system role in the list, and one cache breakpoint on the last block of the last
    message, so the next request (same messages plus two) reads all of this one from cache."""
    msgs = [dict(m) for m in plain_messages(messages) if m.get("role") != "system"]
    if msgs:
        last = msgs[-1]
        content = last["content"]
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else [dict(b) for b in content]
        blocks[-1 if blocks[-1].get("type") == "text" else 0]["cache_control"] = {"type": "ephemeral"}
        last["content"] = blocks
    return msgs


def anthropic_payload(model: str, messages: list[dict], effort: str) -> dict:
    system = next((m["content"] for m in messages if m.get("role") == "system"), SYSTEM_PROMPT)
    budget = ANTHROPIC_THINKING_BUDGET.get(effort or "high", ANTHROPIC_THINKING_BUDGET["high"])
    payload = {"model": model, "max_tokens": HTTP_MAX_TOKENS,
               "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
               "messages": anthropic_messages(messages)}
    if effort not in ("none", "off", "minimal"):
        payload["thinking"] = {"type": "enabled", "budget_tokens": min(budget, HTTP_MAX_TOKENS // 2)}
    return payload


def stream_payload(payload: dict, style: str) -> dict:
    if style == "anthropic":
        return {**payload, "stream": True}
    return {**payload, "stream": True, "stream_options": {"include_usage": True}}


def sum_usage(entries: list[dict]) -> dict:
    """Totals for one move (or one reflection): calls, input, cached, output."""
    return {"calls": len(entries), "input": sum(e.get("input", 0) for e in entries),
            "cached": sum(e.get("cached", 0) for e in entries), "output": sum(e.get("output", 0) for e in entries)}


def cli_error_text(output: str) -> str:
    """The useful part of a failed CLI run: error events and result text first (they name a usage limit),
    then the output tail."""
    picked = []
    for line in (output or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") in {"error", "turn.failed"} or event.get("is_error"):
            detail = event.get("message") or event.get("error") or event.get("result") or ""
            picked.append(json.dumps(detail)[:300] if not isinstance(detail, str) else detail[:300])
    tail = (output or "")[-400:]
    return (" | ".join(picked) + " | " if picked else "") + tail


def session_files(provider: str, session_id: str) -> list[Path]:
    """Session files a CLI wrote for one of our game sessions (deleted when the game ends)."""
    if not session_id or not re.fullmatch(r"[A-Za-z0-9-]{8,80}", session_id):
        return []
    if provider == "claude":
        root = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"
        return list(root.glob(f"*/{session_id}.jsonl")) if root.is_dir() else []
    if provider == "codex":
        root = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "sessions"
        return list(root.glob(f"*/*/*/rollout-*{session_id}.jsonl")) if root.is_dir() else []
    return []


STALE_SESSION_SECONDS = 3600


def cleanup_stale_sessions(provider: str, now: float | None = None) -> int:
    """Delete session files our isolated working directory left behind (a CLI killed mid-turn), older than
    an hour. Only files whose recorded working directory is ours are touched, never other codex or claude work."""
    now = time.time() if now is None else now
    ours = str(isolated_workdir())
    removed = 0
    if provider == "claude":
        root = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"
        folder_tag = re.sub(r"[^A-Za-z0-9]", "-", ours)
        candidates = list((root / folder_tag).glob("*.jsonl")) if (root / folder_tag).is_dir() else []
        check_cwd = False
    elif provider == "codex":
        root = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "sessions"
        candidates = []
        for days in range(3):
            day = time.strftime("%Y/%m/%d", time.localtime(now - days * 86400))
            folder = root / day
            if folder.is_dir():
                candidates += list(folder.glob("rollout-*.jsonl"))
        check_cwd = True
    else:
        return 0
    for path in candidates:
        try:
            if now - path.stat().st_mtime < STALE_SESSION_SECONDS:
                continue
            if check_cwd:
                with open(path, encoding="utf-8", errors="replace") as handle:
                    first = handle.readline(20000)
                if json.dumps(ours)[1:-1] not in first:
                    continue
            path.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def _int_env(name: str, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(os.environ.get(name, default))))
    except (TypeError, ValueError):
        return default
