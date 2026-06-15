"""Run Wisdom-chess vs Stockfish fixed-depth gate matches."""

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
WISDOM_CMD = ROOT / "engines" / "wisdom-chess" / "wisdom-chess.cmd"
WISDOM_PURE_CMD = ROOT / "engines" / "wisdom-chess" / "wisdom-chess-pure.cmd"
MATCH_DIR = ROOT / "out" / "wisdom-depth-matches"
WISDOM_LOG = ROOT / "engines" / "wisdom-chess" / "wisdom-gate-log.md"
DEFAULT_VIEWER_PORT = 8880
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
            timeout = max(90, self.depth * 12)
        else:
            ms = self.movetime_ms or 2000
            self._cmd(f"go movetime {ms}")
            timeout = max(60, ms // 1000 + 120)
        lines = self._read_until("bestmove", timeout)
        thought = ""
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


def append_gate_log(depth: int, result: str, archive_path: Path, wisdom_white: bool) -> None:
    won = (result == "1-0" and wisdom_white) or (result == "0-1" and not wisdom_white)
    outcome = "win" if won else ("draw" if result == "1/2-1/2" else "loss")
    stamp = time.strftime("%Y-%m-%d")
    block = f"""
### {stamp} — Depth {depth} gate ({outcome})

**Result:** {result} — `{archive_path.name}`

**Side:** Wisdom-chess as {"White" if wisdom_white else "Black"}

**Next:** {"Goal met at depth 8." if won and depth >= 8 else "Rerun with adjusted movetime or both colors."}
"""
    WISDOM_LOG.parent.mkdir(parents=True, exist_ok=True)
    if not WISDOM_LOG.exists():
        WISDOM_LOG.write_text("# Wisdom-chess gate log\n\n## Log\n", encoding="utf-8")
    with WISDOM_LOG.open("a", encoding="utf-8") as handle:
        handle.write(block)


def play_gate(
    depth: int,
    wisdom_ms: int,
    wisdom_white: bool,
    live_pgn_path: Path | None,
    max_plies: int,
    wisdom_cmd: Path | None = None,
) -> tuple[chess.pgn.Game, str, Path]:
    depth = max(1, depth)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    archive_path = MATCH_DIR / f"wisdom-vs-stockfish-depth-{depth}-{stamp}.pgn"
    game_start = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S %z")

    engine_cmd = wisdom_cmd or WISDOM_CMD
    wisdom = UciEngine("Wisdom-chess", engine_cmd, movetime_ms=wisdom_ms)
    stockfish = UciEngine(
        f"Stockfish depth {depth}",
        load_stockfish(),
        depth=depth,
        options={"Threads": 1, "Hash": 16},
    )
    wisdom.new_game()
    stockfish.new_game()

    board = chess.Board()
    game = chess.pgn.Game()
    game.headers["Event"] = f"Wisdom-chess vs Stockfish depth {depth}"
    game.headers["Site"] = str(ROOT)
    game.headers["Date"] = time.strftime("%Y.%m.%d")
    game.headers["Round"] = "1"
    game.headers["White"] = "Wisdom-chess" if wisdom_white else f"Stockfish depth {depth}"
    game.headers["Black"] = f"Stockfish depth {depth}" if wisdom_white else "Wisdom-chess"
    game.headers["StockfishDepth"] = str(depth)
    game.headers["WisdomMovetimeMs"] = str(wisdom_ms)
    game.headers["WisdomEngine"] = "pure" if (engine_cmd == WISDOM_PURE_CMD or engine_cmd == WISDOM_CMD) else "hybrid"
    game.headers["GameStartTime"] = game_start
    game.headers["Result"] = "*"
    node = game
    write_depth_match_live_state(live_pgn_path, game, completed=False)

    try:
        for ply in range(max_plies):
            if board.is_game_over(claim_draw=True):
                break
            is_wisdom = (board.turn == chess.WHITE) == wisdom_white
            engine = wisdom if is_wisdom else stockfish
            move, thought = engine.bestmove(board)
            node = node.add_variation(move)
            if thought and is_wisdom:
                node.comment = thought[:500]
            board.push(move)
            write_depth_match_live_state(live_pgn_path, game, completed=False)
            print(f"ply {ply + 1}: {move.uci()} ({'Wisdom' if is_wisdom else 'SF'})", flush=True)

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
        append_gate_log(depth, result, archive_path, wisdom_white)
        return game, result, archive_path
    finally:
        wisdom.close()
        stockfish.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--depth", type=int, default=8)
    parser.add_argument("--wisdom-ms", type=int, default=18000)
    parser.add_argument("--wisdom-white", action="store_true", default=True)
    parser.add_argument("--wisdom-black", action="store_true")
    parser.add_argument("--hybrid-sf", action="store_true", help="Use SF backend cmd (not for climb ladder)")
    parser.add_argument("--live-pgn", type=Path)
    parser.add_argument("--viewer-port", type=int, default=DEFAULT_VIEWER_PORT)
    parser.add_argument("--max-plies", type=int, default=200)
    args = parser.parse_args()

    wisdom_white = not args.wisdom_black
    live_pgn = args.live_pgn.resolve() if args.live_pgn else None
    if live_pgn:
        slug = tournament_slug(live_pgn)
        print(f"Live viewer: http://127.0.0.1:{args.viewer_port}/#{slug}--live-game-1", flush=True)

    wisdom_cmd = ROOT / "engines" / "wisdom-chess" / "wisdom-chess-sf.cmd" if args.hybrid_sf else WISDOM_CMD
    _, result, archive_path = play_gate(
        args.depth,
        args.wisdom_ms,
        wisdom_white,
        live_pgn,
        args.max_plies,
        wisdom_cmd=wisdom_cmd,
    )

    wisdom_side = chess.WHITE if wisdom_white else chess.BLACK
    won = (result == "1-0" and wisdom_side == chess.WHITE) or (
        result == "0-1" and wisdom_side == chess.BLACK
    )
    print(f"Result: {result} -> {archive_path.name}")
    summary = {
        "depth": args.depth,
        "result": result,
        "won": won,
        "archive_pgn": str(archive_path),
        "goal_met": won and args.depth >= 8,
    }
    print(json.dumps(summary, indent=2))
    sys.exit(0 if summary["goal_met"] else 1)


if __name__ == "__main__":
    main()
