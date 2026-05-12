import asyncio
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
LOG_PATH = LOG_DIR / f"codex-chess-{time.strftime('%Y%m%d-%H%M%S')}.log"
CONTEXT_DIR = Path(os.environ.get("CODEX_CHESS_CONTEXT_DIR", ENGINE_DIR)).resolve()
MEMORY_PATH = CONTEXT_DIR / "MEMORY.md"
SKILLS_DIR = CONTEXT_DIR / "skills"
KNOWLEDGEBASE_DIR = CONTEXT_DIR / "knowledgebase"
FEN_KNOWLEDGE_PATH = KNOWLEDGEBASE_DIR / "fen-curriculum-lessons.md"
DEFAULT_USE_MEMORY = os.environ.get("CODEX_CHESS_USE_MEMORY", "false").lower() in {"1", "true", "yes", "on"}
DEFAULT_USE_SKILLS = os.environ.get("CODEX_CHESS_USE_SKILLS", "false").lower() in {"1", "true", "yes", "on"}
DEFAULT_LEARNING_MODE = os.environ.get("CODEX_CHESS_LEARNING_MODE", "false").lower() in {"1", "true", "yes", "on"}
TEXT_CONTEXT_EXTENSIONS = {".md", ".txt", ".json", ".yaml", ".yml"}


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


def log(message: str) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(f"[{timestamp}] {message}\n")


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


