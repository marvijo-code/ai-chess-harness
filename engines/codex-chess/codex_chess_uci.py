import asyncio
import copy
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import chess
import websockets
from websockets.exceptions import ConnectionClosed


ENGINE_DIR = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("CODEX_CHESS_ROOT", ENGINE_DIR.parents[1]))
ENGINE_NAME = os.environ.get("CODEX_CHESS_ENGINE_NAME", "Codex-chess")
ENGINE_AUTHOR = os.environ.get("CODEX_CHESS_AUTHOR", "marvijo/Codex app-server")
LOG_DIR = ROOT / "out" / "codex-chess-logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH = LOG_DIR / f"codex-chess-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}.log"
CONTEXT_DIR = Path(os.environ.get("CODEX_CHESS_CONTEXT_DIR", ENGINE_DIR)).resolve()
MEMORY_PATH = CONTEXT_DIR / "MEMORY.md"
SKILLS_DIR = CONTEXT_DIR / "skills"
KNOWLEDGEBASE_DIR = CONTEXT_DIR / "knowledgebase"
FEN_KNOWLEDGE_PATH = KNOWLEDGEBASE_DIR / "fen-curriculum-lessons.md"
STRATEGY_LESSONS_PATH = KNOWLEDGEBASE_DIR / "strategy-lessons.md"
DEFAULT_USE_MEMORY = os.environ.get("CODEX_CHESS_USE_MEMORY", "false").lower() in {"1", "true", "yes", "on"}
DEFAULT_USE_SKILLS = os.environ.get("CODEX_CHESS_USE_SKILLS", "false").lower() in {"1", "true", "yes", "on"}
DEFAULT_LEARNING_MODE = os.environ.get("CODEX_CHESS_LEARNING_MODE", "false").lower() in {"1", "true", "yes", "on"}
DEFAULT_ZERO_MODE = os.environ.get("CODEX_CHESS_ZERO_MODE", "false").lower() in {"1", "true", "yes", "on"}
DEFAULT_FORCE_LEAN_CONTEXT = os.environ.get("CODEX_CHESS_FORCE_LEAN_CONTEXT", "false").lower() in {"1", "true", "yes", "on"}
TEXT_CONTEXT_EXTENSIONS = {".md", ".txt", ".json", ".yaml", ".yml"}
UCI_TEXT_TRANSLATION = str.maketrans(
    {
        "\u00a0": " ",
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2013": "-",
        "\u2014": "-",
        "\u2026": "...",
    }
)
PIECE_VALUES = {
    chess.PAWN: 100,
    chess.KNIGHT: 300,
    chess.BISHOP: 300,
    chess.ROOK: 500,
    chess.QUEEN: 900,
    chess.KING: 0,
}
PIECE_NAMES = {
    chess.PAWN: "pawn",
    chess.KNIGHT: "knight",
    chess.BISHOP: "bishop",
    chess.ROOK: "rook",
    chess.QUEEN: "queen",
    chess.KING: "king",
}


def config_value(path: str, default=None):
    config_path = ROOT / "chess-harness.config.json"
    try:
        current = json.loads(config_path.read_text(encoding="utf-8"))
    except OSError:
        return default
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return default if current is None else current


def int_config_value(path: str, default: int) -> int:
    try:
        return int(config_value(path, default))
    except (TypeError, ValueError):
        return default


