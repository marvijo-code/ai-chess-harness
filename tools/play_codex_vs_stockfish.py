import argparse
import asyncio
import json
import re
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import chess
import chess.pgn
from PIL import Image, ImageDraw, ImageFont
import websockets
from websockets.exceptions import ConnectionClosed

from harness_config import config_value


ROOT = Path(__file__).resolve().parents[1]
ENGINE_CONFIG = Path.home() / "AppData/Roaming/org.encroissant.app/engines/engines.json"
OUT_DIR = ROOT / "out"
DEFAULT_LIVE_PGN_PATH = OUT_DIR / "live" / "codex-vs-stockfish-live.pgn"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def load_stockfish_path() -> Path:
    engines = json.loads(ENGINE_CONFIG.read_text(encoding="utf-8"))
    for engine in engines:
        if engine.get("name", "").lower() == "stockfish" and engine.get("enabled"):
            path = Path(engine["path"])
            if path.exists():
                return path
    raise RuntimeError(f"No enabled Stockfish executable found in {ENGINE_CONFIG}")


class Stockfish:
    def __init__(self, exe: Path, movetime_ms: int):
        self.exe = exe
        self.movetime_ms = movetime_ms
        self.proc = subprocess.Popen(
            [str(exe)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self.command("uci")
        self.read_until("uciok")
        self.command("setoption name Threads value 1")
        self.command("setoption name Hash value 16")
        self.command("isready")
        self.read_until("readyok")

    def command(self, line: str) -> None:
        if self.proc.stdin is None:
            raise RuntimeError("Stockfish stdin is closed")
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()

    def read_until(self, marker: str) -> list[str]:
        if self.proc.stdout is None:
            raise RuntimeError("Stockfish stdout is closed")
        lines = []
        while True:
            line = self.proc.stdout.readline()
            if line == "":
                raise RuntimeError("Stockfish exited unexpectedly")
            line = line.rstrip("\n")
            lines.append(line)
            if marker in line:
                return lines

    def bestmove(self, board: chess.Board) -> chess.Move:
        self.command(f"position fen {board.fen()}")
        self.command(f"go movetime {self.movetime_ms}")
        lines = self.read_until("bestmove")
        best = lines[-1].split()[1]
        return chess.Move.from_uci(best)

    def close(self) -> None:
        if self.proc.poll() is None:
            try:
                self.command("quit")
                self.proc.wait(timeout=3)
            except Exception:
                self.proc.kill()


@dataclass
class CodexMove:
    uci: str
    explanation: str


class CodexAppServer:
    def __init__(self, model: str, effort: str):
        self.model = model
        self.effort = effort
        self.port = free_port()
        self.url = f"ws://127.0.0.1:{self.port}"
        self.proc: asyncio.subprocess.Process | None = None
        self.ws = None
        self.next_id = 1
        self.pending: dict[int, asyncio.Future] = {}
        self.turn_text: dict[str, str] = {}
        self.turn_done: dict[str, asyncio.Future] = {}
        self.thread_id: str | None = None

    async def start(self) -> None:
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
                "clientInfo": {"name": "chess-harness-codex", "version": "0.1.0"},
                "capabilities": None,
            },
        )
        print(f"Codex app-server: {init['userAgent']}")

        started = await self.request(
            "thread/start",
            {
                "cwd": str(ROOT),
                "model": self.model,
                "approvalPolicy": "never",
                "sandbox": "read-only",
                "ephemeral": True,
                "baseInstructions": (
                    "You are playing a legal chess game from the side to move. "
                    "Return only JSON matching the schema. Never call tools."
                ),
                "developerInstructions": (
                    "Choose exactly one UCI move from the supplied legal_moves list. "
                    "No prose outside JSON. Do not use tools or browse."
                ),
            },
        )
        self.thread_id = started["thread"]["id"]
        print(
            "Codex thread:",
            self.thread_id,
            "model=" + started["model"],
            "provider=" + started["modelProvider"],
            "serviceTier=" + str(started.get("serviceTier")),
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
                    plan = limits.get("planType")
                    credits = limits.get("credits")
                    print(f"Codex account: planType={plan} credits={credits}")
        except ConnectionClosed:
            return

    async def request(self, method: str, params: dict) -> dict:
        request_id = self.next_id
        self.next_id += 1
        fut = asyncio.get_running_loop().create_future()
        self.pending[request_id] = fut
        await self.ws.send(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}))
        return await fut

    async def choose_move(self, board: chess.Board, pgn_text: str) -> CodexMove:
        if self.thread_id is None:
            raise RuntimeError("Codex thread is not started")
        legal_moves = [move.uci() for move in board.legal_moves]
        prompt = {
            "side_to_move": "white" if board.turn == chess.WHITE else "black",
            "fen": board.fen(),
            "legal_moves": legal_moves,
            "pgn_so_far": pgn_text,
            "task": "Pick one legal move from legal_moves. Prefer sound development, king safety, and tactics.",
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
                    "required": ["uci", "explanation"],
                    "properties": {
                        "uci": {"type": "string"},
                        "explanation": {"type": "string"},
                    },
                },
            },
        )
        turn_id = response["turn"]["id"]
        done = asyncio.get_running_loop().create_future()
        self.turn_done[turn_id] = done
        await asyncio.wait_for(done, timeout=120)
        text = self.turn_text.get(turn_id, "").strip()
        data = parse_json_object(text)
        move = data.get("uci", "")
        if move not in legal_moves:
            raise RuntimeError(f"Codex returned illegal move {move!r}; response was {text!r}")
        return CodexMove(uci=move, explanation=data.get("explanation", ""))

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


