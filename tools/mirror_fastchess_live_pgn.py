import argparse
import io
import re
import time
from datetime import datetime
from pathlib import Path

import chess
import chess.pgn


STARTED_RE = re.compile(r"Started game\s+(?P<game>\d+)\s+of\s+(?P<total>\d+)\s+\((?P<white>.+?)\s+vs\s+(?P<black>.+?)\)")
FINISHED_RE = re.compile(r"Finished game\s+(?P<game>\d+)\s+\((?P<white>.+?)\s+vs\s+(?P<black>.+?)\):\s+(?P<result>1-0|0-1|1/2-1/2|\*)\s+\{(?P<reason>[^}]*)\}")
POSITION_RE = re.compile(r"position startpos(?: moves (?P<moves>[a-h][1-8][a-h][1-8][qrbn]?(?: [a-h][1-8][a-h][1-8][qrbn]?)*))?")
GO_RE = re.compile(r"\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s+>\s+go\b.*?\bwtime\s+(?P<wtime>\d+)\s+btime\s+(?P<btime>\d+)\b")


def parse_current_game(stdout_path: Path) -> dict | None:
    if not stdout_path.exists():
        return None
    text = stdout_path.read_text(encoding="utf-8", errors="replace")
    started = [match.groupdict() for match in STARTED_RE.finditer(text)]
    if not started:
        return None
    finished = {int(match.group("game")): match.groupdict() for match in FINISHED_RE.finditer(text)}
    current = started[-1]
    game_no = int(current["game"])
    total = int(current["total"])
    result = "*"
    reason = ""
    if game_no in finished:
        result = finished[game_no]["result"]
        reason = finished[game_no]["reason"]
    return {
        "game": game_no,
        "total": total,
        "white": current["white"],
        "black": current["black"],
        "result": result,
        "reason": reason,
        "finished": game_no in finished,
    }


def log_timestamp_ms(value: str) -> int:
    return int(datetime.strptime(value, "%Y-%m-%d %H:%M:%S").timestamp() * 1000)


def latest_engine_state(log_dir: Path) -> dict:
    paths = sorted(
        log_dir.glob("codex-chess-*.log"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    latest_state: dict | None = None
    for path in paths[:8]:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        moves: list[str] = []
        for line in lines:
            position_match = POSITION_RE.search(line)
            if position_match:
                raw_moves = position_match.group("moves") or ""
                moves = raw_moves.split() if raw_moves else []
                continue
            go_match = GO_RE.search(line)
            if not go_match:
                continue
            updated_at = log_timestamp_ms(go_match.group("ts"))
            state = {
                "moves": list(moves),
                "wtime": int(go_match.group("wtime")),
                "btime": int(go_match.group("btime")),
                "updated_at": updated_at,
                "running_side": "White" if len(moves) % 2 == 0 else "Black",
                "source": str(path),
            }
            if latest_state is None or updated_at >= latest_state["updated_at"]:
                latest_state = state
    return latest_state or {"moves": []}


def build_game(current: dict, state: dict) -> chess.pgn.Game:
    game = chess.pgn.Game()
    game.headers["Event"] = "FastChess live mirror"
    game.headers["Site"] = "C:/dev/chess-harness-codex"
    game.headers["Round"] = str(current["game"])
    game.headers["White"] = current["white"]
    game.headers["Black"] = current["black"]
    game.headers["Result"] = current["result"]
    if current["reason"]:
        game.headers["Termination"] = current["reason"]
    if {"wtime", "btime", "updated_at", "running_side"}.issubset(state):
        game.headers["WhiteClockMs"] = str(state["wtime"])
        game.headers["BlackClockMs"] = str(state["btime"])
        game.headers["ClockUpdatedAtEpochMs"] = str(state["updated_at"])
        game.headers["ClockRunningSide"] = "" if current["finished"] else state["running_side"]

    board = game.board()
    node = game
    move_tokens = state.get("moves", [])
    for token in move_tokens:
        try:
            move = chess.Move.from_uci(token)
        except ValueError:
            break
        if move not in board.legal_moves:
            break
        node = node.add_variation(move)
        board.push(move)
    return game


def pgn_text(game: chess.pgn.Game) -> str:
    output = io.StringIO()
    print(game, file=output, end="\n\n")
    return output.getvalue()


def mirror(stdout_path: Path, log_dir: Path, output_path: Path, interval: float, once: bool) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    previous = None
    while True:
        current = parse_current_game(stdout_path)
        if current is not None:
            text = pgn_text(build_game(current, latest_engine_state(log_dir)))
            if text != previous:
                output_path.write_text(text, encoding="utf-8")
                previous = text
        if once:
            return
        time.sleep(interval)


def main() -> None:
    parser = argparse.ArgumentParser(description="Mirror the active FastChess game from engine logs into a live PGN.")
    parser.add_argument("--fastchess-stdout", type=Path, required=True)
    parser.add_argument("--engine-log-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    mirror(args.fastchess_stdout, args.engine_log_dir, args.output, args.interval, args.once)


if __name__ == "__main__":
    main()
