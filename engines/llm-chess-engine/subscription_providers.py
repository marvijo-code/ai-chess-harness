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
# The last step (lowest effort, own thinking returned) is NOT cut at its share: it runs until the model answers
# or its clock runs out. Run 6 (2026-10-06): a ~6 s hard limit there turned slow first tokens into "invalid
# replies" and forfeited Muse, Qwen, MiMo and Grok in round 1. Out of clock is a time loss, never invalid.
ANSWER_MAX_TOKENS = 16000
# CLI routes (Codex, Claude) have no same-effort answer step: they are stopped at 80% of the cap and asked
# again at low effort with whatever thinking they revealed (run 7: GPT averaged 30 s a move with no cap and flagged).
CLI_CUT_SHARE = THINK_SHARE + SAME_SHARE
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
# A subscription usage or rate limit (forever tournament, LLM_LIMIT_WAIT=1): the engine waits and asks the
# SAME move again once the limit resets. Never a forfeit, never a fallback move, never charged to the clock.
LIMIT_MARKERS = ("usagelimit", "usage limit", "usage_limit", "rate limit", "rate_limit", "ratelimit", "too many requests",
                 "hit your limit", "limit reached", "limit exceeded", "quota", "try again at", "try again in",
                 "overloaded", "capacity")
LIMIT_429 = re.compile(r"(?:http|status|code)[^0-9]{0,4}429\b|\b429 too many", re.I)
LIMIT_WAIT_FIRST_SECONDS = 60.0
LIMIT_WAIT_MAX_SECONDS = 900.0
CLAUDE_RESET = re.compile(r"usage limit reached\|(\d{9,11})", re.I)
NOTE_MAX_CHARS = 200
# Static rules for the cache-friendly prompt (forever tournament). Byte-identical for every player, game and
# move: nothing in it may depend on the player, the game, the clock or the time of day.
RULES_TEXT = """AI CHESS TOURNAMENT RULES (the same text for every player, every game and every move)
1. You play one game of chess against another AI player or against the Stockfish engine.
2. On every turn you get the moves played since your last turn, then the current position: the FEN, a board diagram (White pieces uppercase, Black lowercase, White plays up the board), the legal moves in SAN and in UCI, and both clocks.
3. Reply with ONLY one JSON object: {"move": "<one legal move, SAN or UCI>", "comment": "<one or two short sentences about your idea>", "note": "<optional private note to yourself, at most 200 characters>"}
4. The note is optional. Use it to remember a plan, a threat or a lesson from this game. Your notes are saved with the game, you see them again after the game when you update your memory, and they are public after the game.
5. Choose the move with your own reasoning. Do not run code, call tools, or use a chess engine.
6. A reply that is not the JSON object, or that names an illegal move, is rejected and you get another try with the reason. After the last allowed try (see the game header) you forfeit the game.
7. Your clock runs only while you think. If it reaches 0:00 you lose on time. You get the increment after every move you make.
8. A game is drawn by stalemate, insufficient material, threefold repetition, the 50-move rule, or at the game's ply cap.
9. Before you move, look at every check, capture and threat for both sides, and do not leave a piece undefended by accident.
10. Your memory below holds what you wrote after your earlier games. It does not change during a game; you update it after the game."""


class ProviderError(RuntimeError):
    pass


class CliCrash(ProviderError):
    """The CLI exited non-zero without producing an answer."""


class CliCut(Exception):
    """The CLI was still thinking at its move cap and was stopped (not an error, not a rejected reply)."""


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
        argv = [
            binary, "-p",
            "--model", model,
            "--effort", effort,
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
                   nudge: str | None = None, image: bool = False) -> str:
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
    lines.append("Reply with ONLY the JSON object.")
    return "\n".join(lines)


def history_section(history: list[str], notes: list[dict] | None = None) -> str:
    text = "MOVES SO FAR\n" + (san_moves_since(history, 0) or "(none, the game starts now)")
    own = [n for n in (notes or []) if n.get("note")]
    if own:
        text += "\nYOUR NOTES IN THIS GAME\n" + "\n".join(f"after ply {n.get('ply')}: {n['note']}" for n in own)
    return text


