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
MEMORY_PATH = ENGINE_DIR / "MEMORY.md"
SKILLS_DIR = ENGINE_DIR / "skills"
DEFAULT_USE_MEMORY = os.environ.get("CODEX_CHESS_USE_MEMORY", "false").lower() in {"1", "true", "yes", "on"}
DEFAULT_USE_SKILLS = os.environ.get("CODEX_CHESS_USE_SKILLS", "false").lower() in {"1", "true", "yes", "on"}
DEFAULT_LEARNING_MODE = os.environ.get("CODEX_CHESS_LEARNING_MODE", "false").lower() in {"1", "true", "yes", "on"}


def log(message: str) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(f"[{timestamp}] {message}\n")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def parse_json_object(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if not match:
            raise
        return json.loads(match.group(0))


class CodexAppServer:
    def __init__(
        self,
        model: str = "gpt-5.5",
        effort: str = "low",
        use_memory: bool = DEFAULT_USE_MEMORY,
        use_skills: bool = DEFAULT_USE_SKILLS,
        learning_mode: bool = DEFAULT_LEARNING_MODE,
    ):
        self.model = model
        self.effort = effort
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

        developer_instructions = (
            "Return only JSON matching the schema. Do not call tools. "
            "The host GUI will reject illegal moves, so uci must be copied exactly from legal_moves."
        )
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
                f" After games or reusable insights, create or update concise Agent Skills under {SKILLS_DIR} "
                f"and update {MEMORY_PATH} so this learner becomes a better chess player over time."
            )

        started = await self.request(
            "thread/start",
            {
                "cwd": str(ROOT),
                "model": self.model,
                "approvalPolicy": "never",
                "sandbox": "read-only",
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
            f"provider={started.get('modelProvider')} serviceTier={started.get('serviceTier')}"
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
                if method == "item/agentMessage/delta":
                    turn_id = params["turnId"]
                    self.turn_text[turn_id] = self.turn_text.get(turn_id, "") + params.get("delta", "")
                elif method == "item/completed":
                    item = params.get("item", {})
                    if item.get("type") == "agentMessage":
                        self.turn_text[params["turnId"]] = item.get("text", self.turn_text.get(params["turnId"], ""))
                elif method == "turn/completed":
                    turn_id = params["turn"]["id"]
                    fut = self.turn_done.get(turn_id)
                    if fut and not fut.done():
                        fut.set_result(params["turn"])
                elif method == "account/rateLimits/updated":
                    limits = params.get("rateLimits", {})
                    log(f"account: planType={limits.get('planType')} credits={limits.get('credits')}")
        except ConnectionClosed:
            return

    async def request(self, method: str, params: dict) -> dict:
        request_id = self.next_id
        self.next_id += 1
        fut = asyncio.get_running_loop().create_future()
        self.pending[request_id] = fut
        await self.ws.send(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}))
        return await fut

    async def choose_move(self, board: chess.Board, go_args: dict, history: list[str]) -> str:
        await self.start()
        legal_moves = [move.uci() for move in board.legal_moves]
        if not legal_moves:
            return "0000"

        remaining = go_args.get("wtime") if board.turn == chess.WHITE else go_args.get("btime")
        increment = go_args.get("winc") if board.turn == chess.WHITE else go_args.get("binc")
        if remaining is not None and remaining <= 2000:
            move = legal_moves[0]
            log(f"emergency move due to low clock {remaining}ms: {move}")
            return move

        prompt = {
            "engine": "Codex-chess",
            "game_time_control": "Use only the GUI clock fields below; infer the practical time control from them.",
            "side_to_move": "white" if board.turn == chess.WHITE else "black",
            "fen": board.fen(),
            "legal_moves": legal_moves,
            "uci_history": history,
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
                "If low on time, prefer a simple safe legal move over deep calculation."
            ),
            "comment_policy": "Optionally include one short comment explaining the move; it will be shown as a UCI info string in the chess GUI logs.",
        }

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

        timeout = 90
        if remaining is not None:
            timeout = max(5, min(90, int(remaining / 1000) - 2))
        try:
            await asyncio.wait_for(done, timeout=timeout)
            text = self.turn_text.get(turn_id, "").strip()
            data = parse_json_object(text)
            move = data.get("uci", "")
            comment = data.get("comment", "")
        except Exception as exc:
            move = legal_moves[0]
            comment = ""
            log(f"Codex move fallback after {type(exc).__name__}: {exc}; move={move}")

        if move not in legal_moves:
            self.invalid_model_moves += 1
            if self.invalid_model_moves >= 3:
                log(f"illegal Codex move {move!r}; invalid count reached 3; forfeiting with bestmove 0000")
                print("info string invalid model move limit reached; forfeiting game", flush=True)
                return "0000"
            fallback = legal_moves[0]
            log(f"illegal Codex move {move!r}; invalid_count={self.invalid_model_moves}; fallback={fallback}")
            return fallback
        if comment:
            safe_comment = " ".join(str(comment).split())
            print(f"info string {safe_comment[:240]}", flush=True)
        return move

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
            os.environ.get("CODEX_CHESS_MODEL", "gpt-5.5"),
            os.environ.get("CODEX_CHESS_EFFORT", "low"),
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
                print(f"bestmove {bestmove}", flush=True)
            elif command == "stop":
                print("bestmove 0000", flush=True)
            elif command == "quit":
                break
        except Exception as exc:
            legal = [move.uci() for move in engine.board.legal_moves]
            fallback = legal[0] if legal else "0000"
            log(f"error for command {line!r}: {type(exc).__name__}: {exc}; fallback={fallback}")
            if command == "go":
                print(f"bestmove {fallback}", flush=True)

    await engine.codex.close()
    log("Codex-chess UCI stopped")


if __name__ == "__main__":
    asyncio.run(main())
