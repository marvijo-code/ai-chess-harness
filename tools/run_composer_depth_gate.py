"""Run Composer-chess vs Stockfish fixed-depth gate matches with optional live PGN."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import chess
import chess.pgn

ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))
from live_pgn_viewer import tournament_slug, write_depth_match_live_state

ENGINE_CONFIG = Path(os.environ["APPDATA"]) / "org.encroissant.app" / "engines" / "engines.json"
COMPOSER_CMD = ROOT / "engines" / "composer-chess" / "composer-chess.cmd"
MATCH_DIR = ROOT / "out" / "composer-depth-matches"
LIVE_DIR = ROOT / "out" / "live"
WISDOM_PATH = ROOT / "engines" / "composer-chess" / "composer-wisdom.md"
DEFAULT_VIEWER_PORT = 8879
INFO_STRING_RE = re.compile(r"^info string (.+)$")


class UciEngine:
    def __init__(
        self,
        name: str,
        path: Path,
        movetime_ms: int | None = None,
        depth: int | None = None,
        options: dict[str, str | bool | int] | None = None,
    ) -> None:
        self.name = name
        self.path = path
        self.movetime_ms = movetime_ms
        self.depth = depth
        self.proc = subprocess.Popen(
            [str(path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self._cmd("uci")
        self._read_until("uciok", 30)
        for key, value in (options or {}).items():
            val = "true" if value is True else "false" if value is False else str(value)
            self._cmd(f"setoption name {key} value {val}")
        self._cmd("isready")
        self._read_until("readyok", 30)

    def _cmd(self, line: str) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()

    def _read_until(self, marker: str, timeout: int) -> list[str]:
        assert self.proc.stdout is not None
        deadline = time.time() + timeout
        lines: list[str] = []
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if line == "":
                raise RuntimeError(f"{self.name} exited unexpectedly")
            line = line.rstrip("\n")
            lines.append(line)
            if marker in line:
                return lines
        raise TimeoutError(f"{self.name} timed out waiting for {marker!r}")

    def new_game(self) -> None:
        self._cmd("ucinewgame")
        self._cmd("isready")
        self._read_until("readyok", 30)

    def bestmove(self, board: chess.Board) -> tuple[chess.Move, str]:
        self._cmd(f"position fen {board.fen()}")
        if self.depth is not None:
            self._cmd(f"go depth {self.depth}")
            timeout = max(60, self.depth * 8)
        else:
            ms = self.movetime_ms or 1000
            self._cmd(f"go movetime {ms}")
            timeout = max(30, ms // 1000 + 60)
        lines = self._read_until("bestmove", timeout)
        thought = ""
        if self.depth is None:
            for line in lines:
                match = INFO_STRING_RE.match(line)
                if match:
                    thought = match.group(1).strip()
        best_line = next(line for line in reversed(lines) if line.startswith("bestmove "))
        uci = best_line.split()[1]
        if uci == "0000":
            raise RuntimeError(f"{self.name} forfeited")
        move = chess.Move.from_uci(uci)
        if move not in board.legal_moves:
            raise RuntimeError(f"{self.name} illegal move {uci} for {board.fen()}")
        if not thought and self.depth is not None:
            thought = f"Stockfish depth {self.depth} search."
        return move, thought

    def close(self) -> None:
        if self.proc.poll() is None:
            try:
                self._cmd("quit")
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()


def load_stockfish() -> Path:
    engines = json.loads(ENGINE_CONFIG.read_text(encoding="utf-8"))
    for engine in engines:
        if engine.get("name", "").lower() == "stockfish" and engine.get("enabled"):
            path = Path(engine["path"])
            if path.exists():
                return path
    raise RuntimeError(f"Stockfish not found in {ENGINE_CONFIG}")


def live_viewer_url(live_pgn_path: Path, port: int = DEFAULT_VIEWER_PORT) -> str:
    slug = tournament_slug(live_pgn_path)
    return f"http://127.0.0.1:{port}/#{slug}--live-game-1"


def append_wisdom(depth: int, result: str, archive_path: Path, composer_white: bool) -> None:
    won = (result == "1-0" and composer_white) or (result == "0-1" and not composer_white)
    outcome = "win" if won else ("draw" if result == "1/2-1/2" else "loss")
    stamp = time.strftime("%Y-%m-%d")
    block = f"""
### {stamp} — Depth {depth} gate ({outcome})

**Hypothesis:** Out-search fixed depth-{depth} Stockfish; persist reasoning as PGN comments after each Composer move.

**Change:** Live PGN rewritten with {{comment}} on every ply — visible on the board without log APIs.

**Result:** {result} — `{archive_path.name}`

**Lesson (general):** If the operator cannot see thinking, the fix is the PGN file, not another API.