def print_neutral_score_info() -> None:
    print("info depth 0 score cp 0 nodes 0 time 0", flush=True)


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
    ):
        self.model = model or str(config_value("codex.model", "gpt-5.3-codex"))
        self.effort = effort or str(config_value("codex.effort", "low"))
        self.use_memory = use_memory
        self.use_skills = use_skills
        self.learning_mode = learning_mode
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
            "When own_remaining is below 25000ms, answer immediately with one legal uci and an empty comment."
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

        started = await self.request(
            "thread/start",
            {
                "cwd": str(ROOT),
                "model": self.model,
                "approvalPolicy": "never",
                "sandbox": sandbox,
                "ephemeral": True,
                "baseInstructions": (
                    "You are Codex-chess, a UCI chess engine playing under the time control supplied by the chess GUI. "
                    "Pick exactly one legal move from the supplied legal_moves list. "
                    "Respect the remaining clocks and avoid spending time on prose."
                ),
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

    def learner_context(self) -> dict:
        if not (self.use_memory or self.use_skills or self.learning_mode):
            return {}
        fen_knowledge = read_limited_text(FEN_KNOWLEDGE_PATH, 3500)
        return {
            "memory_path": str(MEMORY_PATH),
            "memory": read_limited_text(MEMORY_PATH, 6000),
            "knowledgebase_path": str(KNOWLEDGEBASE_DIR),
            "fen_knowledge_path": str(FEN_KNOWLEDGE_PATH),
            "fen_knowledge": fen_knowledge,
            "knowledgebase": collect_text_context(KNOWLEDGEBASE_DIR, max_files=8, max_chars_per_file=2500),
            "skills_path": str(SKILLS_DIR),
            "skills": collect_text_context(SKILLS_DIR, max_files=4, max_chars_per_file=1800),
            "policy": (
                "Apply the learner memory, fen_knowledge, and knowledgebase directly. "
                "Never invent UCI: copy uci exactly from legal_moves, and never return 0000 while legal moves exist."
            ),
        }

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
        await self.start()
        legal_moves = [move.uci() for move in board.legal_moves]
        if not legal_moves:
            return "0000"
        repetition = self.client_repetition_risk(board, legal_moves)

        remaining = go_args.get("wtime") if board.turn == chess.WHITE else go_args.get("btime")
        increment = go_args.get("winc") if board.turn == chess.WHITE else go_args.get("binc")

        critical_clock = remaining is not None and remaining < 25000
        prompt = {
            "engine": ENGINE_NAME,
            "game_time_control": "Use only the GUI clock fields below; infer the practical time control from them.",
            "side_to_move": "white" if board.turn == chess.WHITE else "black",
            "fen": board.fen(),
            "legal_moves": legal_moves,
            "uci_history": history[-20:] if critical_clock else history,
            "clock_ms": {
                "white": go_args.get("wtime"),
                "black": go_args.get("btime"),
                "white_increment": go_args.get("winc", 0),
                "black_increment": go_args.get("binc", 0),
                "own_remaining": remaining,
                "own_increment": increment or 0,
            },
            "time_management": (
                "Choose a practical legal move quickly under the supplied remaining clocks. "
                "If own_remaining is below 25000ms, do not analyze deeply: copy any clearly legal useful move from legal_moves, "
                "set comment to an empty string, and return strict JSON immediately. The harness will not choose a move for you."
            ),
            "comment_policy": "Optionally include one short comment explaining the move; it will be shown as a UCI info string in the chess GUI logs.",
            "invalid_response_policy": (
                "Return only JSON matching the schema, with uci copied exactly from legal_moves. "
                "Empty text, non-JSON, and any uci not in legal_moves count as invalid model responses. "
                "Three consecutive invalid responses for this engine forfeit the game."
            ),
        }
        context = {} if critical_clock else self.learner_context()
        if context:
            prompt["learner_context"] = context
        elif critical_clock and self.learning_mode:
            prompt["learner_context_summary"] = (
                "Critical clock mode. Apply only the most important learner rule: do not flag or forfeit; "
                "read the FEN side-to-move and legal_moves directly, copy one legal uci from legal_moves immediately, "
                "avoid repetition moves if easy, and use an empty comment."
            )
        log(
            "decision prompt: "
            f"side={prompt['side_to_move']} fen={board.fen()} "
            f"legal_moves={len(legal_moves)} own_remaining={remaining} own_increment={increment or 0} "
            f"learner_context={'yes' if context else 'no'} repeat_moves={repetition['repeat_moves']} "
            f"threefold_moves={repetition['threefold_moves']}"
        )

        timeout = 90
        if remaining is not None:
            timeout = max(1, min(90, int(remaining / 1000) - 1))

        while self.invalid_model_moves < 3:
            attempt = self.invalid_model_moves + 1
            prompt["attempt"] = attempt
            if self.invalid_model_moves:
                prompt["previous_invalid_responses"] = self.invalid_model_moves
            response = await self.request(
                "turn/start",
                {
                    "threadId": self.thread_id,
                    "effort": self.effort,
                    "input": [{"type": "text", "text": json.dumps(prompt, separators=(",", ":"))}],
                    "outputSchema": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["uci", "comment"],
                        "properties": {
                            "uci": {"type": "string"},
                            "comment": {"type": "string"},
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
            except CodexTurnError as exc:
                code = f" code={exc.code}" if exc.code else ""
                log(f"Codex app-server turn failed{code}; forfeiting without retry: {exc}")
                print(f"info string Codex app-server turn failed{code}; forfeiting game", flush=True)
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
                    print("info string invalid model response limit reached; forfeiting game", flush=True)
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
                    print("info string invalid model move limit reached; forfeiting game", flush=True)
                    print_neutral_score_info()
                    return "0000"
                continue

            self.invalid_model_moves = 0
            if comment:
                safe_comment = " ".join(str(comment).split())
                log(f"decision comment: move={move} comment={safe_comment[:500]}")
                print(f"info string {safe_comment[:240]}", flush=True)
            else:
                log(f"decision comment: move={move} comment=")
            return move

        log("invalid response streak reached 3; forfeiting with bestmove 0000")
        print("info string invalid model response limit reached; forfeiting game", flush=True)
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

    def new_game(self) -> None:
        self.invalid_model_moves = 0


class CodexChessUci:
    def __init__(self):
        self.board = chess.Board()
        self.history: list[str] = []
        self.codex = CodexAppServer(
            os.environ.get("CODEX_CHESS_MODEL") or str(config_value("codex.model", "gpt-5.3-codex")),
            os.environ.get("CODEX_CHESS_EFFORT") or str(config_value("codex.effort", "low")),
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
                print(f"id name {ENGINE_NAME}", flush=True)
                print(f"id author {ENGINE_AUTHOR}", flush=True)
                print("option name UCI_Chess960 type check default false", flush=True)
                print(f"option name UseMemory type check default {'true' if DEFAULT_USE_MEMORY else 'false'}", flush=True)
                print(f"option name UseSkills type check default {'true' if DEFAULT_USE_SKILLS else 'false'}", flush=True)
                print(f"option name LearningMode type check default {'true' if DEFAULT_LEARNING_MODE else 'false'}", flush=True)
                print("uciok", flush=True)
            elif command == "isready":
                print("readyok", flush=True)
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
                print(f"bestmove {bestmove}", flush=True)
            elif command == "stop":
                print_neutral_score_info()
                print("bestmove 0000", flush=True)
            elif command == "quit":
                break
        except Exception as exc:
            log(f"error for command {line!r}: {type(exc).__name__}: {exc}; forfeiting with bestmove 0000")
            if command == "go":
                print_neutral_score_info()
                print("bestmove 0000", flush=True)

    await engine.codex.close()
    log("Codex-chess UCI stopped")


if __name__ == "__main__":
    asyncio.run(main())