def build_full_turn(context: dict, board: chess.Board, go_args: dict, history: list[str], show_legal: bool = True,
                    cap_seconds: float | None = None, nudge: str | None = None, image: bool = False,
                    notes: list[dict] | None = None) -> str:
    """The whole request text for a fresh session: rules, memory, game header, moves so far, position."""
    return "\n\n".join([RULES_TEXT, memory_section(context), game_header_section(context),
                        history_section(history, notes),
                        position_block(board, go_args, show_legal, cap_seconds, nudge, image)])


def build_delta_turn(board: chess.Board, go_args: dict, history: list[str], seen_plies: int, show_legal: bool = True,
                     cap_seconds: float | None = None, nudge: str | None = None, image: bool = False) -> str:
    """A later turn of the same session: only the moves since the last turn, then the position."""
    since = san_moves_since(history, seen_plies)
    head = f"MOVES SINCE YOUR LAST TURN\n{since}" if since else "MOVES SINCE YOUR LAST TURN\n(none)"
    return head + "\n\n" + position_block(board, go_args, show_legal, cap_seconds, nudge, image)


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


def limit_wait_seconds(text: str, attempt: int, now: float | None = None) -> float:
    """Seconds to wait before asking again: the reset time when the CLI states it, else 60 s doubling to 15 min."""
    match = CLAUDE_RESET.search(text or "")
    if match:
        until = int(match.group(1))
        left = until - (time.time() if now is None else now)
        if 0 < left < 7 * 24 * 3600:
            return min(max(left + 5, LIMIT_WAIT_FIRST_SECONDS), 6 * 3600)
    return min(LIMIT_WAIT_MAX_SECONDS, LIMIT_WAIT_FIRST_SECONDS * (2 ** max(0, attempt)))


