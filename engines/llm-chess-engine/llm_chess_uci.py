import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import chess


ROOT = Path(__file__).resolve().parents[2]
LOG_DIR = ROOT / "out" / "llm-chess-engine-logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH = LOG_DIR / f"llm-chess-engine-{time.strftime('%Y%m%d-%H%M%S')}.log"
DEFAULT_MODEL = "moonshotai/kimi-k2.6"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


def log(message: str) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(f"[{timestamp}] {message}\n")


def parse_json_object(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if not match:
            raise
        return json.loads(match.group(0))


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


def print_neutral_score_info() -> None:
    print("info depth 0 score cp 0 nodes 0 time 0", flush=True)


def forfeit_move(reason: str) -> tuple[str, str]:
    return "0000", reason


def normalize_model_name(model: str) -> str:
    return model.strip()


class OpenRouterChessClient:
    def __init__(self) -> None:
        self.model = normalize_model_name(os.environ.get("OPENROUTER_MODEL", DEFAULT_MODEL))
        self.temperature = int(os.environ.get("OPENROUTER_TEMPERATURE", "20"))
        self.max_retries = int(os.environ.get("OPENROUTER_MAX_RETRIES", "1"))
        self.invalid_model_moves = 0

    def set_option(self, name: str, value: str) -> None:
        lowered = name.lower()
        if lowered in {"model", "openrouter_model"} and value:
            self.model = normalize_model_name(value)
        elif lowered == "temperature":
            try:
                self.temperature = max(0, min(100, int(value)))
            except ValueError:
                log(f"invalid temperature option: {value!r}")
        elif lowered == "maxretries":
            try:
                self.max_retries = max(0, min(5, int(value)))
            except ValueError:
                log(f"invalid max retries option: {value!r}")

    def choose_move(self, board: chess.Board, go_args: dict, history: list[str]) -> tuple[str, str]:
        legal_moves = [move.uci() for move in board.legal_moves]
        if not legal_moves:
            return "0000", "no legal moves"

        api_key = os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            return forfeit_move("OPENROUTER_API_KEY is not set; forfeiting")

        remaining = go_args.get("wtime") if board.turn == chess.WHITE else go_args.get("btime")
        payload = self._build_payload(board, go_args, history, legal_moves, with_schema=True)
        timeout = self._timeout_seconds(go_args, remaining)
        last_error = None
        saw_illegal_move = False
        for attempt in range(self.max_retries + 1):
            try:
                data = self._post(api_key, payload, timeout)
                move, comment = self._parse_response(data, legal_moves)
                if move in legal_moves:
                    return move, comment
                saw_illegal_move = True
                last_error = f"illegal move {move!r}"
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")[:500]
                last_error = f"HTTP {exc.code}: {body}"
                if exc.code == 400 and payload.get("response_format"):
                    payload = self._build_payload(board, go_args, history, legal_moves, with_schema=False)
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            log(f"OpenRouter move attempt {attempt + 1} failed: {last_error}")

        if saw_illegal_move:
            self.invalid_model_moves += 1
            if self.invalid_model_moves >= 3:
                return forfeit_move("invalid model move limit reached; forfeiting game")

        return forfeit_move(f"OpenRouter failed; forfeiting ({last_error})")

    def new_game(self) -> None:
        self.invalid_model_moves = 0

    def _timeout_seconds(self, go_args: dict, remaining: int | None) -> int:
        configured = int(os.environ.get("OPENROUTER_TIMEOUT_SECONDS", "90"))
        candidates = [configured]
        if go_args.get("movetime"):
            candidates.append(max(5, int(go_args["movetime"] / 1000) + 2))
        if remaining is not None:
            candidates.append(max(5, int(remaining / 1000) - 1))
        return max(5, min(candidates))

    def _build_payload(
        self,
        board: chess.Board,
        go_args: dict,
        history: list[str],
        legal_moves: list[str],
        with_schema: bool,
    ) -> dict:
        prompt = {
            "engine": "llm-chess-engine",
            "side_to_move": "white" if board.turn == chess.WHITE else "black",
            "fen": board.fen(),
            "legal_moves": legal_moves,
            "uci_history": history,
            "clock_ms": {
                "white": go_args.get("wtime"),
                "black": go_args.get("btime"),
                "white_increment": go_args.get("winc", 0),
                "black_increment": go_args.get("binc", 0),
                "movetime": go_args.get("movetime"),
            },
            "task": "Choose exactly one legal chess move. Copy the uci value exactly from legal_moves.",
        }
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a UCI chess engine. Return only JSON. "
                        "The uci field must be copied exactly from the supplied legal_moves list."
                    ),
                },
                {"role": "user", "content": json.dumps(prompt, separators=(",", ":"))},
            ],
            "temperature": self.temperature / 100,
            "max_tokens": 200,
        }
        if with_schema:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "chess_move",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["uci", "comment"],
                        "properties": {
                            "uci": {"type": "string"},
                            "comment": {"type": "string"},
                        },
                    },
                },
            }
        return payload

    def _post(self, api_key: str, payload: dict, timeout: int) -> dict:
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-Title": os.environ.get("OPENROUTER_APP_NAME", "ai-chess-harness"),
        }
        referer = os.environ.get("OPENROUTER_HTTP_REFERER")
        if referer:
            headers["HTTP-Referer"] = referer
        request = urllib.request.Request(OPENROUTER_URL, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def _parse_response(self, data: dict, legal_moves: list[str]) -> tuple[str, str]:
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError(f"OpenRouter returned no choices: {data}")
        content = choices[0].get("message", {}).get("content", "")
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        text = str(content).strip()
        try:
            parsed = parse_json_object(text)
            move = str(parsed.get("uci", ""))
            repaired = extract_legal_move(move, legal_moves)
            return repaired or move, str(parsed.get("comment", ""))
        except json.JSONDecodeError:
            repaired = extract_legal_move(text, legal_moves)
            if repaired:
                return repaired, "model returned non-JSON text; extracted legal UCI move"
            raise


def extract_legal_move(text: str, legal_moves: list[str]) -> str | None:
    compact = "".join(str(text).split())
    for move in sorted(legal_moves, key=len, reverse=True):
        if compact.startswith(move):
            return move
        if re.search(rf"(?<![a-h1-8qrbn]){re.escape(move)}(?![a-h1-8qrbn])", compact):
            return move
    return None


class LlmChessUci:
    def __init__(self) -> None:
        self.board = chess.Board()
        self.history: list[str] = []
        self.client = OpenRouterChessClient()

    def set_position(self, tokens: list[str]) -> None:
        if not tokens:
            return
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

    def go(self, tokens: list[str]) -> str:
        go_args = parse_go_args(tokens)
        move, comment = self.client.choose_move(self.board.copy(), go_args, list(self.history))
        if comment and re.search(r"[A-Za-z0-9]", comment):
            print(f"info string {' '.join(comment.split())[:240]}", flush=True)
        log(f"bestmove {move} model={self.client.model} fen={self.board.fen()} go={go_args}")
        return move

    def set_option(self, tokens: list[str]) -> None:
        if "name" not in tokens:
            return
        name_start = tokens.index("name") + 1
        if "value" in tokens:
            value_index = tokens.index("value")
            name = " ".join(tokens[name_start:value_index])
            value = " ".join(tokens[value_index + 1 :])
        else:
            name = " ".join(tokens[name_start:])
            value = ""
        self.client.set_option(name, value)


def main() -> None:
    engine = LlmChessUci()
    log("llm-chess-engine UCI started")
    while True:
        line = sys.stdin.readline()
        if line == "":
            break
        line = line.strip()
        if not line:
            continue
        log(f"> {line}")
        command, *tokens = line.split()

        try:
            if command == "uci":
                print("id name llm-chess-engine", flush=True)
                print("id author marvijo/OpenRouter", flush=True)
                print(f"option name Model type string default {DEFAULT_MODEL}", flush=True)
                print("option name Temperature type spin default 20 min 0 max 100", flush=True)
                print("option name MaxRetries type spin default 1 min 0 max 5", flush=True)
                print("uciok", flush=True)
            elif command == "isready":
                print("readyok", flush=True)
            elif command == "ucinewgame":
                engine.board = chess.Board()
                engine.history = []
                engine.client.new_game()
            elif command == "setoption":
                engine.set_option(tokens)
            elif command == "position":
                engine.set_position(tokens)
            elif command == "go":
                bestmove = engine.go(tokens)
                print_neutral_score_info()
                print(f"bestmove {bestmove}", flush=True)
            elif command == "stop":
                print_neutral_score_info()
                print("bestmove 0000", flush=True)
                continue
            elif command == "quit":
                break
        except Exception as exc:
            log(f"error for command {line!r}: {type(exc).__name__}: {exc}; forfeiting with bestmove 0000")
            if command == "go":
                print_neutral_score_info()
                print("bestmove 0000", flush=True)

    log("llm-chess-engine UCI stopped")


if __name__ == "__main__":
    main()