**Next:** {"Tighten repetition/convert logic, rerun gate" if not won else "Validate depth-8 win and promote principle."}
"""
    WISDOM_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not WISDOM_PATH.exists():
        WISDOM_PATH.write_text(
            "# Composer-chess wisdom\n\nFirst-principles lessons. General concepts only.\n\n## Log\n",
            encoding="utf-8",
        )
    with WISDOM_PATH.open("a", encoding="utf-8") as handle:
        handle.write(block)


def play_gate(
    depth: int,
    composer_ms: int,
    composer_white: bool,
    live_pgn_path: Path | None,
    max_plies: int,
) -> tuple[chess.pgn.Game, str, Path]:
    depth = max(1, depth)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    archive_path = MATCH_DIR / f"composer-vs-stockfish-depth-{depth}-{stamp}.pgn"
    game_start = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S %z")

    composer = UciEngine("Composer-chess", COMPOSER_CMD, movetime_ms=composer_ms)
    stockfish = UciEngine(
        f"Stockfish depth {depth}",
        load_stockfish(),
        depth=depth,
        options={"Threads": 1, "Hash": 16},
    )
    composer.new_game()
    stockfish.new_game()

    board = chess.Board()
    game = chess.pgn.Game()
    game.headers["Event"] = f"Composer-chess vs Stockfish depth {depth}"
    game.headers["Site"] = str(ROOT)
    game.headers["Date"] = time.strftime("%Y.%m.%d")
    game.headers["Round"] = "1"
    game.headers["White"] = "Composer-chess" if composer_white else f"Stockfish depth {depth}"
    game.headers["Black"] = f"Stockfish depth {depth}" if composer_white else "Composer-chess"
    game.headers["StockfishDepth"] = str(depth)
    game.headers["GameStartTime"] = game_start
    game.headers["Result"] = "*"
    node = game
    write_depth_match_live_state(live_pgn_path, game, completed=False)

    try:
        for _ in range(max_plies):
            if board.is_game_over(claim_draw=True):
                break
            is_composer = (board.turn == chess.WHITE) == composer_white
            engine = composer if is_composer else stockfish
            move, thought = engine.bestmove(board)
            node = node.add_variation(move)
            if thought:
                node.comment = thought[:500]
            board.push(move)
            write_depth_match_live_state(live_pgn_path, game, completed=False)

        if board.is_game_over(claim_draw=True):
            result = board.result(claim_draw=True)
            termination = str(board.outcome(claim_draw=True).termination.name).replace("_", " ")
        else:
            result = "1/2-1/2"
            termination = f"max plies {max_plies}"
        game.headers["Result"] = result
        game.headers["Termination"] = termination
        game.headers["GameEndTime"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S %z")
        write_depth_match_live_state(live_pgn_path, game, completed=True)
        MATCH_DIR.mkdir(parents=True, exist_ok=True)
        archive_path.write_text(str(game) + "\n\n", encoding="utf-8")
        append_wisdom(depth, result, archive_path, composer_white)
        return game, result, archive_path
    finally:
        composer.close()
        stockfish.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--depth", type=int, default=8)
    parser.add_argument("--composer-ms", type=int, default=3000)
    parser.add_argument("--composer-white", action="store_true", default=True)
    parser.add_argument("--composer-black", action="store_true")
    parser.add_argument("--live-pgn", type=Path)
    parser.add_argument("--viewer-port", type=int, default=DEFAULT_VIEWER_PORT)
    parser.add_argument("--max-plies", type=int, default=200)
    args = parser.parse_args()

    composer_white = not args.composer_black
    live_pgn = args.live_pgn.resolve() if args.live_pgn else None
    live_url = live_viewer_url(live_pgn, args.viewer_port) if live_pgn else None
    if live_url:
        print(f"Live viewer: {live_url}", flush=True)

    _, result, archive_path = play_gate(
        args.depth,
        args.composer_ms,
        composer_white,
        live_pgn,
        args.max_plies,
    )

    composer_side = chess.WHITE if composer_white else chess.BLACK
    won = (result == "1-0" and composer_side == chess.WHITE) or (
        result == "0-1" and composer_side == chess.BLACK
    )
    print(f"Result: {result} -> {archive_path.name}")
    summary = {
        "depth": args.depth,
        "result": result,
        "won": won,
        "archive_pgn": str(archive_path),
        "live_viewer_url": live_url,
        "live_pgn_path": str(live_pgn) if live_pgn else None,
        "viewer_port": args.viewer_port if live_pgn else None,
        "goal_met": won and args.depth >= 8,
    }
    print(json.dumps(summary, indent=2))
    sys.exit(0 if summary["goal_met"] else 1)


if __name__ == "__main__":
    main()