def parse_json_object(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if not match:
            raise
        return json.loads(match.group(0))


def game_to_pgn(game: chess.pgn.Game) -> str:
    return str(game)


def write_live_pgn(path: Path | None, game: chess.pgn.Game) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(game_to_pgn(game) + "\n", encoding="utf-8")


def render_board_png(board: chess.Board, png_path: Path, title: str, footer: str) -> None:
    size = 88
    margin_top = 90
    margin_bottom = 70
    margin_side = 54
    board_px = size * 8
    image = Image.new("RGB", (board_px + margin_side * 2, board_px + margin_top + margin_bottom), "#f5f2ec")
    draw = ImageDraw.Draw(image)
    font_path = Path("C:/Windows/Fonts/seguisym.ttf")
    text_font_path = Path("C:/Windows/Fonts/segoeui.ttf")
    piece_font = ImageFont.truetype(str(font_path), 52)
    title_font = ImageFont.truetype(str(text_font_path), 24)
    small_font = ImageFont.truetype(str(text_font_path), 15)
    label_font = ImageFont.truetype(str(text_font_path), 16)

    draw.text((margin_side, 24), title, fill="#1f2933", font=title_font)
    draw.text((margin_side, 56), footer, fill="#52606d", font=small_font)

    light = "#e9d8b4"
    dark = "#92724a"
    white_piece = "#f8fafc"
    black_piece = "#202124"
    outline = "#111827"
    glyphs = {
        chess.Piece(chess.KING, chess.WHITE): "\u2654",
        chess.Piece(chess.QUEEN, chess.WHITE): "\u2655",
        chess.Piece(chess.ROOK, chess.WHITE): "\u2656",
        chess.Piece(chess.BISHOP, chess.WHITE): "\u2657",
        chess.Piece(chess.KNIGHT, chess.WHITE): "\u2658",
        chess.Piece(chess.PAWN, chess.WHITE): "\u2659",
        chess.Piece(chess.KING, chess.BLACK): "\u265A",
        chess.Piece(chess.QUEEN, chess.BLACK): "\u265B",
        chess.Piece(chess.ROOK, chess.BLACK): "\u265C",
        chess.Piece(chess.BISHOP, chess.BLACK): "\u265D",
        chess.Piece(chess.KNIGHT, chess.BLACK): "\u265E",
        chess.Piece(chess.PAWN, chess.BLACK): "\u265F",
    }

    for rank in range(8):
        for file in range(8):
            x = margin_side + file * size
            y = margin_top + rank * size
            square_color = light if (rank + file) % 2 == 0 else dark
            draw.rectangle((x, y, x + size, y + size), fill=square_color)
            square = chess.square(file, 7 - rank)
            piece = board.piece_at(square)
            if piece:
                glyph = glyphs[piece]
                bbox = draw.textbbox((0, 0), glyph, font=piece_font)
                tx = x + (size - (bbox[2] - bbox[0])) / 2
                ty = y + (size - (bbox[3] - bbox[1])) / 2 - 5
                fill = white_piece if piece.color == chess.WHITE else black_piece
                if piece.color == chess.WHITE:
                    for dx, dy in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
                        draw.text((tx + dx, ty + dy), glyph, fill=outline, font=piece_font)
                draw.text((tx, ty), glyph, fill=fill, font=piece_font)

    for i, file_char in enumerate("abcdefgh"):
        x = margin_side + i * size + size / 2 - 5
        draw.text((x, margin_top + board_px + 8), file_char, fill="#334e68", font=label_font)
    for i, rank_char in enumerate("87654321"):
        y = margin_top + i * size + size / 2 - 10
        draw.text((margin_side - 28, y), rank_char, fill="#334e68", font=label_font)

    png_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(png_path)


async def play(args: argparse.Namespace) -> dict:
    OUT_DIR.mkdir(exist_ok=True)
    stockfish_path = load_stockfish_path()
    engine = Stockfish(stockfish_path, args.stockfish_movetime_ms)
    codex = CodexAppServer(args.model, args.effort)
    await codex.start()

    board = chess.Board()
    game = chess.pgn.Game()
    game.headers["Event"] = "Codex App-server vs Stockfish"
    game.headers["Site"] = "C:/dev/chess-harness-codex"
    game.headers["Date"] = time.strftime("%Y.%m.%d")
    game.headers["White"] = "Codex App-server"
    game.headers["Black"] = "Stockfish 18"
    game.headers["Result"] = "*"
    node = game
    moves_log = []
    write_live_pgn(args.live_pgn_path, game)

    try:
        for ply in range(args.max_plies):
            if board.is_game_over(claim_draw=True):
                break
            pgn_so_far = game_to_pgn(game)
            if board.turn == chess.WHITE:
                codex_move = await codex.choose_move(board, pgn_so_far)
                move = chess.Move.from_uci(codex_move.uci)
                source = f"Codex: {codex_move.explanation}"
            else:
                move = engine.bestmove(board)
                source = "Stockfish"
            san = board.san(move)
            board.push(move)
            node = node.add_variation(move)
            moves_log.append({"ply": ply + 1, "uci": move.uci(), "san": san, "source": source, "fen_after": board.fen()})
            write_live_pgn(args.live_pgn_path, game)
            print(f"{ply + 1:02d}. {source.split(':', 1)[0]} {san} ({move.uci()})")

        result = board.result(claim_draw=True) if board.is_game_over(claim_draw=True) else "*"
        game.headers["Result"] = result
        write_live_pgn(args.live_pgn_path, game)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        pgn_path = OUT_DIR / f"codex-vs-stockfish-{stamp}.pgn"
        json_path = OUT_DIR / f"codex-vs-stockfish-{stamp}.json"
        png_path = OUT_DIR / f"codex-vs-stockfish-{stamp}.png"
        pgn_path.write_text(game_to_pgn(game), encoding="utf-8")
        summary = {
            "result": result,
            "completed": board.is_game_over(claim_draw=True),
            "termination": board.outcome(claim_draw=True).termination.name if board.outcome(claim_draw=True) else "max_plies",
            "plies": len(moves_log),
            "fen": board.fen(),
            "stockfish": str(stockfish_path),
            "pgn": str(pgn_path),
            "live_pgn": str(args.live_pgn_path) if args.live_pgn_path else None,
            "png": str(png_path),
            "moves": moves_log,
        }
        json_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        render_board_png(
            board,
            png_path,
            "Codex App-server vs Stockfish",
            f"Result {result} | plies {len(moves_log)} | {summary['termination']}",
        )
        summary["json"] = str(json_path)
        return summary
    finally:
        engine.close()
        await codex.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=str(config_value("codex.model", "gpt-5.3-codex")))
    parser.add_argument("--effort", default=str(config_value("codex.effort", "high")))
    parser.add_argument("--max-plies", type=int, default=80)
    parser.add_argument("--stockfish-movetime-ms", type=int, default=150)
    parser.add_argument("--live-pgn-path", type=Path, default=DEFAULT_LIVE_PGN_PATH)
    parser.add_argument("--no-live-pgn", dest="live_pgn_path", action="store_const", const=None)
    args = parser.parse_args()
    summary = asyncio.run(play(args))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
