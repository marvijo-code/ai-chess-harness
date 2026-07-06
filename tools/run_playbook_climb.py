"""Playbook-chess Stockfish depth-ladder climb loop (PRD 169-171).

Ladder: Stockfish depth 1 -> 8, win-gated (draws and material adjudications
never advance a gate). After every non-win the trainer updates the
human-readable playbook from the failed game plus TWIC evidence, then the gate
is retried. Every newly passed depth is committed and pushed as a git
checkpoint scoped to the playbook allowlist only (PRD 170).

Fail-early rules:
- Playbook resigns after `--resign-moves` consecutive own evals at or below
  `--resign-threshold` (recorded as a loss).
- At `--max-plies` the game is stopped and material-adjudicated for the log,
  but an adjudicated edge never counts as a gate pass.
"""

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

from playbook_trainer import train_round  # noqa: E402

ENGINE_NAME = "Playbook-chess"
PLAYBOOK_CMD = ROOT / "engines" / "playbook-chess" / "playbook-chess.cmd"
PLAYBOOK_FILE = ROOT / "engines" / "playbook-chess" / "playbook.md"
CLIMB_LOG = ROOT / "engines" / "playbook-chess" / "playbook-climb-log.md"
MATCH_DIR = ROOT / "out" / "playbook-matches"
STATE_DIR = ROOT / "out" / "playbook-climb"
STATE_PATH = STATE_DIR / "climb-state.json"
LIVE_DIR = ROOT / "out" / "live"
ENGINE_CONFIG = Path(os.environ.get("APPDATA", "")) / "org.encroissant.app" / "engines" / "engines.json"

INFO_STRING_RE = re.compile(r"^info string (.+)$")
EVAL_RE = re.compile(r"eval ([+-]?\d+)cp")

# PRD.md / PRD_CHECKLIST.md are deliberately excluded: they can carry unrelated
# in-flight hunks and are committed manually, not by climb checkpoints.
CHECKPOINT_ALLOWLIST = (
    "engines/playbook-chess/",
    "tools/playbook_trainer.py",
    "tools/run_playbook_climb.py",
    "tests/test_playbook_chess.py",
)

PIECE_CP = {chess.PAWN: 100, chess.KNIGHT: 320, chess.BISHOP: 330, chess.ROOK: 500, chess.QUEEN: 900}


