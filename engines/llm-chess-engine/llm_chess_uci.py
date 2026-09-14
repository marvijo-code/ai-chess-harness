import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path

import chess


ROOT = Path(__file__).resolve().parents[2]
LOG_DIR = ROOT / "out" / "llm-chess-engine-logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH = LOG_DIR / f"llm-chess-engine-{time.strftime('%Y%m%d-%H%M%S')}.log"
DEFAULT_MODEL = "moonshotai/kimi-k2.6"
DEFAULT_MAX_ATTEMPTS = 3
MAX_ATTEMPTS_CEILING = 9
DEFAULT_MAX_TOKENS = 1500
MAX_TOKENS_CEILING = 8192
DEFAULT_REASONING_EFFORT = "low"
DEFAULT_PROVIDER_SORT = "throughput"
DEFAULT_RETRY_BACKOFF_SECONDS = 2.0
MAX_RETRY_BACKOFF_SECONDS = 20.0
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
CONFIG_PATH = ROOT / "chess-harness.config.json"


def config_openrouter(key: str):
    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    section = config.get("openrouter") if isinstance(config, dict) else None
    if not isinstance(section, dict):
        return None
    return section.get(key)


def config_max_attempts(default: int = DEFAULT_MAX_ATTEMPTS) -> int:
    value = config_openrouter("maxAttempts")
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def config_max_tokens(default: int = DEFAULT_MAX_TOKENS) -> int:
    value = config_openrouter("maxTokens")
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def clamp_max_attempts(value: object, default: int = DEFAULT_MAX_ATTEMPTS) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return max(1, min(MAX_ATTEMPTS_CEILING, parsed))


def clamp_max_tokens(value: object, default: int = DEFAULT_MAX_TOKENS) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return max(64, min(MAX_TOKENS_CEILING, parsed))


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


class EmptyModelResponse(RuntimeError):
    pass


def env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    try:
        return float(value)
    except ValueError:
        return default


