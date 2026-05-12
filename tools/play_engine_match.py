import argparse
import json
import os
import subprocess
import time
from pathlib import Path

import chess
import chess.pgn


ROOT = Path(__file__).resolve().parents[1]
ENGINE_CONFIG = Path(os.environ["APPDATA"]) / "org.encroissant.app" / "engines" / "engines.json"
OUT_DIR = ROOT / "out"


def parse_assignment(value: str, label: str) -> tuple[str, str]:
    if "=" not in value:
        raise argparse.ArgumentTypeError(f"{label} must use NAME=VALUE syntax: {value!r}")
    name, assigned = value.split("=", 1)
    name = name.strip()
    if not name:
        raise argparse.ArgumentTypeError(f"{label} name is empty: {value!r}")
    return name, assigned


def parse_assignments(values: list[str], label: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for value in values:
        name, assigned = parse_assignment(value, label)
        parsed[name] = assigned
    return parsed


class UciEngine:
    def __init__(
        self,
        name: str,
        path: Path,
        movetime_ms: int,
        options: dict[str, str] | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.name = name
        self.path = path
        self.movetime_ms = movetime_ms
        process_env = os.environ.copy()
        if env:
            process_env.update(env)
        self.proc = subprocess.Popen(
            [str(path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=process_env,
        )
        self.command("uci")
        self.read_until("uciok", 30)
        for option_name, option_value in (options or {}).items():
            self.command(f"setoption name {option_name} value {option_value}")
        self.command("isready")
        self.read_until("readyok", 30)

    def command(self, line: str) -> None:
        if self.proc.stdin is None:
            raise RuntimeError(f"{self.name} stdin is closed")
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()

    def read_until(self, marker: str, timeout: int) -> list[str]:
        if self.proc.stdout is None:
            raise RuntimeError(f"{self.name} stdout is closed")
        deadline = time.time() + timeout
        lines = []
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if line == "":
                raise RuntimeError(f"{self.name} exited unexpectedly")
            line = line.rstrip("\n")
            lines.append(line)
            if marker in line:
                return lines
        raise TimeoutError(f"{self.name} timed out waiting for {marker!r}; saw {lines!r}")

    def new_game(self) -> None:
        self.command("ucinewgame")
        self.command("isready")
        self.read_until("readyok", 30)

    def bestmove(self, board: chess.Board) -> tuple[chess.Move, list[str]]:
        self.command(f"position fen {board.fen()}")
        self.command(f"go movetime {self.movetime_ms}")
        lines = self.read_until("bestmove", max(30, int(self.movetime_ms / 1000) + 120))
        best_line = next(line for line in reversed(lines) if line.startswith("bestmove "))
        move = chess.Move.from_uci(best_line.split()[1])
        if move not in board.legal_moves:
            raise RuntimeError(f"{self.name} returned illegal move {move.uci()} for {board.fen()}")
        return move, lines

    def close(self) -> None:
        if self.proc.poll() is None:
            try:
                self.command("quit")
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()


def load_engine_path(name: str) -> Path:
    engines = json.loads(ENGINE_CONFIG.read_text(encoding="utf-8"))
    for engine in engines:
        if engine.get("name", "").lower() == name.lower() and engine.get("enabled"):
            path = Path(engine["path"])
            if path.exists():
                return path
    raise RuntimeError(f"No enabled {name!r} executable found in {ENGINE_CONFIG}")


def game_to_pgn(game: chess.pgn.Game) -> str:
    return str(game)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--white-name", default="llm-chess-engine")
    parser.add_argument("--white-path", type=Path, default=ROOT / "engines" / "llm-chess-engine" / "llm-chess-engine.cmd")
    parser.add_argument("--white-movetime-ms", type=int, default=30000)
    parser.add_argument("--white-option", action="append", default=[], help="UCI option for White, as NAME=VALUE.")
    parser.add_argument("--white-env", action="append", default=[], help="Environment override for White, as NAME=VALUE.")
    parser.add_argument("--black-name", default="Stockfish")
    parser.add_argument("--black-path", type=Path)
    parser.add_argument("--black-movetime-ms", type=int, default=150)
    parser.add_argument("--black-option", action="append", default=[], help="UCI option for Black, as NAME=VALUE.")
    parser.add_argument("--black-env", action="append", default=[], help="Environment override for Black, as NAME=VALUE.")
    parser.add_argument("--openrouter-model", help="Shortcut for --white-option Model=<id> and a readable White name.")
    parser.add_argument("--codex-learner-black", action="store_true", help="Use Codex-chess-learner as Black.")
    parser.add_argument("--max-plies", type=int, default=8)
    args = parser.parse_args()

    OUT_DIR.mkdir(exist_ok=True)
    white_options = parse_assignments(args.white_option, "--white-option")
    black_options = parse_assignments(args.black_option, "--black-option")
    white_env = parse_assignments(args.white_env, "--white-env")
    black_env = parse_assignments(args.black_env, "--black-env")

    white_name = args.white_name
    if args.openrouter_model:
        white_options["Model"] = args.openrouter_model
        white_env["OPENROUTER_MODEL"] = args.openrouter_model
        if white_name == "llm-chess-engine":
            white_name = f"OpenRouter {args.openrouter_model}"

    black_name = args.black_name
    black_path = args.black_path
    if args.codex_learner_black:
        black_name = "Codex-chess-learner"
        black_path = ROOT / "engines" / "codex-chess-learner" / "codex-chess-learner.cmd"
    black_path = black_path or load_engine_path(black_name)

    white = UciEngine(white_name, args.white_path, args.white_movetime_ms, options=white_options, env=white_env)
    black = UciEngine(black_name, black_path, args.black_movetime_ms, options=black_options, env=black_env)
    white.new_game()
    black.new_game()

    board = chess.Board()
    game = chess.pgn.Game()
    game.headers["Event"] = "AI chess harness engine match"
    game.headers["Site"] = str(ROOT)
    game.headers["Date"] = time.strftime("%Y.%m.%d")
    game.headers["White"] = white_name
    game.headers["Black"] = black_name
    node = game
    moves = []

    try:
        for ply in range(args.max_plies):
            if board.is_game_over(claim_draw=True):
                break
            engine = white if board.turn == chess.WHITE else black
            move, lines = engine.bestmove(board)
            san = board.san(move)
            board.push(move)
            node = node.add_variation(move)
            moves.append(
                {
                    "ply": ply + 1,
                    "engine": engine.name,
                    "uci": move.uci(),
                    "san": san,
                    "fen_after": board.fen(),
                    "info": [line for line in lines if line.startswith("info ")],
                }
            )
            print(f"{ply + 1:02d}. {engine.name} {san} ({move.uci()})")

        result = board.result(claim_draw=True) if board.is_game_over(claim_draw=True) else "*"
        game.headers["Result"] = result
        stamp = time.strftime("%Y%m%d-%H%M%S")
        pgn_path = OUT_DIR / f"engine-match-{stamp}.pgn"
        json_path = OUT_DIR / f"engine-match-{stamp}.json"
        summary = {
            "result": result,
            "completed": board.is_game_over(claim_draw=True),
            "termination": board.outcome(claim_draw=True).termination.name if board.outcome(claim_draw=True) else "max_plies",
            "plies": len(moves),
            "fen": board.fen(),
            "white": str(args.white_path),
            "black": str(black_path),
            "white_name": white_name,
            "black_name": black_name,
            "white_options": white_options,
            "black_options": black_options,
            "white_env_keys": sorted(white_env),
            "black_env_keys": sorted(black_env),
            "pgn": str(pgn_path),
            "json": str(json_path),
            "moves": moves,
        }
        pgn_path.write_text(game_to_pgn(game), encoding="utf-8")
        json_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(json.dumps(summary, indent=2))
    finally:
        white.close()
        black.close()


if __name__ == "__main__":
    main()