class UciEngine:
    def __init__(
        self,
        name: str,
        path: Path,
        movetime_ms: int | None = None,
        depth: int | None = None,
        options: dict[str, object] | None = None,
    ) -> None:
        self.name = name
        self.movetime_ms = movetime_ms
        self.depth = depth
        self.proc = subprocess.Popen(
            [str(path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
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

    def _read_until(self, marker: str, timeout: float) -> list[str]:
        assert self.proc.stdout is not None
        deadline = time.time() + timeout
        lines: list[str] = []
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if line == "":
                raise RuntimeError(f"{self.name} exited unexpectedly")
            line = line.rstrip("\n")
            lines.append(line)
            if line.startswith(marker):
                return lines
        raise TimeoutError(f"{self.name} timed out waiting for {marker!r}")

    def new_game(self) -> None:
        self._cmd("ucinewgame")
        self._cmd("isready")
        self._read_until("readyok", 30)

    def bestmove(self, board: chess.Board, move_history: str) -> tuple[chess.Move, str]:
        # PRD 171: full history so repetition state stays visible to both engines.
        position = "position startpos" + (f" moves {move_history}" if move_history else "")
        self._cmd(position)
        if self.depth is not None:
            self._cmd(f"go depth {self.depth}")
            timeout = max(90.0, self.depth * 12.0)
        else:
            ms = self.movetime_ms or 8000
            self._cmd(f"go movetime {ms}")
            timeout = ms / 1000.0 + 120.0
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
            raise RuntimeError(f"{self.name} illegal move {uci} in {board.fen()}")
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


def material_cp_white(board: chess.Board) -> int:
    score = 0
    for pt, value in PIECE_CP.items():
        score += value * (len(board.pieces(pt, chess.WHITE)) - len(board.pieces(pt, chess.BLACK)))
    return score


def playbook_version() -> int:
    try:
        match = re.search(r"-\s*meta\.version\s*=\s*(\d+)", PLAYBOOK_FILE.read_text(encoding="utf-8"))
        return int(match.group(1)) if match else 0
    except OSError:
        return 0


def default_state() -> dict:
    return {
        "engine": ENGINE_NAME,
        "current_depth": 1,
        "target_depth": 8,
        "max_passed_depth": 0,
        "attempts_total": 0,
        "per_depth": {},
        "history": [],
        "last_checkpoint": None,
        "goal_met": False,
    }


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default_state()


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def append_climb_log(block: str) -> None:
    if not CLIMB_LOG.exists():
        CLIMB_LOG.write_text("# Playbook-chess climb log\n", encoding="utf-8")
    with CLIMB_LOG.open("a", encoding="utf-8") as handle:
        handle.write(block)


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )


def staged_paths_allowed(paths: list[str]) -> bool:
    for path in paths:
        norm = path.replace("\\", "/")
        if not any(norm == allowed.rstrip("/") or norm.startswith(allowed) for allowed in CHECKPOINT_ALLOWLIST):
            return False
    return True


def checkpoint_commit(depth: int, result: str, color: str, attempt: int, push: bool) -> str | None:
    """Commit + push a scoped checkpoint. Returns the commit hash or None."""
    git("reset")  # unstage anything a previous run left behind
    existing = [p for p in CHECKPOINT_ALLOWLIST if (ROOT / p.rstrip("/")).exists()]
    git("add", "--", *existing)
    staged = [p for p in git("diff", "--cached", "--name-only").stdout.splitlines() if p.strip()]
    if not staged:
        return None
    if not staged_paths_allowed(staged):
        git("reset")
        append_climb_log(
            f"\n> Checkpoint aborted: staged paths escaped the allowlist: {staged}\n"
        )
        return None
    message = (
        f"feat(playbook-chess): pass Stockfish depth {depth} gate "
        f"({result} as {color}, attempt {attempt}, playbook v{playbook_version()})"
    )
    commit = git("commit", "-m", message)
    if commit.returncode != 0:
        append_climb_log(f"\n> Checkpoint commit failed: {commit.stderr.strip()[:400]}\n")
        return None
    sha = git("rev-parse", "--short", "HEAD").stdout.strip()
    if push:
        pushed = git("push", "origin", "main")
        if pushed.returncode != 0:
            append_climb_log(f"\n> Checkpoint {sha} committed but push failed: {pushed.stderr.strip()[:400]}\n")
    return sha


def play_game(
    depth: int,
    playbook_ms: int,
    playbook_white: bool,
    max_plies: int,
    resign_threshold: int,
    resign_moves: int,
    live: bool,
) -> tuple[str, str, Path]:
    """Play one gate game. Returns (result, termination, archive_path)."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    archive_path = MATCH_DIR / f"playbook-vs-stockfish-depth-{depth}-{stamp}.pgn"
    live_pgn = LIVE_DIR / f"playbook-vs-stockfish-depth-{depth}-{stamp}-live.pgn" if live else None

    write_live = None
    if live_pgn is not None:
        try:
            from live_pgn_viewer import write_depth_match_live_state  # noqa: PLC0415

            write_live = write_depth_match_live_state
        except Exception:
            write_live = None

    playbook = UciEngine(ENGINE_NAME, PLAYBOOK_CMD, movetime_ms=playbook_ms)
    stockfish = UciEngine(
        f"Stockfish depth {depth}", load_stockfish(), depth=depth, options={"Threads": 1, "Hash": 16}
    )
    playbook.new_game()
    stockfish.new_game()

    board = chess.Board()
    game = chess.pgn.Game()
    game.headers["Event"] = f"{ENGINE_NAME} vs Stockfish depth {depth}"
    game.headers["Site"] = str(ROOT)
    game.headers["Date"] = time.strftime("%Y.%m.%d")
    game.headers["Round"] = "1"
    game.headers["White"] = ENGINE_NAME if playbook_white else f"Stockfish depth {depth}"
    game.headers["Black"] = f"Stockfish depth {depth}" if playbook_white else ENGINE_NAME
    game.headers["StockfishDepth"] = str(depth)
    game.headers["PlaybookMovetimeMs"] = str(playbook_ms)
    game.headers["PlaybookVersion"] = str(playbook_version())
    game.headers["GameStartTime"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S %z")
    game.headers["Result"] = "*"
    node = game
    if write_live:
        write_live(live_pgn, game, completed=False)

    history: list[str] = []
    bad_evals = 0
    result = None
    termination = None
    try:
        for ply in range(max_plies):
            if board.is_game_over(claim_draw=True):
                break
            is_playbook = (board.turn == chess.WHITE) == playbook_white
            engine = playbook if is_playbook else stockfish
            move, thought = engine.bestmove(board, " ".join(history))
            node = node.add_variation(move)
            if is_playbook and thought:
                node.comment = thought[:400]
                match = EVAL_RE.search(thought)
                if match:
                    if int(match.group(1)) <= -resign_threshold:
                        bad_evals += 1
                    else:
                        bad_evals = 0
            history.append(move.uci())
            board.push(move)
            if write_live:
                write_live(live_pgn, game, completed=False)
            print(f"ply {ply + 1}: {move.uci()} ({'PB' if is_playbook else 'SF'})", flush=True)
            if is_playbook and bad_evals >= resign_moves:
                result = "0-1" if playbook_white else "1-0"
                termination = "resignation (playbook self-eval hopeless)"
                break

        if result is None:
            if board.is_game_over(claim_draw=True):
                result = board.result(claim_draw=True)
                outcome = board.outcome(claim_draw=True)
                termination = str(outcome.termination.name).replace("_", " ") if outcome else "finished"
            else:
                # Ply cap: deterministic material adjudication for the record;
                # never counts as a gate pass (PRD 169).
                diff = material_cp_white(board)
                if diff >= 200:
                    result = "1-0"
                elif diff <= -200:
                    result = "0-1"
                else:
                    result = "1/2-1/2"
                termination = f"max plies {max_plies} material adjudication ({diff:+d}cp white)"

        game.headers["Result"] = result
        game.headers["Termination"] = termination
        game.headers["GameEndTime"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S %z")
        MATCH_DIR.mkdir(parents=True, exist_ok=True)
        archive_path.write_text(str(game) + "\n\n", encoding="utf-8")
        if write_live:
            write_live(live_pgn, game, completed=True)
        return result, termination, archive_path
    finally:
        playbook.close()
        stockfish.close()


def gate_won(result: str, termination: str, playbook_white: bool) -> bool:
    won = (result == "1-0" and playbook_white) or (result == "0-1" and not playbook_white)
    if not won:
        return False
    return "adjudication" not in termination.lower()


def run_climb(args: argparse.Namespace) -> dict:
    state = load_state()
    state["target_depth"] = args.target_depth
    if args.start_depth:
        state["current_depth"] = args.start_depth
    depth = max(1, int(state["current_depth"]))
    deadline = time.time() + args.budget_minutes * 60 if args.budget_minutes else None
    attempts_this_run = 0

    while depth <= args.target_depth:
        if deadline and time.time() >= deadline:
            print("budget reached, stopping", flush=True)
            break
        if args.max_attempts and attempts_this_run >= args.max_attempts:
            print("max attempts reached, stopping", flush=True)
            break

        per_depth = state["per_depth"].setdefault(str(depth), {"wins": 0, "losses": 0, "draws": 0, "attempts": 0})
        attempt = per_depth["attempts"] + 1
        playbook_white = attempt % 2 == 1
        color = "White" if playbook_white else "Black"
        movetime = args.playbook_ms
        print(f"=== depth {depth} attempt {attempt} as {color} (playbook v{playbook_version()}) ===", flush=True)

        result, termination, archive_path = play_game(
            depth,
            movetime,
            playbook_white,
            args.max_plies,
            args.resign_threshold,
            args.resign_moves,
            live=not args.no_live,
        )
        attempts_this_run += 1
        per_depth["attempts"] = attempt
        state["attempts_total"] += 1
        won = gate_won(result, termination, playbook_white)
        drew = result == "1/2-1/2"
        if won:
            per_depth["wins"] += 1
        elif drew:
            per_depth["draws"] += 1
        else:
            per_depth["losses"] += 1

        outcome = "WIN" if won else ("DRAW" if drew else "LOSS")
        stamp = time.strftime("%Y-%m-%d %H:%M")
        state["history"].append(
            {
                "time": stamp,
                "depth": depth,
                "attempt": attempt,
                "color": color,
                "result": result,
                "termination": termination,
                "outcome": outcome,
                "playbook_version": playbook_version(),
                "pgn": archive_path.name,
            }
        )
        append_climb_log(
            f"\n### {stamp} — Depth {depth} attempt {attempt} ({outcome})\n\n"
            f"**Result:** {result} ({termination}) as {color}, playbook v{playbook_version()} — `{archive_path.name}`\n"
        )
        print(f"depth {depth} attempt {attempt}: {outcome} ({result}, {termination})", flush=True)

        if won:
            state["max_passed_depth"] = max(state["max_passed_depth"], depth)
            sha = None
            if not args.no_checkpoint:
                sha = checkpoint_commit(depth, result, color, attempt, push=not args.no_push)
            state["last_checkpoint"] = sha
            append_climb_log(
                f"\n**Gate passed.** Checkpoint: {sha or 'skipped'}. Advancing to depth {depth + 1}.\n"
            )
            depth += 1
            state["current_depth"] = depth
            if depth > args.target_depth:
                state["goal_met"] = True
            save_state(state)
            continue

        # Fail early, learn quick: train on this game immediately.
        summary = train_round(
            [archive_path],
            PLAYBOOK_FILE,
            fresh_sample=args.fresh_sample,
        )
        append_climb_log(
            f"\n**Training:** classes {summary['classes']}, adjustments {summary['adjustments'] or 'none'}.\n"
        )
        print(f"training: {summary['classes']} -> {summary['adjustments']}", flush=True)
        save_state(state)

    save_state(state)
    return state


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-depth", type=int, default=0, help="override resume depth")
    parser.add_argument("--target-depth", type=int, default=8)
    parser.add_argument("--playbook-ms", type=int, default=8000)
    parser.add_argument("--max-plies", type=int, default=240)
    parser.add_argument("--resign-threshold", type=int, default=900)
    parser.add_argument("--resign-moves", type=int, default=6)
    parser.add_argument("--max-attempts", type=int, default=0, help="stop after N games this run")
    parser.add_argument("--budget-minutes", type=float, default=0, help="wall-clock budget for this run")
    parser.add_argument("--fresh-sample", type=int, default=150)
    parser.add_argument("--no-checkpoint", action="store_true")
    parser.add_argument("--no-push", action="store_true")
    parser.add_argument("--no-live", action="store_true")
    args = parser.parse_args()

    state = run_climb(args)
    print(json.dumps({k: state[k] for k in ("current_depth", "max_passed_depth", "attempts_total", "goal_met")}, indent=2))
    sys.exit(0 if state.get("goal_met") else 1)


if __name__ == "__main__":
    main()
