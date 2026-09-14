import argparse
import json
import os
import subprocess
import time
from pathlib import Path

import chess
import chess.pgn

try:
    from live_pgn_viewer import write_depth_match_live_state
except ImportError:  # imported as a package module (e.g. tools.play_engine_match)
    from tools.live_pgn_viewer import write_depth_match_live_state


ROOT = Path(__file__).resolve().parents[1]
ENGINE_CONFIG = Path(os.environ["APPDATA"]) / "org.encroissant.app" / "engines" / "engines.json"
OUT_DIR = ROOT / "out"
LLM_ENGINE_PATH = ROOT / "engines" / "llm-chess-engine" / "llm-chess-engine.cmd"


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
        max_attempts: int = 3,
    ) -> None:
        self.name = name
        self.path = path
        self.movetime_ms = movetime_ms
        self.max_attempts = max(1, int(max_attempts))
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

    def bestmove(self, board: chess.Board, go_line: str | None = None) -> tuple[chess.Move | None, list[str]]:
        self.command(f"position fen {board.fen()}")
        self.command(go_line or f"go movetime {self.movetime_ms}")
        read_timeout = max(120, int(self.movetime_ms / 1000) * self.max_attempts + 120)
        lines = self.read_until("bestmove", read_timeout)
        best_line = next(line for line in reversed(lines) if line.startswith("bestmove "))
        uci = best_line.split()[1]
        if uci == "0000":
            return None, lines
        move = chess.Move.from_uci(uci)
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


def clk_comment(ms: int) -> str:
    total = max(0, int(ms)) // 1000
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"[%clk {hours}:{minutes:02d}:{seconds:02d}]"