def env_text(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None or value == "" else value.strip()


def normalize_model_name(model: str) -> str:
    return model.strip()


class OpenRouterChessClient:
    def __init__(self) -> None:
        self.model = normalize_model_name(os.environ.get("OPENROUTER_MODEL", DEFAULT_MODEL))
        self.temperature = int(os.environ.get("OPENROUTER_TEMPERATURE", "20"))
        self.max_attempts = clamp_max_attempts(
            os.environ.get("OPENROUTER_MAX_ATTEMPTS"),
            clamp_max_attempts(config_max_attempts(), DEFAULT_MAX_ATTEMPTS),
        )
        self.max_retries = max(0, self.max_attempts - 1)
        self.max_tokens = clamp_max_tokens(
            os.environ.get("OPENROUTER_MAX_TOKENS"),
            clamp_max_tokens(config_max_tokens(), DEFAULT_MAX_TOKENS),
        )
        self.reasoning_effort = env_text(
            "OPENROUTER_REASONING_EFFORT",
            str(config_openrouter("reasoningEffort") or DEFAULT_REASONING_EFFORT),
        )
        self.provider_sort = env_text(
            "OPENROUTER_PROVIDER_SORT",
            str(config_openrouter("providerSort") or DEFAULT_PROVIDER_SORT),
        )
        self.retry_backoff = max(
            0.0,
            env_float(
                "OPENROUTER_RETRY_BACKOFF_SECONDS",
                float(config_openrouter("retryBackoffSeconds") or DEFAULT_RETRY_BACKOFF_SECONDS),
            ),
        )
        self.use_reasoning = True
        schema_setting = os.environ.get("OPENROUTER_USE_JSON_SCHEMA")
        if schema_setting is None:
            schema_setting = config_openrouter("useJsonSchema")
        self.use_schema = str(schema_setting).strip().lower() in {"1", "true", "yes", "on"}
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
        elif lowered in {"maxattempts", "max_attempts"}:
            parsed = clamp_max_attempts(value, self.max_attempts)
            if str(parsed) != value.strip():
                log(f"clamped MaxAttempts option {value!r} to {parsed}")
            self.max_attempts = parsed
            self.max_retries = max(0, self.max_attempts - 1)
        elif lowered == "maxretries":
            try:
                self.max_attempts = max(1, min(MAX_ATTEMPTS_CEILING, int(value) + 1))
                self.max_retries = max(0, self.max_attempts - 1)
            except ValueError:
                log(f"invalid max retries option: {value!r}")
        elif lowered in {"maxtokens", "max_tokens"}:
            self.max_tokens = clamp_max_tokens(value, self.max_tokens)
        elif lowered in {"reasoning", "reasoningeffort", "reasoning_effort"} and value:
            self.reasoning_effort = value.strip()
        elif lowered in {"providersort", "provider_sort"} and value:
            self.provider_sort = value.strip()
        elif lowered in {"usejsonschema", "use_json_schema"}:
            self.use_schema = value.strip().lower() in {"1", "true", "yes", "on"}

    def choose_move(self, board: chess.Board, go_args: dict, history: list[str]) -> tuple[str, str]:
        legal_moves = [move.uci() for move in board.legal_moves]
        if not legal_moves:
            return "0000", "no legal moves"

        remaining = go_args.get("wtime") if board.turn == chess.WHITE else go_args.get("btime")
        if remaining is not None and remaining <= 0:
            side = "White" if board.turn == chess.WHITE else "Black"
            return forfeit_move(f"{side} clock expired; forfeiting without calling OpenRouter")

        api_key = os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            return forfeit_move("OPENROUTER_API_KEY is not set; forfeiting")

        timeout = self._timeout_seconds(go_args, remaining)
        last_error = None
        attempt = 0
        downgrades = 0
        while attempt < self.max_attempts:
            attempt += 1
            payload = self._build_payload(board, go_args, history, legal_moves)
            try:
                data = self._post_with_deadline(api_key, payload, timeout)
                move, comment = self._parse_response(data, legal_moves)
                if move in legal_moves:
                    self.invalid_model_moves = 0
                    return move, comment
                last_error = f"illegal move {move!r}"
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")[:500]
                last_error = f"HTTP {exc.code}: {body}"
                lowered = body.lower()
                if exc.code == 400:
                    downgraded = False
                    if self.use_reasoning and "reasoning" in lowered:
                        self.use_reasoning = False
                        downgraded = True
                        log("endpoint rejected reasoning control; retrying without it")
                    if self.use_schema and ("response_format" in lowered or "json_schema" in lowered or "schema" in lowered):
                        self.use_schema = False
                        downgraded = True
                        log("endpoint rejected json_schema; retrying without it")
                    if downgraded and downgrades < 2:
                        downgrades += 1
                        attempt -= 1
                        continue
                if exc.code in {429, 500, 502, 503, 504} and attempt < self.max_attempts:
                    delay = self._retry_backoff(attempt, exc)
                    if delay > 0:
                        log(f"transient HTTP {exc.code}; backing off {delay:.1f}s before attempt {attempt + 1}")
                        time.sleep(delay)
            except EmptyModelResponse as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < self.max_attempts and self.max_tokens < MAX_TOKENS_CEILING:
                    self.max_tokens = min(MAX_TOKENS_CEILING, self.max_tokens * 2)
                    log(f"empty or truncated model response; raising MaxTokens to {self.max_tokens}")
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            self.invalid_model_moves += 1
            log(
                f"OpenRouter move attempt {attempt}/{self.max_attempts} failed: {last_error} "
                f"(invalid_count={self.invalid_model_moves})"
            )

        return forfeit_move(
            f"OpenRouter {self.model} failed after {self.max_attempts} attempts; forfeiting ({last_error})"
        )

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

    def _retry_backoff(self, attempt: int, exc: urllib.error.HTTPError) -> float:
        delay = self.retry_backoff * (2 ** max(0, attempt - 1))
        retry_after = exc.headers.get("Retry-After") if exc.headers else None
        if retry_after:
            try:
                delay = max(delay, float(retry_after))
            except ValueError:
                pass
        return min(delay, MAX_RETRY_BACKOFF_SECONDS)

    def _post_with_deadline(self, api_key: str, payload: dict, timeout: int) -> dict:
        pool = ThreadPoolExecutor(max_workers=1)
        future = pool.submit(self._post, api_key, payload, timeout)
        try:
            return future.result(timeout=timeout + 3)
        except FutureTimeoutError:
            future.cancel()
            raise TimeoutError(f"OpenRouter request exceeded {timeout + 3}s wall-clock limit")
        finally:
            pool.shutdown(wait=False)

    def _build_payload(
        self,
        board: chess.Board,
        go_args: dict,
        history: list[str],
        legal_moves: list[str],
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
                        "You are a UCI chess engine. Return only a JSON object of the form "
                        '{"uci": "<move>", "comment": "<short reason>"}. '
                        "The uci field must be copied exactly from the supplied legal_moves list."
                    ),
                },
                {"role": "user", "content": json.dumps(prompt, separators=(",", ":"))},
            ],
            "temperature": self.temperature / 100,
            "max_tokens": self.max_tokens,
        }
        if self.provider_sort:
            payload["provider"] = {"sort": self.provider_sort, "allow_fallbacks": True}
        if self.use_reasoning and self.reasoning_effort.lower() not in {"", "none", "off", "false"}:
            payload["reasoning"] = {"effort": self.reasoning_effort}
        if self.use_schema:
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
        choice = choices[0] if isinstance(choices[0], dict) else {}
        message = choice.get("message", {}) if isinstance(choice.get("message"), dict) else {}
        content = message.get("content", "")
        if content is None:
            content = ""
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        text = str(content).strip()
        if not text:
            finish = choice.get("finish_reason")
            reasoning_len = len(str(message.get("reasoning") or ""))
            if finish == "length":
                raise EmptyModelResponse(
                    f"model hit the token limit before emitting JSON (reasoning_chars={reasoning_len}); "
                    "increase MaxTokens or lower reasoning effort"
                )
            raise EmptyModelResponse(f"model returned empty content (finish_reason={finish})")
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
                print("option name MaxAttempts type spin default 3 min 1 max 9", flush=True)
                print("option name MaxTokens type spin default 1500 min 64 max 8192", flush=True)
                print("option name Reasoning type string default low", flush=True)
                print("option name ProviderSort type string default throughput", flush=True)
                print("option name UseJsonSchema type check default false", flush=True)
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