def int_env_value(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default


def context_limit(engine_profile: str, context_profile: str, key: str, default: int) -> int:
    value = int_config_value(f"codex.contextLimits.{engine_profile}.{context_profile}.{key}", default)
    return max(0, value)


def material_balance(board: chess.Board, color: bool) -> int:
    own = 0
    enemy = 0
    for piece in board.piece_map().values():
        value = PIECE_VALUES.get(piece.piece_type, 0)
        if piece.color == color:
            own += value
        else:
            enemy += value
    return own - enemy


def piece_name(piece: chess.Piece | None) -> str:
    if piece is None:
        return ""
    return PIECE_NAMES.get(piece.piece_type, piece.symbol().lower())


def captured_piece_before_move(board: chess.Board, move: chess.Move) -> chess.Piece | None:
    if board.is_en_passant(move):
        offset = -8 if board.turn == chess.WHITE else 8
        return board.piece_at(move.to_square + offset)
    return board.piece_at(move.to_square)


def material_safety_summary(board: chess.Board, legal_moves: list[str], max_items: int = 16) -> dict:
    color = board.turn
    current_balance = material_balance(board, color)
    risks = []
    for move_text in legal_moves:
        try:
            move = chess.Move.from_uci(move_text)
        except ValueError:
            continue
        if move not in board.legal_moves:
            continue
        mover = board.piece_at(move.from_square)
        if mover is None:
            continue
        captured = captured_piece_before_move(board, move)
        san = board.san(move)
        after = board.copy(stack=False)
        after.push(move)
        after_balance = material_balance(after, color)
        moved_after = after.piece_at(move.to_square)
        worst = None
        for reply in after.legal_moves:
            reply_san = after.san(reply)
            reply_board = after.copy(stack=False)
            reply_board.push(reply)
            after_reply_balance = material_balance(reply_board, color)
            swing = after_balance - after_reply_balance
            captures_moved_piece = bool(moved_after and after.is_capture(reply) and reply.to_square == move.to_square)
            if swing >= 300 or (captures_moved_piece and PIECE_VALUES.get(moved_after.piece_type, 0) >= 300):
                candidate = {
                    "reply_uci": reply.uci(),
                    "reply_san": reply_san,
                    "material_swing_cp": swing,
                    "balance_after_reply_cp": after_reply_balance,
                    "captures_moved_piece": captures_moved_piece,
                }
                if worst is None or swing > worst["material_swing_cp"]:
                    worst = candidate
        if not worst:
            continue
        moved_name = piece_name(moved_after or mover)
        captured_name = piece_name(captured)
        warning_parts = [
            f"{move_text} ({san}) moves {moved_name}",
            f"worst legal reply {worst['reply_uci']} ({worst['reply_san']}) shifts material {worst['material_swing_cp']}cp",
        ]
        if captured_name:
            warning_parts.append(f"after winning {captured_name}")
        if worst["captures_moved_piece"]:
            warning_parts.append(f"by capturing the moved {moved_name}")
        risks.append(
            {
                "uci": move_text,
                "san": san,
                "moved_piece": moved_name,
                "moved_piece_value_cp": PIECE_VALUES.get((moved_after or mover).piece_type, 0),
                "captured_piece": captured_name,
                "captured_piece_value_cp": PIECE_VALUES.get(captured.piece_type, 0) if captured else 0,
                "balance_after_move_cp": after_balance,
                **worst,
                "warning": "; ".join(warning_parts),
            }
        )
    risks.sort(key=lambda item: (-int(item["material_swing_cp"]), -int(item["moved_piece_value_cp"]), item["uci"]))
    return {
        "current_material_balance_cp": current_balance,
        "risky_moves": risks[:max_items],
        "omitted_risky_move_count": max(0, len(risks) - max_items),
        "policy": (
            "Before choosing captures, queen moves, rook moves, checks, or trades, inspect risky_moves. "
            "Avoid moves where an immediate legal reply wins a queen/rook or causes a large material swing unless there is concrete forcing compensation."
        ),
    }


def log(message: str) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(f"[{timestamp}] {message}\n")


def sanitize_uci_line(line: str) -> str:
    text = str(line).replace("\r", " ").replace("\n", " ").translate(UCI_TEXT_TRANSLATION)
    return text.encode("ascii", errors="replace").decode("ascii")


def emit_uci_line(line: str, *, optional: bool = False) -> bool:
    safe_line = sanitize_uci_line(line)
    try:
        sys.stdout.write(safe_line + "\n")
        sys.stdout.flush()
        return True
    except (BrokenPipeError, OSError, UnicodeError) as exc:
        log(f"failed to write UCI line {safe_line[:120]!r}: {type(exc).__name__}: {exc}")
        if optional:
            return False
        raise


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def parse_json_object(text: str) -> dict:
    text = text.strip()
    if not text:
        raise ValueError("empty Codex response")
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I).strip()
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if not match:
            raise
        return json.loads(match.group(0))


def print_neutral_score_info(*, optional: bool = False) -> None:
    emit_uci_line("info depth 0 score cp 0 nodes 0 time 0", optional=optional)


class CodexTurnError(RuntimeError):
    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code


def codex_error_message(error: dict | None) -> tuple[str, str]:
    if not isinstance(error, dict):
        return "unknown Codex app-server error", ""
    message = str(error.get("message") or error)
    code = str(error.get("codexErrorInfo") or error.get("code") or "")
    return message, code


def read_limited_text(path: Path, max_chars: int) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n[truncated]"


def collect_text_context(root: Path, max_files: int = 8, max_chars_per_file: int = 2000) -> list[dict]:
    if not root.exists():
        return []
    paths = [
        path
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in TEXT_CONTEXT_EXTENSIONS
    ]
    paths.sort(key=lambda path: (path.stat().st_mtime, str(path).lower()), reverse=True)
    files = []
    for path in paths[:max_files]:
        files.append(
            {
                "path": str(path.relative_to(root)),
                "text": read_limited_text(path, max_chars_per_file),
            }
        )
    return files


