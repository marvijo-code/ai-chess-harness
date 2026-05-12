import argparse
import io
import json
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
CLK_COMMENT_RE = re.compile(r"\[%clk\s+[^\]]+\]")


def parse_games(stdout_path: Path) -> list[dict]:
    if not stdout_path.exists():
        return []
    text = stdout_path.read_text(encoding="utf-8", errors="replace")
    started = [(match.start(), match.groupdict()) for match in STARTED_RE.finditer(text)]
    if not started:
        return []
    finished = {int(match.group("game")): match.groupdict() for match in FINISHED_RE.finditer(text)}
    games: list[dict] = []
    for order, (_, current) in enumerate(started, start=1):
        game_no = int(current["game"])
        total = int(current["total"])
        result = "*"
        reason = ""
        if game_no in finished:
            result = finished[game_no]["result"]
            reason = finished[game_no]["reason"]
        games.append(
            {
                "game": game_no,
                "total": total,
                "white": current["white"],
                "black": current["black"],
                "result": result,
                "reason": reason,
                "finished": game_no in finished,
                "order": order,
            }
        )
    return games


def select_board_game(games: list[dict], locked_game: int | None) -> tuple[dict | None, int | None]:
    if locked_game is not None:
        for game in games:
            if int(game["game"]) == locked_game:
                return game, locked_game
    for game in games:
        if not game["finished"]:
            return game, int(game["game"])
    if games:
        game = games[-1]
        return game, int(game["game"])
    return None, locked_game


def selection_path_for(output_path: Path) -> Path:
    return output_path.with_suffix(".selection.json")


def read_selected_game(output_path: Path) -> int | None:
    path = selection_path_for(output_path)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        game = int(payload.get("locked_game") or 0)
        return game if game > 0 else None
    except Exception:
        return None


def log_timestamp_ms(value: str) -> int:
    return int(datetime.strptime(value, "%Y-%m-%d %H:%M:%S").timestamp() * 1000)


def moves_are_compatible(left: list[str], right: list[str]) -> bool:
    if len(left) <= len(right):
        return right[: len(left)] == left
    return left[: len(right)] == right


