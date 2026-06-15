"""Run Composer-chess vs Stockfish UCI matches and report results."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
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
OUT_DIR = ROOT / "out" / "composer-matches"
LIVE_DIR = ROOT / "out" / "live"
DEFAULT_VIEWER_PORT = 8878


class UciEngine:
    def __init__(
        self,
        name: str,
        path: Path,
        movetime_ms: int,
        options: dict[str, str | bool | int] | None = None,
    ) -> None:
        self.name = name
        self.path = path
        self.movetime_ms = movetime_ms
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
            if isinstance(value, bool):
                val = "true" if value else "false"
            else:
                val = str(value)
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

    def bestmove(self, board: chess.Board) -> chess.Move:
        self._cmd(f"position fen {board.fen()}")
        self._cmd(f"go movetime {self.movetime_ms}")
        lines = self._read_until("bestmove", max(30, self.movetime_ms // 1000 + 60))
        best_line = next(line for line in reversed(lines) if line.startswith("bestmove "))
        uci = best_line.split()[1]
        if uci == "0000":
            raise RuntimeError(f"{self.name} forfeited")
        move = chess.Move.from_uci(uci)
        if move not in board.legal_moves:
            raise RuntimeError(f"{self.name} illegal move {uci} for {board.fen()}")
        return move

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


def play_game(
    composer_white: bool,
    composer_ms: int,
    stockfish_ms: int,
    stockfish_elo: int | None,
    live_pgn_path: Path | None = None,
) -> tuple[chess.pgn.Game, str]:
    sf_opts: dict[str, str | bool | int] = {"Threads": 1, "Hash": 16}
    if stockfish_elo is not None:
        sf_opts["UCI_LimitStrength"] = True
        sf_opts["UCI_Elo"] = stockfish_elo

    composer = UciEngine("Composer-chess", COMPOSER_CMD, composer_ms)
    stockfish = UciEngine("Stockfish", load_stockfish(), stockfish_ms, options=sf_opts)
    composer.new_game()
    stockfish.new_game()

    board = chess.Board()
    game = chess.pgn.Game()
    game.headers["Event"] = "Composer-chess vs Stockfish"
    game.headers["Site"] = str(ROOT)
    game.headers["Date"] = time.strftime("%Y.%m.%d")
    game.headers["White"] = "Composer-chess" if composer_white else "Stockfish"
    game.headers["Black"] = "Stockfish" if composer_white else "Composer-chess"
    if stockfish_elo is not None:
        game.headers["StockfishElo"] = str(stockfish_elo)
    game.headers["Result"] = "*"
    node = game
    write_depth_match_live_state(live_pgn_path, game, completed=False)

    try:
        while not board.is_game_over(claim_draw=True) and board.fullmove_number <= 200:
            engine = composer if (board.turn == chess.WHITE) == composer_white else stockfish
            move = engine.bestmove(board)
            node = node.add_variation(move)
            board.push(move)
            write_depth_match_live_state(live_pgn_path, game, completed=False)
        result = board.result(claim_draw=True) if board.is_game_over(claim_draw=True) else "1/2-1/2"
        game.headers["Result"] = result
        if not board.is_game_over(claim_draw=True):
            game.headers["Termination"] = "max plies"
        write_depth_match_live_state(live_pgn_path, game, completed=True)
        return game, result
    finally:
        composer.close()
        stockfish.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--games", type=int, default=1)
    parser.add_argument("--composer-ms", type=int, default=2000)
    parser.add_argument("--stockfish-ms", type=int, default=300)
    parser.add_argument("--stockfish-elo", type=int, default=1200)
    parser.add_argument("--no-elo-limit", action="store_true")
    parser.add_argument("--composer-white", action="store_true", default=True)
    parser.add_argument("--live-pgn", type=Path, help="Mirror in-progress moves to this live PGN path.")
    parser.add_argument("--viewer-port", type=int, default=DEFAULT_VIEWER_PORT)
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    elo = None if args.no_elo_limit else args.stockfish_elo
    wins = losses = draws = 0
    live_url = None
    if args.live_pgn is not None:
        live_url = live_viewer_url(args.live_pgn.resolve(), args.viewer_port)
        print(f"Live viewer: {live_url}", flush=True)

    for i in range(args.games):
        live_pgn = args.live_pgn.resolve() if args.live_pgn is not None else None
        if live_pgn is not None and args.games > 1:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            live_pgn = live_pgn.with_name(f"{live_pgn.stem}-game{i + 1}{live_pgn.suffix}")
        game, result = play_game(
            args.composer_white,
            args.composer_ms,
            args.stockfish_ms,
            elo,
            live_pgn_path=live_pgn,
        )
        stamp = time.strftime("%Y%m%d-%H%M%S")
        tag = f"elo{elo}" if elo else "full"
        pgn_path = OUT_DIR / f"composer-vs-sf-{tag}-{stamp}.pgn"
        pgn_path.write_text(str(game), encoding="utf-8")
        composer_side = chess.WHITE if args.composer_white else chess.BLACK
        if result == "1/2-1/2":
            draws += 1
            print(f"Game {i + 1}: draw -> {pgn_path.name}")
        elif result == "1-0":
            if composer_side == chess.WHITE:
                wins += 1
                print(f"Game {i + 1}: WIN ({result}) -> {pgn_path.name}")
            else:
                losses += 1
                print(f"Game {i + 1}: loss ({result}) -> {pgn_path.name}")
        elif result == "0-1":
            if composer_side == chess.BLACK:
                wins += 1
                print(f"Game {i + 1}: WIN ({result}) -> {pgn_path.name}")
            else:
                losses += 1
                print(f"Game {i + 1}: loss ({result}) -> {pgn_path.name}")
        else:
            draws += 1
            print(f"Game {i + 1}: unknown result {result} -> {pgn_path.name}")

    summary = {
        "wins": wins,
        "losses": losses,
        "draws": draws,
        "stockfish_elo": elo,
        "live_viewer_url": live_url,
        "live_pgn_path": str(args.live_pgn.resolve()) if args.live_pgn else None,
        "viewer_port": args.viewer_port if args.live_pgn else None,
    }
    print(json.dumps(summary, indent=2))
    sys.exit(0 if wins > 0 else 1)


if __name__ == "__main__":
    main()