def extract_text_fragment(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if isinstance(value.get("text"), str):
            return value["text"]
        if isinstance(value.get("json"), (dict, list)):
            return json.dumps(value["json"], separators=(",", ":"))
        return "".join(extract_text_fragment(item) for item in value.values())
    if isinstance(value, list):
        return "".join(extract_text_fragment(item) for item in value)
    return ""


class CodexAppServer:
    def __init__(
        self,
        model: str | None = None,
        effort: str | None = None,
        use_memory: bool = DEFAULT_USE_MEMORY,
        use_skills: bool = DEFAULT_USE_SKILLS,
        learning_mode: bool = DEFAULT_LEARNING_MODE,
        zero_mode: bool = DEFAULT_ZERO_MODE,
        force_lean_context: bool = DEFAULT_FORCE_LEAN_CONTEXT,
    ):
        self.model = model or str(config_value("codex.model", "gpt-5.3-codex"))
        self.effort = effort or str(config_value("codex.effort", "low"))
        self.use_memory = use_memory
        self.use_skills = use_skills
        self.learning_mode = learning_mode
        self.zero_mode = zero_mode
        self.force_lean_context = force_lean_context or zero_mode
        self.port = free_port()
        self.url = f"ws://127.0.0.1:{self.port}"
        self.proc: asyncio.subprocess.Process | None = None
        self.ws = None
        self.next_id = 1
        self.pending: dict[int, asyncio.Future] = {}
        self.turn_text: dict[str, str] = {}
        self.turn_done: dict[str, asyncio.Future] = {}
        self.thread_id: str | None = None
        self.started = False
        self.invalid_model_moves = 0

    async def start(self) -> None:
        if self.started:
            return
        codex_cmd = Path.home() / "AppData/Roaming/npm/codex.cmd"
        self.proc = await asyncio.create_subprocess_exec(
            str(codex_cmd),
            "app-server",
            "--listen",
            self.url,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

        last_error = None
        for _ in range(80):
            try:
                self.ws = await websockets.connect(self.url)
                break
            except Exception as exc:
                last_error = exc
                await asyncio.sleep(0.25)
        if self.ws is None:
            raise RuntimeError(f"Could not connect to Codex app-server: {last_error}")

        asyncio.create_task(self._reader())
        init = await self.request(
            "initialize",
            {
                "clientInfo": {"name": "Codex-chess UCI", "version": "0.1.0"},
                "capabilities": None,
            },
        )
        log(f"app-server initialized: {init.get('userAgent')}")

        sandbox = "workspace-write" if self.learning_mode else "read-only"
        if self.learning_mode:
            KNOWLEDGEBASE_DIR.mkdir(parents=True, exist_ok=True)

        developer_instructions = (
            "Return only JSON matching the schema. "
            "The host GUI will reject illegal moves, so uci must be copied exactly from legal_moves. "
            "When own_remaining is below 25000ms, answer immediately with one legal uci and an empty comment. "
            "Otherwise include one short public move comment; do not reveal hidden chain-of-thought."
        )
        if self.zero_mode:
            developer_instructions += (
                " Zero mode is active: play very quickly from first principles. "
                "Use only the current FEN, legal_moves, clocks, material_safety, and compact engine-local feedback. "
                "Do not use an opening book, memorized move answers, or broad inherited learner strategy. "
                "Apply a short checklist internally: legal move, own king safety, immediate material swing, opponent's best capture, then simple development or conversion. "
                "Prefer a safe practical legal move over slow calculation, and keep comments empty or extremely short."
            )
        if not self.learning_mode:
            developer_instructions += " Do not call tools."
        if self.use_memory:
            developer_instructions += (
                f" Use the engine-local memory file at {MEMORY_PATH} as durable context for this engine."
            )
        if self.use_skills:
            developer_instructions += (
                f" Use engine-local Agent Skills under {SKILLS_DIR} when they help chess move selection or tournament learning."
            )
        if self.learning_mode:
            developer_instructions += (
                f" Post-game learning is handled by a local autolearn process that writes {MEMORY_PATH}, {SKILLS_DIR}, and {KNOWLEDGEBASE_DIR}. "
                "During UCI move selection, use the learner_context included in each prompt and do not spend clock time editing files. "
                "Do not use network access. Return only the required move JSON."
            )

        base_instructions = (
            "You are Codex-chess, a UCI chess engine playing under the time control supplied by the chess GUI. "
            "Pick exactly one legal move from the supplied legal_moves list. "
            "Respect the remaining clocks and avoid spending time on prose."
        )
        if self.zero_mode:
            base_instructions = (
                "You are Codex-chess-zero, a fast self-learning UCI chess engine. "
                "You start from first principles each move, use only the supplied position data and compact post-game feedback, "
                "and return exactly one legal UCI move quickly."
            )

        started = await self.request(
            "thread/start",
            {
                "cwd": str(ROOT),
                "model": self.model,
                "approvalPolicy": "never",
                "sandbox": sandbox,
                "ephemeral": True,
                "baseInstructions": base_instructions,
                "developerInstructions": developer_instructions,
            },
        )
        self.thread_id = started["thread"]["id"]
        self.started = True
        log(
            "thread started: "
            f"id={self.thread_id} model={started.get('model')} "
            f"provider={started.get('modelProvider')} serviceTier={started.get('serviceTier')} "
            f"context={CONTEXT_DIR} memory={self.use_memory} skills={self.use_skills} "
            f"learning={self.learning_mode} sandbox={sandbox} knowledgebase={KNOWLEDGEBASE_DIR}"
        )

    async def _reader(self) -> None:
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                if "id" in msg and msg["id"] in self.pending:
                    fut = self.pending.pop(msg["id"])
                    if "error" in msg:
                        fut.set_exception(RuntimeError(json.dumps(msg["error"])))
                    else:
                        fut.set_result(msg.get("result"))
                    continue

                method = msg.get("method")
                params = msg.get("params", {})
                if method in {"item/agentMessage/delta", "item/agent_message/delta"}:
                    turn_id = params["turnId"]
                    self.turn_text[turn_id] = self.turn_text.get(turn_id, "") + params.get("delta", "")
                elif method == "item/completed":
                    item = params.get("item", {})
                    if item.get("type") in {"agentMessage", "agent_message", "message"}:
                        text = extract_text_fragment(item) or self.turn_text.get(params["turnId"], "")
                        self.turn_text[params["turnId"]] = text
                elif method == "turn/completed":
                    turn = params["turn"]
                    turn_id = turn["id"]
                    fut = self.turn_done.get(turn_id)
                    if fut and not fut.done():
                        if turn.get("status") == "failed" or turn.get("error"):
                            message, code = codex_error_message(turn.get("error"))
                            fut.set_exception(CodexTurnError(message, code))
                        else:
                            fut.set_result(turn)
                elif method == "error":
                    message, code = codex_error_message(params.get("error"))
                    turn_id = params.get("turnId")
                    log(f"codex turn error: code={code or 'unknown'} willRetry={params.get('willRetry')} message={message}")
                    fut = self.turn_done.get(turn_id)
                    if fut and not fut.done():
                        fut.set_exception(CodexTurnError(message, code))
                elif method == "account/rateLimits/updated":
                    limits = params.get("rateLimits", {})
                    primary = limits.get("primary", {})
                    secondary = limits.get("secondary", {})
                    log(
                        "account: "
                        f"limit={limits.get('limitName')} "
                        f"primary_used={primary.get('usedPercent')} reset={primary.get('resetsAt')} "
                        f"secondary_used={secondary.get('usedPercent')} reset={secondary.get('resetsAt')} "
                        f"planType={limits.get('planType')} credits={limits.get('credits')}"
                    )
        except ConnectionClosed:
            return

    async def request(self, method: str, params: dict) -> dict:
        request_id = self.next_id
        self.next_id += 1
        fut = asyncio.get_running_loop().create_future()
        self.pending[request_id] = fut
        await self.ws.send(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}))
        return await fut

    def learner_context(self, profile: str = "full") -> dict:
        if not (self.use_memory or self.use_skills or self.learning_mode):
            return {}
        lean = profile == "lean"
        if self.zero_mode:
            engine_profile = "zero"
            default_limits = {
                "memoryChars": 600 if lean else 900,
                "fenChars": 250 if lean else 450,
                "strategyChars": 300 if lean else 600,
                "knowledgebaseFiles": 1 if lean else 2,
                "knowledgebaseChars": 300 if lean else 500,
                "skillFiles": 0,
                "skillChars": 0,
            }
        else:
            engine_profile = "learner"
            default_limits = {
                "memoryChars": 700 if lean else 1200,
                "fenChars": 350 if lean else 700,
                "strategyChars": 500 if lean else 1000,
                "knowledgebaseFiles": 1 if lean else 2,
                "knowledgebaseChars": 350 if lean else 600,
                "skillFiles": 0 if lean else 1,
                "skillChars": 0 if lean else 350,
            }
        memory_chars = context_limit(engine_profile, profile, "memoryChars", default_limits["memoryChars"])
        fen_chars = context_limit(engine_profile, profile, "fenChars", default_limits["fenChars"])
        strategy_chars = context_limit(engine_profile, profile, "strategyChars", default_limits["strategyChars"])
        kb_files = context_limit(engine_profile, profile, "knowledgebaseFiles", default_limits["knowledgebaseFiles"])
        kb_chars = context_limit(engine_profile, profile, "knowledgebaseChars", default_limits["knowledgebaseChars"])
        skill_files = context_limit(engine_profile, profile, "skillFiles", default_limits["skillFiles"])
        skill_chars = context_limit(engine_profile, profile, "skillChars", default_limits["skillChars"])
        fen_knowledge = read_limited_text(FEN_KNOWLEDGE_PATH, fen_chars)
        strategy_lessons = read_limited_text(STRATEGY_LESSONS_PATH, strategy_chars)
        policy = (
            "Apply the learner memory, fen_knowledge, strategy_lessons, and knowledgebase directly. "
            "Use model-discovered strategy_lessons as generic value adjustments before finalizing a move, not as memorized move answers. "
            "Never invent UCI: copy uci exactly from legal_moves, and never return 0000 while legal moves exist."
        )
        if self.zero_mode:
            policy = (
                "Zero mode: use memory and knowledgebase only as compact post-game feedback. "
                "Current FEN, legal_moves, clocks, and material_safety are the source of truth. "
                "Do not copy memorized openings; choose quickly from first principles and let post-game feedback improve future choices. "
                "Never invent UCI or return 0000 while legal moves exist."
            )
        if lean:
            policy += " Lean clock mode is active: use only the strongest remembered idea and answer quickly."
        return {
            "profile": profile,
            "memory_path": str(MEMORY_PATH),
            "memory": read_limited_text(MEMORY_PATH, memory_chars),
            "knowledgebase_path": str(KNOWLEDGEBASE_DIR),
            "fen_knowledge_path": str(FEN_KNOWLEDGE_PATH),
            "fen_knowledge": fen_knowledge,
            "strategy_lessons_path": str(STRATEGY_LESSONS_PATH),
            "strategy_lessons": strategy_lessons,
            "knowledgebase": collect_text_context(KNOWLEDGEBASE_DIR, max_files=kb_files, max_chars_per_file=kb_chars),
            "skills_path": str(SKILLS_DIR),
            "skills": collect_text_context(SKILLS_DIR, max_files=skill_files, max_chars_per_file=skill_chars),
            "policy": policy,
        }

    def move_timeout_seconds(self, remaining: int | None) -> int:
        if self.zero_mode:
            max_timeout = max(1, int_env_value("CODEX_CHESS_MOVE_TIMEOUT_SECONDS", int_config_value("codex.zeroMoveTimeoutSeconds", 6)))
            critical_timeout = max(1, int_env_value("CODEX_CHESS_CRITICAL_MOVE_TIMEOUT_SECONDS", int_config_value("codex.zeroCriticalMoveTimeoutSeconds", 2)))
        else:
            max_timeout = max(1, int_env_value("CODEX_CHESS_MOVE_TIMEOUT_SECONDS", int_config_value("codex.moveTimeoutSeconds", 12)))
            critical_timeout = max(1, int_env_value("CODEX_CHESS_CRITICAL_MOVE_TIMEOUT_SECONDS", int_config_value("codex.criticalMoveTimeoutSeconds", 3)))
        if remaining is None:
            return max_timeout
        if remaining <= 1000:
            return 1
        remaining_seconds = max(1, int(remaining / 1000))
        if remaining < 25000:
            return max(1, min(critical_timeout, remaining_seconds - 1))
        if self.zero_mode:
            clock_divisor = max(1, int_env_value("CODEX_CHESS_MOVE_TIMEOUT_CLOCK_DIVISOR", int_config_value("codex.zeroMoveTimeoutClockDivisor", 60)))
        else:
            clock_divisor = max(1, int_env_value("CODEX_CHESS_MOVE_TIMEOUT_CLOCK_DIVISOR", int_config_value("codex.moveTimeoutClockDivisor", 12)))
        clock_budget = max(1, int(remaining_seconds / clock_divisor))
        return max(1, min(max_timeout, clock_budget, remaining_seconds - 1))

    def retry_timeout_seconds(self, remaining: int | None, elapsed_seconds: float) -> int:
        if self.zero_mode:
            retry_timeout = max(1, int_env_value("CODEX_CHESS_RETRY_MOVE_TIMEOUT_SECONDS", int_config_value("codex.zeroRetryMoveTimeoutSeconds", 3)))
        else:
            retry_timeout = max(1, int_env_value("CODEX_CHESS_RETRY_MOVE_TIMEOUT_SECONDS", int_config_value("codex.retryMoveTimeoutSeconds", 8)))
        if remaining is None:
            return retry_timeout
        remaining_after_elapsed = max(0, int(remaining / 1000) - int(elapsed_seconds))
        if remaining_after_elapsed <= 1:
            return 1
        return max(1, min(retry_timeout, remaining_after_elapsed - 1))

    def move_effort(self, critical_clock: bool) -> str:
        if critical_clock:
            return str(config_value("codex.criticalMoveEffort", config_value("codex.retryEffort", "low")))
        if self.zero_mode:
            return str(config_value("codex.zeroMoveEffort", "low"))
        if self.learning_mode:
            return str(config_value("codex.learnerMoveEffort", "medium"))
        return self.effort

    def comment_schema(self, require_comment: bool) -> dict:
        schema = {"type": "string", "maxLength": 240}
        if require_comment:
            schema["minLength"] = 1
        return schema

    def visible_move_comment(self, move: str, comment: object, require_comment: bool) -> str:
        safe_comment = " ".join(str(comment or "").split())
        if safe_comment:
            return safe_comment
        if require_comment:
            return f"Selected {move}; model returned no comment."
        return ""

    def urgent_retry_prompt(self, prompt: dict, timeout: int, reason: str) -> dict:
        retry_prompt = copy.deepcopy(prompt)
        retry_prompt.pop("learner_context", None)
        retry_prompt["_turn_effort"] = str(config_value("codex.retryEffort", "low"))
        retry_prompt["comment_required"] = False
        retry_prompt["learner_context_summary"] = (
            "Urgent retry mode. Do not use memory, skills, tools, files, or long analysis. "
            "Read side_to_move, fen, and legal_moves directly, then copy one legal uci exactly from legal_moves."
        )
        retry_prompt["turn_timeout_seconds"] = timeout
        retry_prompt["retry_reason"] = reason
        retry_prompt["time_management"] = (
            "Return strict JSON immediately. Pick a practical legal move from legal_moves, "
            "set comment to an empty string, and finish before turn_timeout_seconds."
        )
        retry_prompt["comment_policy"] = "Set comment to an empty string during urgent retries."
        return retry_prompt

    def client_repetition_risk(self, board: chess.Board, legal_moves: list[str]) -> dict:
        repeated: list[str] = []
        threefold: list[str] = []
        for move_text in legal_moves:
            temp = board.copy()
            move = chess.Move.from_uci(move_text)
            temp.push(move)
            if temp.is_repetition(3):
                threefold.append(move_text)
            elif temp.is_repetition(2):
                repeated.append(move_text)
        return {
            "repeat_moves": len(repeated),
            "threefold_moves": len(threefold),
        }

    async def choose_move(self, board: chess.Board, go_args: dict, history: list[str]) -> str:
        legal_moves = [move.uci() for move in board.legal_moves]
        if not legal_moves:
            return "0000"

        remaining = go_args.get("wtime") if board.turn == chess.WHITE else go_args.get("btime")
        increment = go_args.get("winc") if board.turn == chess.WHITE else go_args.get("binc")
        if remaining is not None and remaining <= 0:
            side = "White" if board.turn == chess.WHITE else "Black"
            log(f"{side} clock already expired ({remaining} ms); forfeiting without starting a Codex turn")
            emit_uci_line(f"info string {side} clock expired; forfeiting game without starting model turn")
            print_neutral_score_info()
            return "0000"

        await self.start()
        repetition = self.client_repetition_risk(board, legal_moves)
        material_safety_limit = max(1, int_config_value("codex.materialSafetyMaxItems", 8))
        material_safety = material_safety_summary(board, legal_moves, max_items=material_safety_limit)

        critical_context_below = int_env_value("CODEX_CHESS_CRITICAL_CONTEXT_BELOW_MS", int_config_value("codex.criticalContextBelowMs", 60000))
        lean_context_below = int_env_value("CODEX_CHESS_LEAN_CONTEXT_BELOW_MS", int_config_value("codex.leanContextBelowMs", 240000))
        critical_clock = remaining is not None and remaining < critical_context_below
        lean_clock = self.force_lean_context or (remaining is not None and remaining < lean_context_below)
        comment_required = not critical_clock and not self.zero_mode
        history_limit = max(0, int_config_value("codex.historyMoveLimit", 16))
        recent_history = history[-history_limit:] if history_limit else []
        prompt = {
            "engine": ENGINE_NAME,
            "engine_profile": "zero" if self.zero_mode else "standard",
            "game_time_control": "Use only the GUI clock fields below; infer the practical time control from them.",
            "side_to_move": "white" if board.turn == chess.WHITE else "black",
            "fen": board.fen(),
            "legal_moves": legal_moves,
            "material_safety": material_safety,
            "uci_history": recent_history,
            "omitted_history_count": max(0, len(history) - len(recent_history)),
            "clock_ms": {
                "white": go_args.get("wtime"),
                "black": go_args.get("btime"),
                "white_increment": go_args.get("winc", 0),
                "black_increment": go_args.get("binc", 0),
                "own_remaining": remaining,
                "own_increment": increment or 0,
            },
            "turn_timeout_seconds": self.move_timeout_seconds(remaining),
            "time_management": (
                "Choose a practical legal move quickly under the supplied remaining clocks. "
                "The host will stop waiting after turn_timeout_seconds, so return before that budget. "
                "If own_remaining is below 60000ms, do not analyze deeply: copy any clearly legal useful move from legal_moves, "
                "set comment to an empty string, and return strict JSON immediately. The harness will not choose a move for you."
            ),
            "comment_required": comment_required,
            "comment_policy": (
                "Return one short public move comment, at most 16 words, explaining the selected move. "
                "Do not leave comment empty and do not include hidden chain-of-thought."
                if comment_required
                else "Set comment to an empty string in critical-clock mode."
            ),
            "invalid_response_policy": (
                "Return only JSON matching the schema, with uci copied exactly from legal_moves. "
                "Empty text, non-JSON, and any uci not in legal_moves count as invalid model responses. "
                "Three consecutive invalid responses for this engine forfeit the game."
            ),
            "tactical_safety_policy": (
                "Use material_safety as current-position tactical context. Do not describe a move as simplifying "
                "if material_safety says an immediate legal reply wins your queen, rook, or large material with no forced compensation."
            ),
            "_turn_effort": self.move_effort(critical_clock),
        }
        if self.zero_mode:
            prompt["time_management"] += (
                " Zero mode target: answer in a few seconds, use lean context only, and prefer a safe first-principles move over slow calculation."
            )
            prompt["zero_learning_policy"] = (
                "You may learn only from compact post-game feedback already present in this engine's memory/knowledgebase. "
                "During a move, do not edit files or call tools; infer from FEN, legal_moves, material_safety, and the current clock."
            )
        context_profile = "none"
        context = {}
        if not critical_clock:
            context_profile = "lean" if lean_clock else "full"
            context = self.learner_context(context_profile)
            if not context:
                context_profile = "none"
        if context:
            prompt["learner_context"] = context
        elif critical_clock and self.learning_mode:
            context_profile = "critical-summary"
            prompt["learner_context_summary"] = (
                "Critical clock mode. Apply only the most important learner rule: do not flag or forfeit; "
                "read the FEN side-to-move and legal_moves directly, copy one legal uci from legal_moves immediately, "
                "avoid repetition moves if easy, and use an empty comment."
            )
        log(
            "decision prompt: "
            f"side={prompt['side_to_move']} fen={board.fen()} "
            f"legal_moves={len(legal_moves)} own_remaining={remaining} own_increment={increment or 0} "
            f"learner_context={context_profile} timeout={prompt['turn_timeout_seconds']}s "
            f"effort={prompt['_turn_effort']} history={len(recent_history)}/{len(history)} "
            f"material_risks={len(material_safety.get('risky_moves', []))} "
            f"repeat_moves={repetition['repeat_moves']} threefold_moves={repetition['threefold_moves']}"
        )

        timeout = prompt["turn_timeout_seconds"]
        move_started = time.monotonic()

        while self.invalid_model_moves < 3:
            if not self.started:
                await self.start()
            attempt = self.invalid_model_moves + 1
            prompt["attempt"] = attempt
            if self.invalid_model_moves:
                prompt["previous_invalid_responses"] = self.invalid_model_moves
            turn_effort = str(prompt.get("_turn_effort", self.effort))
            wire_prompt = {key: value for key, value in prompt.items() if not str(key).startswith("_")}
            response = await self.request(
                "turn/start",
                {
                    "threadId": self.thread_id,
                    "effort": turn_effort,
                    "input": [{"type": "text", "text": json.dumps(wire_prompt, separators=(",", ":"))}],
                    "outputSchema": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["uci", "comment"],
                        "properties": {
                            "uci": {"type": "string"},
                            "comment": self.comment_schema(bool(prompt.get("comment_required"))),
                        },
                    },
                },
            )
            turn_id = response["turn"]["id"]
            done = asyncio.get_running_loop().create_future()
            self.turn_done[turn_id] = done

            try:
                await asyncio.wait_for(done, timeout=timeout)
                text = self.turn_text.get(turn_id, "").strip()
                data = parse_json_object(text)
                move = str(data.get("uci", "")).strip()
                comment = data.get("comment", "")
            except asyncio.TimeoutError:
                self.invalid_model_moves += 1
                log(f"Codex app-server turn timed out after {timeout}s; invalid_count={self.invalid_model_moves}/3")
                emit_uci_line(f"info string Codex app-server turn timed out after {timeout}s", optional=True)
                await self.close()
                if self.invalid_model_moves >= 3:
                    log("Codex app-server timeout streak reached 3; forfeiting with bestmove 0000")
                    emit_uci_line("info string model timeout limit reached; forfeiting game")
                    print_neutral_score_info()
                    return "0000"
                timeout = self.retry_timeout_seconds(remaining, time.monotonic() - move_started)
                prompt = self.urgent_retry_prompt(prompt, timeout, "previous Codex app-server turn timed out")
                log(f"retrying with urgent context-free prompt timeout={timeout}s effort={prompt.get('_turn_effort', self.effort)} after timeout")
                continue
            except CodexTurnError as exc:
                code = f" code={exc.code}" if exc.code else ""
                log(f"Codex app-server turn failed{code}; forfeiting without retry: {exc}")
                emit_uci_line(f"info string Codex app-server turn failed{code}; forfeiting game")
                print_neutral_score_info()
                return "0000"
            except Exception as exc:
                self.invalid_model_moves += 1
                log(
                    "invalid Codex response "
                    f"{self.invalid_model_moves}/3 after {type(exc).__name__}: {exc}"
                )
                if self.invalid_model_moves >= 3:
                    log("invalid Codex response streak reached 3; forfeiting with bestmove 0000")
                    emit_uci_line("info string invalid model response limit reached; forfeiting game")
                    print_neutral_score_info()
                    return "0000"
                continue
            finally:
                self.turn_done.pop(turn_id, None)
                self.turn_text.pop(turn_id, None)

            if move not in legal_moves:
                self.invalid_model_moves += 1
                log(f"illegal Codex move {move!r}; invalid_count={self.invalid_model_moves}/3")
                if self.invalid_model_moves >= 3:
                    log("illegal Codex move streak reached 3; forfeiting with bestmove 0000")
                    emit_uci_line("info string invalid model move limit reached; forfeiting game")
                    print_neutral_score_info()
                    return "0000"
                timeout = self.retry_timeout_seconds(remaining, time.monotonic() - move_started)
                prompt = self.urgent_retry_prompt(prompt, timeout, f"previous move {move!r} was not in legal_moves")
                log(f"retrying with urgent context-free prompt timeout={timeout}s effort={prompt.get('_turn_effort', self.effort)} after illegal move")
                continue

            self.invalid_model_moves = 0
            safe_comment = self.visible_move_comment(
                move,
                comment,
                bool(prompt.get("comment_required")),
            )
            if safe_comment:
                log(f"decision comment: move={move} comment={safe_comment[:500]}")
                emit_uci_line(f"info string {safe_comment[:240]}", optional=True)
            else:
                log(f"decision comment: move={move} comment=")
            return move

        log("invalid response streak reached 3; forfeiting with bestmove 0000")
        emit_uci_line("info string invalid model response limit reached; forfeiting game")
        print_neutral_score_info()
        return "0000"

    async def close(self) -> None:
        if self.ws is not None:
            await self.ws.close()
        if self.proc is not None and self.proc.returncode is None:
            subprocess.run(
                ["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self.proc.kill()
        self.proc = None
        self.ws = None
        self.thread_id = None
        self.started = False
        self.pending.clear()
        self.turn_text.clear()
        self.turn_done.clear()

    def new_game(self) -> None:
        self.invalid_model_moves = 0


class CodexChessUci:
    def __init__(self):
        self.board = chess.Board()
        self.history: list[str] = []
        self.codex = CodexAppServer(
            os.environ.get("CODEX_CHESS_MODEL") or str(config_value("codex.model", "gpt-5.3-codex")),
            os.environ.get("CODEX_CHESS_EFFORT") or str(config_value("codex.effort", "high")),
        )

    def set_position(self, tokens: list[str]) -> None:
        if not tokens:
            return
        moves_index = None
        if "moves" in tokens:
            moves_index = tokens.index("moves")
            position_tokens = tokens[:moves_index]
            move_tokens = tokens[moves_index + 1 :]
        else:
            position_tokens = tokens
            move_tokens = []

        if position_tokens[0] == "startpos":
            board = chess.Board()
        elif position_tokens[0] == "fen":
            board = chess.Board(" ".join(position_tokens[1:]))
        else:
            log(f"unknown position command: {' '.join(tokens)}")
            return

        history = []
        for move_text in move_tokens:
            move = chess.Move.from_uci(move_text)
            if move not in board.legal_moves:
                raise ValueError(f"illegal historical move {move_text} for {board.fen()}")
            board.push(move)
            history.append(move_text)

        self.board = board
        self.history = history
        log(f"position set: fen={board.fen()} moves={len(history)}")

    async def go(self, tokens: list[str]) -> str:
        go_args = parse_go_args(tokens)
        move = await self.codex.choose_move(self.board.copy(), go_args, list(self.history))
        log(f"bestmove {move} from fen={self.board.fen()} go={go_args}")
        return move

    def set_option(self, tokens: list[str]) -> None:
        if "name" not in tokens:
            return
        name_start = tokens.index("name") + 1
        if "value" in tokens:
            value_index = tokens.index("value")
            name = " ".join(tokens[name_start:value_index]).lower()
            value = " ".join(tokens[value_index + 1 :])
        else:
            name = " ".join(tokens[name_start:]).lower()
            value = "true"

        bool_value = value.lower() in {"1", "true", "yes", "on"}
        if name == "usememory":
            self.codex.use_memory = bool_value
        elif name == "useskills":
            self.codex.use_skills = bool_value
        elif name == "learningmode":
            self.codex.learning_mode = bool_value
        elif name == "zeromode":
            self.codex.zero_mode = bool_value
            self.codex.force_lean_context = bool_value or DEFAULT_FORCE_LEAN_CONTEXT


def parse_go_args(tokens: list[str]) -> dict:
    numeric_keys = {"wtime", "btime", "winc", "binc", "movetime", "depth", "nodes", "movestogo"}
    args = {}
    i = 0
    while i < len(tokens):
        key = tokens[i]
        if key in numeric_keys and i + 1 < len(tokens):
            try:
                args[key] = int(tokens[i + 1])
            except ValueError:
                pass
            i += 2
        else:
            args[key] = True
            i += 1
    return args


async def main() -> None:
    engine = CodexChessUci()
    log("Codex-chess UCI started")
    loop = asyncio.get_running_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if line == "":
            break
        line = line.strip()
        if not line:
            continue
        log(f"> {line}")
        command, *tokens = line.split()

        try:
            if command == "uci":
                emit_uci_line(f"id name {ENGINE_NAME}")
                emit_uci_line(f"id author {ENGINE_AUTHOR}")
                emit_uci_line("option name UCI_Chess960 type check default false")
                emit_uci_line(f"option name UseMemory type check default {'true' if DEFAULT_USE_MEMORY else 'false'}")
                emit_uci_line(f"option name UseSkills type check default {'true' if DEFAULT_USE_SKILLS else 'false'}")
                emit_uci_line(f"option name LearningMode type check default {'true' if DEFAULT_LEARNING_MODE else 'false'}")
                emit_uci_line(f"option name ZeroMode type check default {'true' if DEFAULT_ZERO_MODE else 'false'}")
                emit_uci_line("uciok")
            elif command == "isready":
                emit_uci_line("readyok")
            elif command == "ucinewgame":
                engine.board = chess.Board()
                engine.history = []
                engine.codex.new_game()
            elif command == "setoption":
                engine.set_option(tokens)
            elif command == "position":
                engine.set_position(tokens)
            elif command == "go":
                bestmove = await engine.go(tokens)
                print_neutral_score_info()
                emit_uci_line(f"bestmove {bestmove}")
            elif command == "stop":
                print_neutral_score_info(optional=True)
                emit_uci_line("bestmove 0000", optional=True)
            elif command == "quit":
                break
        except Exception as exc:
            log(f"error for command {line!r}: {type(exc).__name__}: {exc}; forfeiting with bestmove 0000")
            if command == "go":
                print_neutral_score_info()
                emit_uci_line("bestmove 0000")

    await engine.codex.close()
    log("Codex-chess UCI stopped")


if __name__ == "__main__":
    asyncio.run(main())