def collect_engine_states(log_dir: Path) -> list[dict]:
    paths = sorted(
        log_dir.glob("codex-chess-*.log"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    states: list[dict] = []
    for path in paths[:8]:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        moves: list[str] = []
        clocks_by_ply: dict[int, dict] = {}
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
            ply = len(moves)
            current_ply_state = clocks_by_ply.get(ply)
            if current_ply_state is None or updated_at >= current_ply_state["updated_at"]:
                clocks_by_ply[ply] = state
            state["clocks_by_ply"] = dict(clocks_by_ply)
            states.append(state)
    return states


def collect_engine_tracks(log_dir: Path) -> list[dict]:
    tracks: list[dict] = []
    for state in sorted(collect_engine_states(log_dir), key=lambda item: item["updated_at"]):
        target = None
        for track in tracks:
            if moves_are_compatible(track["moves"], state["moves"]):
                target = track
                break
        if target is None:
            target = {
                "first_seen": state["updated_at"],
                "updated_at": state["updated_at"],
                "moves": list(state["moves"]),
                "state": state,
                "clocks_by_ply": {},
            }
            tracks.append(target)
        target["updated_at"] = max(target["updated_at"], state["updated_at"])
        if len(state["moves"]) > len(target["moves"]) or (
            len(state["moves"]) == len(target["moves"]) and state["updated_at"] >= target["state"]["updated_at"]
        ):
            target["moves"] = list(state["moves"])
            target["state"] = state
        for ply, clock_state in state.get("clocks_by_ply", {}).items():
            current = target["clocks_by_ply"].get(ply)
            if current is None or clock_state["updated_at"] >= current["updated_at"]:
                target["clocks_by_ply"][ply] = clock_state

    result = []
    for track in sorted(tracks, key=lambda item: (item["first_seen"], item["updated_at"])):
        state = dict(track["state"])
        state["moves"] = list(track["moves"])
        state["clocks_by_ply"] = dict(track["clocks_by_ply"])
        result.append(state)
    return result


def select_engine_state(log_dir: Path, games: list[dict], current: dict, locked_moves: list[str] | None) -> tuple[dict, list[str] | None]:
    tracks = collect_engine_tracks(log_dir)
    if not tracks:
        return {"moves": [], "clocks_by_ply": {}}, locked_moves

    if locked_moves:
        compatible = [track for track in tracks if moves_are_compatible(locked_moves, track["moves"])]
        if compatible:
            selected = max(compatible, key=lambda item: (len(item["moves"]), item["updated_at"]))
            if len(selected["moves"]) >= len(locked_moves):
                return selected, list(selected["moves"])
            return selected, locked_moves

    game_index = 0
    for index, game in enumerate(games):
        if int(game["game"]) == int(current["game"]):
            game_index = index
            break
    if game_index < len(tracks):
        selected = tracks[game_index]
    else:
        selected = tracks[0]
    return selected, list(selected.get("moves", []))


def format_clk(ms: int) -> str:
    ms = max(0, int(ms))
    whole_seconds, milliseconds = divmod(ms, 1000)
    hours, remainder = divmod(whole_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if milliseconds:
        return f"{hours}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"
    return f"{hours}:{minutes:02d}:{seconds:02d}"


def merge_clk_comment(existing: str, ms: int) -> str:
    clk = f"[%clk {format_clk(ms)}]"
    stripped = CLK_COMMENT_RE.sub("", existing or "").strip()
    return f"{stripped} {clk}".strip()


def time_loss_for(current: dict, state: dict) -> dict | None:
    if current.get("finished"):
        return None
    running_side = state.get("running_side")
    if running_side == "White":
        remaining = state.get("wtime")
        result = "0-1"
        winner_side = "Black"
        loser_name = current.get("white") or "White"
        winner_name = current.get("black") or "Black"
    elif running_side == "Black":
        remaining = state.get("btime")
        result = "1-0"
        winner_side = "White"
        loser_name = current.get("black") or "Black"
        winner_name = current.get("white") or "White"
    else:
        return None
    try:
        if int(remaining) > 0:
            return None
    except (TypeError, ValueError):
        return None
    return {
        "result": result,
        "winner_side": winner_side,
        "reason": f"{loser_name} ({running_side}) lost on time; {winner_name} ({winner_side}) won",
    }


def game_with_timeout(current: dict, state: dict) -> dict:
    time_loss = time_loss_for(current, state)
    if time_loss is None:
        return current
    updated = dict(current)
    updated["result"] = time_loss["result"]
    updated["reason"] = time_loss["reason"]
    updated["finished"] = True
    updated["timeout_inferred"] = True
    return updated


def build_game(current: dict, state: dict) -> chess.pgn.Game:
    current = game_with_timeout(current, state)
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
    clocks_by_ply = state.get("clocks_by_ply", {})
    for ply, token in enumerate(move_tokens, start=1):
        try:
            move = chess.Move.from_uci(token)
        except ValueError:
            break
        if move not in board.legal_moves:
            break
        node = node.add_variation(move)
        board.push(move)
        clock_state = clocks_by_ply.get(ply)
        if clock_state is not None:
            remaining_ms = clock_state["wtime"] if ply % 2 else clock_state["btime"]
            node.comment = merge_clk_comment(node.comment, remaining_ms)
    return game


def pgn_text(game: chess.pgn.Game) -> str:
    output = io.StringIO()
    print(game, file=output, end="\n\n")
    return output.getvalue()


def status_text(games: list[dict], output_path: Path, locked_game: int | None) -> str:
    generated_at = time.time()
    payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(generated_at)),
        "generated_at_epoch": generated_at,
        "output_pgn": str(output_path.resolve()),
        "locked_game": locked_game,
        "games": [
            {
                "game": game["game"],
                "total": game["total"],
                "white": game["white"],
                "black": game["black"],
                "result": game["result"],
                "reason": game["reason"],
                "finished": game["finished"],
                "status": "Completed" if game["finished"] else "In progress",
                "is_board_game": locked_game is not None and int(game["game"]) == locked_game,
            }
            for game in games
        ],
    }
    return json.dumps(payload, indent=2)


def mirror(stdout_path: Path, log_dir: Path, output_path: Path, interval: float, once: bool) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    status_path = output_path.with_suffix(".status.json")
    previous = None
    previous_status = None
    locked_game: int | None = None
    locked_moves: list[str] | None = None
    last_requested_game: int | None = None
    while True:
        games = parse_games(stdout_path)
        requested_game = read_selected_game(output_path)
        if requested_game is not None and requested_game != last_requested_game:
            locked_game = requested_game
            locked_moves = None
            last_requested_game = requested_game
        current, locked_game = select_board_game(games, locked_game)
        if current is not None:
            state, locked_moves = select_engine_state(log_dir, games, current, locked_moves)
            current = game_with_timeout(current, state)
            text = pgn_text(build_game(current, state))
            if text != previous:
                output_path.write_text(text, encoding="utf-8")
                previous = text
        if games:
            status_games = [
                current if current is not None and int(game["game"]) == int(current["game"]) else game
                for game in games
            ]
            status = status_text(status_games, output_path, locked_game)
            if status != previous_status:
                status_path.write_text(status, encoding="utf-8")
                previous_status = status
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