def http_usage(usage: dict) -> dict:
    """OpenAI-compatible usage -> {input, cached, output}. DeepSeek reports prompt_cache_hit_tokens."""
    usage = usage or {}
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
        self.end_session()
        self.context = None
        self._notes = []

    def new_conversation(self) -> dict:
        self._conv = {"id": None, "started": False, "seen": 0, "messages": [], "committed": 0, "codex_total": None}
        return self._conv

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
        if self.context is None:
            return build_prompt(board, go_args, history, rejections, self.show_legal, cap, self._nudge, self.board_image)
        conv = self._conv if self.conversation else None
        if conv and conv.get("started"):
            if rejections and conv.get("turn_plies") == len(history):
                # Same move, previous reply rejected: the position is already in the session.
                return (f"Your last reply was rejected: {rejections[-1]}. Choose again from the legal moves above. "
                        "Reply with ONLY the JSON object.")
            return build_delta_turn(board, go_args, history, conv.get("seen", 0), self.show_legal, cap, self._nudge, image)
        text = build_full_turn(self.context, board, go_args, history, self.show_legal, cap, self._nudge, image, self._notes)
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
                self.log(f"{self.provider} {self.model} LIMIT WAIT {waits}: sleeping {seconds:.0f}s, then the same "
                         f"request again ({reason})")
                if self.on_limit_wait is not None:
                    try:
                        self.on_limit_wait(int(seconds), reason)
                    except Exception:
                        pass
                self._think(f"\n[waiting {seconds:.0f}s for the {self.provider} usage limit to reset; the clock is stopped]\n")
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
                self._think(f"\n[move] {board.san(move)}" + (f" - {comment}" if comment else "") + "\n")
                self._think_close()
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
            self.invalid_model_moves += 1
            self._think(f"\n[reply {attempt} rejected: {last_error[:160]}]\n")
            self.log(
                f"{self.provider} {self.model} attempt {attempt}/{self.max_attempts} rejected after "
                f"{time.monotonic() - started:.1f}s: {last_error[:400]} (invalid_count={self.invalid_model_moves})"
            )
        return "0000", f"{self.provider} {self.model} failed after {self.max_attempts} attempts; forfeiting ({last_error[:200]})"

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
        except CliCut:
            thoughts = "\n".join(t for t in self._cli_thoughts if t.strip())[-THOUGHTS_TAIL_CHARS:]
            self.last_report["hurried"] = self.last_report.get("hurried", 0) + 1
            self._think(f"\n[thinking stopped at the move cap - answering at low effort from its revealed thinking]\n")
            self.log(f"{self.provider} {self.model} stopped at the {self._cli_cut_s:.0f}s cap; "
                     f"asking at low effort with {len(thoughts)} chars of revealed thinking")
            if self._conv is not None and self._conv.get("started"):
                # The killed turn may or may not be in the session: repeat the position, then the thinking.
                board = self._board or chess.Board()
                follow = ("Your thinking on the move below was stopped at the move cap.\n\n"
                          + position_block(board, {}, self.show_legal) + "\n\nYour thinking on this move so far:\n"
                          + (thoughts or "(none visible)")
                          + "\nYour time for this move is up. Reply now with only the JSON object for your move.")
            else:
                follow = (prompt + "\n\nYour thinking on this move so far (stopped at the move cap):\n"
                          + (thoughts or "(none visible)")
                          + "\nYour time for this move is up. Reply now with only the JSON object for your move.")
            self._cli_cut_s = None
            left = timeout - (time.monotonic() - started)
            answer = self._ask_cli(follow, max(10, int(left)), "low")
            self._attempt_think_ms = None  # two calls: charge the wall time (infrastructure crashes still excluded)
            return answer

    def _cli_session(self) -> dict | None:
        conv = self._conv
        if self._oneshot or conv is None or self.provider == "claude":
            # Claude keeps its game as content blocks in one fresh request (see _claude_flat_prompt):
            # measured 2026-10-08, `claude -p --resume` gave no cache reads for chess turns.
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
        elif self.provider == "claude" and self._conv is not None and not self._oneshot:
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
            if self._conv is not None and not self._oneshot and self._flat_turn is not None:
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
        api_key = os.environ.get(route["key"]) or _user_env(route["key"])
        if not api_key:
            raise ProviderError(f"{route['key']} is not set")
        user: object = prompt
        if self._image:
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
        if streamed.get("usage"):
            self._add_usage(http_usage(streamed["usage"]))
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
        if len(thoughts) > THOUGHTS_HEAD_CHARS + THOUGHTS_TAIL_CHARS:
            thoughts = (thoughts[:THOUGHTS_HEAD_CHARS] + "\n[... middle of the thinking omitted for length ...]\n"
                        + thoughts[-THOUGHTS_TAIL_CHARS:])
        total = self._cutoff_s or 30.0
        why = "ran out of output space" if streamed.get("finish") == "length" else f"reached {THINK_SHARE * total:.0f}s of its {total:.0f}s move cap"
        self.log(f"{self.provider} {self.model} thinking stopped after {time.monotonic() - started:.1f}s ({why}); "
                 f"returning its {len(thoughts)} chars of thinking and asking for the move")
        self.last_report["hurried"] = self.last_report.get("hurried", 0) + 1
        self._think(f"\n[thinking stopped: {why} - answering from its own thoughts]\n")
        messages = payload["messages"] + [
            {"role": "assistant", "content": "My thinking on this move so far:\n" + (thoughts or "(no visible thinking)")},
            {"role": "user", "content": "Your time for this move is up. Your full thinking so far is above. "
                                        "Reply now with only the JSON object for your move."}]
        limits = {"same": min(ANSWER_WITH_THOUGHTS_SECONDS, SAME_SHARE * total),
                  "lowest": min(ANSWER_WITH_THOUGHTS_SECONDS, max(LOWEST_MIN_SECONDS, LOWEST_SHARE * total))}
        clock_left = None if self._left_ms is None else self._left_ms / 1000 - (time.monotonic() - started)
        for level in ("same", "lowest"):
            follow = {**payload, "messages": messages, "max_tokens": ANSWER_MAX_TOKENS, "stream": True,
                      "stream_options": {"include_usage": True}}
            if level == "lowest":
                if style == "openrouter":
                    follow["reasoning"] = {"effort": "low"}  # Grok and GLM refuse reasoning off on OpenRouter
                elif style == "effort":
                    follow.pop("reasoning_effort", None)
                    follow["thinking"] = {"type": "disabled"}
            if level == "same":
                answer = self.http_stream(url, follow, headers, int(limits[level]) + 10, limits[level])
            else:
                # No share limit: wait for the answer until the clock is gone (then it is a loss on time).
                left = 600.0 if clock_left is None else max(5.0, clock_left - (time.monotonic() - started))
                answer = self.http_stream(url, follow, headers, int(left) + 5, None)
            self.usage_log.append(answer.get("usage") or {})
            if answer.get("usage"):
                self._add_usage(http_usage(answer["usage"]))
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
                            piece = str(delta.get("reasoning_content") or delta.get("reasoning") or "")
                            state["reasoning"] += piece
                            self._think(piece)
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