def write_clock_headers(game: chess.pgn.Game, clocks: dict[bool, int], running_side: bool | None) -> None:
    game.headers["WhiteClockMs"] = str(clocks[chess.WHITE])
    game.headers["BlackClockMs"] = str(clocks[chess.BLACK])
    game.headers["ClockUpdatedAtEpochMs"] = str(int(time.time() * 1000))
    if running_side is None:
        game.headers["ClockRunningSide"] = ""
    else:
        game.headers["ClockRunningSide"] = "White" if running_side == chess.WHITE else "Black"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--white-name", default="llm-chess-engine")
    parser.add_argument("--white-path", type=Path, default=LLM_ENGINE_PATH)
    parser.add_argument("--white-movetime-ms", type=int, default=30000)
    parser.add_argument("--white-option", action="append", default=[], help="UCI option for White, as NAME=VALUE.")
    parser.add_argument("--white-env", action="append", default=[], help="Environment override for White, as NAME=VALUE.")
    parser.add_argument("--black-name", default="Stockfish")
    parser.add_argument("--black-path", type=Path)
    parser.add_argument("--black-movetime-ms", type=int, default=None)
    parser.add_argument("--black-option", action="append", default=[], help="UCI option for Black, as NAME=VALUE.")
    parser.add_argument("--black-env", action="append", default=[], help="Environment override for Black, as NAME=VALUE.")
    parser.add_argument("--openrouter-model", help="Shortcut for --white-option Model=<id> and a readable White name.")
    parser.add_argument("--black-openrouter-model", help="Run an OpenRouter model as Black with the LLM engine.")
    parser.add_argument("--codex-learner-black", action="store_true", help="Use Codex-chess-learner as Black.")
    parser.add_argument("--max-attempts", type=int, default=3, help="Per-move model attempt limit for LLM engines.")
    parser.add_argument("--max-plies", type=int, default=8)
    parser.add_argument("--time-control-ms", type=int, default=0, help="Per-player clock in ms; 0 uses --*-movetime-ms.")
    parser.add_argument("--increment-ms", type=int, default=0, help="Per-move increment in ms when a time control is set.")
    parser.add_argument("--live-pgn", type=Path, default=None, help="Write an incrementally updating live PGN plus status sidecar.")
    parser.add_argument("--event", default="AI chess harness engine match")
    args = parser.parse_args()

    OUT_DIR.mkdir(exist_ok=True)
    white_options = parse_assignments(args.white_option, "--white-option")
    black_options = parse_assignments(args.black_option, "--black-option")
    white_env = parse_assignments(args.white_env, "--white-env")
    black_env = parse_assignments(args.black_env, "--black-env")

    white_name = args.white_name
    white_is_llm = args.white_path.resolve() == LLM_ENGINE_PATH.resolve()
    if args.openrouter_model:
        white_is_llm = True
        white_options["Model"] = args.openrouter_model
        white_env["OPENROUTER_MODEL"] = args.openrouter_model
        if white_name == "llm-chess-engine":
            white_name = f"OpenRouter {args.openrouter_model}"

    black_name = args.black_name
    black_path = args.black_path
    black_is_llm = False
    if args.codex_learner_black:
        black_name = "Codex-chess-learner"
        black_path = ROOT / "engines" / "codex-chess-learner" / "codex-chess-learner.cmd"
    if args.black_openrouter_model:
        black_is_llm = True
        black_path = black_path or LLM_ENGINE_PATH
        black_options["Model"] = args.black_openrouter_model
        black_env["OPENROUTER_MODEL"] = args.black_openrouter_model
        if args.black_name == "Stockfish":
            black_name = f"OpenRouter {args.black_openrouter_model}"
    black_path = black_path or load_engine_path(black_name)

    if white_is_llm:
        white_options.setdefault("MaxAttempts", str(args.max_attempts))
        white_env.setdefault("OPENROUTER_MAX_ATTEMPTS", str(args.max_attempts))
    if black_is_llm:
        black_options.setdefault("MaxAttempts", str(args.max_attempts))
        black_env.setdefault("OPENROUTER_MAX_ATTEMPTS", str(args.max_attempts))

    white_movetime = args.white_movetime_ms
    black_movetime = args.black_movetime_ms
    if black_movetime is None:
        black_movetime = 30000 if black_is_llm else 150

    white = UciEngine(
        white_name, args.white_path, white_movetime,
        options=white_options, env=white_env, max_attempts=args.max_attempts,
    )
    black = UciEngine(
        black_name, black_path, black_movetime,
        options=black_options, env=black_env, max_attempts=args.max_attempts,
    )
    white.new_game()
    black.new_game()

    board = chess.Board()
    game = chess.pgn.Game()
    game.headers["Event"] = args.event
    game.headers["Site"] = str(ROOT)
    game.headers["Date"] = time.strftime("%Y.%m.%d")
    game.headers["White"] = white_name
    game.headers["Black"] = black_name
    game.headers["Result"] = "*"
    game.headers["MaxAttempts"] = str(args.max_attempts)
    node = game
    moves = []
    forfeited_by = None
    timed_out_side = None
    termination = ""
    clock_mode = args.time_control_ms > 0
    clocks = {chess.WHITE: args.time_control_ms, chess.BLACK: args.time_control_ms}
    increment = max(0, args.increment_ms)
    if clock_mode:
        write_clock_headers(game, clocks, None)
    if args.live_pgn:
        write_depth_match_live_state(args.live_pgn, game, completed=False)

    try:
        for ply in range(args.max_plies):
            if board.is_game_over(claim_draw=True):
                break
            side = board.turn
            engine = white if side == chess.WHITE else black
            go_line = None
            if clock_mode:
                write_clock_headers(game, clocks, side)
                if args.live_pgn:
                    write_depth_match_live_state(args.live_pgn, game, completed=False)
                go_line = (
                    f"go wtime {clocks[chess.WHITE]} btime {clocks[chess.BLACK]} "
                    f"winc {increment} binc {increment}"
                )
            started = time.monotonic()
            move, lines = engine.bestmove(board, go_line)
            if clock_mode:
                elapsed_ms = int((time.monotonic() - started) * 1000)
                clocks[side] = clocks[side] - elapsed_ms + increment
                if clocks[side] <= 0:
                    clocks[side] = 0
                    timed_out_side = side
                    write_clock_headers(game, clocks, None)
                    print(f"{ply + 1:02d}. {engine.name} lost on time")
                    break
            if move is None:
                forfeited_by = engine
                print(f"{ply + 1:02d}. {engine.name} forfeited (0000) after {args.max_attempts} attempts")
                break
            san = board.san(move)
            board.push(move)
            node = node.add_variation(move)
            if clock_mode:
                node.comment = clk_comment(clocks[side])
                write_clock_headers(game, clocks, board.turn)
            moves.append(
                {
                    "ply": ply + 1,
                    "engine": engine.name,
                    "uci": move.uci(),
                    "san": san,
                    "fen_after": board.fen(),
                    "clock_ms": {"white": clocks[chess.WHITE], "black": clocks[chess.BLACK]} if clock_mode else None,
                    "info": [line for line in lines if line.startswith("info ")],
                }
            )
            print(f"{ply + 1:02d}. {engine.name} {san} ({move.uci()})")
            if args.live_pgn:
                write_depth_match_live_state(args.live_pgn, game, completed=False)

        if timed_out_side is not None:
            result = "0-1" if timed_out_side == chess.WHITE else "1-0"
            loser = white_name if timed_out_side == chess.WHITE else black_name
            termination = f"{loser} lost on time"
        elif forfeited_by is not None:
            result = "0-1" if forfeited_by is white else "1-0"
            termination = f"{forfeited_by.name} forfeited: model failed after {args.max_attempts} attempts"
        elif board.is_game_over(claim_draw=True):
            result = board.result(claim_draw=True)
            outcome = board.outcome(claim_draw=True)
            termination = outcome.termination.name if outcome else "finished"
        else:
            result = "*"
            termination = "max_plies"
        game.headers["Result"] = result
        game.headers["Termination"] = termination
        if clock_mode:
            write_clock_headers(game, clocks, None)
        if args.live_pgn:
            write_depth_match_live_state(args.live_pgn, game, completed=result != "*")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        pgn_path = OUT_DIR / f"engine-match-{stamp}.pgn"
        json_path = OUT_DIR / f"engine-match-{stamp}.json"
        summary = {
            "result": result,
            "completed": board.is_game_over(claim_draw=True) or forfeited_by is not None or timed_out_side is not None,
            "termination": termination,
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
            "max_attempts": args.max_attempts,
            "time_control_ms": args.time_control_ms,
            "increment_ms": increment,
            "final_clocks_ms": {"white": clocks[chess.WHITE], "black": clocks[chess.BLACK]} if clock_mode else None,
            "forfeit_by": forfeited_by.name if forfeited_by is not None else None,
            "timeout_by": "White" if timed_out_side == chess.WHITE else "Black" if timed_out_side == chess.BLACK else None,
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
