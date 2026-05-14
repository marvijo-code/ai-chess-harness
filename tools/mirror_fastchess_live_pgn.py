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
ACTIVE_TRACK_MAX_AGE_MS = 15 * 60 * 1000
RUN_ARTIFACT_MAX_IDLE_MS = 30 * 60 * 1000


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


def infer_pgnout_path(stdout_path: Path) -> Path:
    name = stdout_path.name
    suffix = "-launch.out.log"
    if name.endswith(suffix):
        return stdout_path.with_name(name[: -len(suffix)] + ".pgn")
    return stdout_path.with_suffix(".pgn")


def latest_mtime_ms(paths: list[Path]) -> int | None:
    mtimes: list[int] = []
    for path in paths:
        try:
            mtimes.append(int(path.stat().st_mtime * 1000))
        except OSError:
            continue
    return max(mtimes) if mtimes else None


def run_artifacts_recent(stdout_path: Path, pgnout_path: Path, now_ms: int | None = None) -> bool:
    latest_mtime = latest_mtime_ms([stdout_path, pgnout_path])
    if latest_mtime is None:
        return True
    current_now = current_epoch_ms() if now_ms is None else now_ms
    return current_now - latest_mtime <= RUN_ARTIFACT_MAX_IDLE_MS


def parse_pgnout_games(pgnout_path: Path, total: int | None = None) -> list[dict]:
    if not pgnout_path.exists():
        return []
    games: list[dict] = []
    try:
        with pgnout_path.open("r", encoding="utf-8", errors="replace") as handle:
            while True:
                game = chess.pgn.read_game(handle)
                if game is None:
                    break
                result = game.headers.get("Result", "*")
                if result == "*":
                    continue
                game_no = len(games) + 1
                games.append(
                    {
                        "game": game_no,
                        "total": total or 0,
                        "white": game.headers.get("White", "White"),
                        "black": game.headers.get("Black", "Black"),
                        "result": result,
                        "reason": game.headers.get("Termination", ""),
                        "finished": True,
                        "order": game_no,
                    }
                )
    except OSError:
        return []
    return games


def merge_pgnout_games(stdout_games: list[dict], pgnout_games: list[dict]) -> list[dict]:
    if not pgnout_games:
        return stdout_games
    total = int(stdout_games[0].get("total") or pgnout_games[-1].get("total") or len(pgnout_games)) if (stdout_games or pgnout_games) else 0
    by_game = {int(game["game"]): dict(game) for game in stdout_games}
    for game in pgnout_games:
        merged = dict(by_game.get(int(game["game"]), {}))
        merged.update(game)
        merged["total"] = total
        by_game[int(game["game"])] = merged
    return [by_game[index] for index in sorted(by_game)]


def players_for_game(game_no: int, games: list[dict]) -> tuple[str, str]:
    first = next((game for game in games if int(game.get("game") or 0) == 1), None)
    second = next((game for game in games if int(game.get("game") or 0) == 2), None)
    if game_no % 2 == 0 and second is not None:
        return second.get("white") or "White", second.get("black") or "Black"
    if first is not None:
        return first.get("white") or "White", first.get("black") or "Black"
    if games:
        return games[-1].get("white") or "White", games[-1].get("black") or "Black"
    return "White", "Black"


def synthesize_active_game_from_tracks(games: list[dict], tracks: list[dict], now_ms: int | None = None) -> list[dict]:
    if not games or any(not game.get("finished") for game in games):
        return games
    if len(tracks) <= len(games):
        return games
    latest_track = max(tracks, key=lambda item: int(item.get("updated_at") or 0))
    current_now = current_epoch_ms() if now_ms is None else now_ms
    try:
        if current_now - int(latest_track.get("updated_at") or 0) > ACTIVE_TRACK_MAX_AGE_MS:
            return games
    except (TypeError, ValueError):
        return games
    game_no = max(int(game["game"]) for game in games) + 1
    total = int(games[0].get("total") or game_no)
    if total and game_no > total:
        return games
    white, black = players_for_game(game_no, games)
    return [
        *games,
        {
            "game": game_no,
            "total": total,
            "white": white,
            "black": black,
            "result": "*",
            "reason": "",
            "finished": False,
            "order": game_no,
        },
    ]


def select_board_game(games: list[dict], locked_game: int | None) -> tuple[dict | None, int | None]:
    if locked_game is not None:
        for game in games:
            if int(game["game"]) == locked_game and not game["finished"]:
                return game, locked_game
        for game in games:
            if not game["finished"]:
                return game, int(game["game"])
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


def run_start_ms(stdout_path: Path) -> int | None:
    try:
        stat = stdout_path.stat()
    except OSError:
        return None
    return int((min(stat.st_ctime, stat.st_mtime) - 5) * 1000)


def collect_engine_states(log_dir: Path, since_ms: int | None = None) -> list[dict]:
    paths = sorted(
        log_dir.glob("codex-chess-*.log"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    states: list[dict] = []
    for path in paths[:8]:
        if since_ms is not None and int(path.stat().st_mtime * 1000) < since_ms:
            continue
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
            if since_ms is not None and updated_at < since_ms:
                continue
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


def collect_engine_tracks(log_dir: Path, since_ms: int | None = None) -> list[dict]:
    tracks: list[dict] = []
    for state in sorted(collect_engine_states(log_dir, since_ms), key=lambda item: item["updated_at"]):
        target = None
        compatible_tracks = [track for track in tracks if moves_are_compatible(track["moves"], state["moves"])]
        if state["moves"] and compatible_tracks:
            target = max(compatible_tracks, key=lambda item: (item["updated_at"], item["first_seen"]))
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
        state["first_seen"] = track["first_seen"]
        state["moves"] = list(track["moves"])
        state["clocks_by_ply"] = dict(track["clocks_by_ply"])
        result.append(state)
    return result


def select_engine_state(
    log_dir: Path,
    games: list[dict],
    current: dict,
    locked_moves: list[str] | None,
    since_ms: int | None = None,
    tracks: list[dict] | None = None,
    now_ms: int | None = None,
) -> tuple[dict, list[str] | None]:
    if tracks is None:
        tracks = collect_engine_tracks(log_dir, since_ms)
    if not tracks:
        return {"moves": [], "clocks_by_ply": {}}, locked_moves

    game_index = 0
    for index, game in enumerate(games):
        if int(game["game"]) == int(current["game"]):
            game_index = index
            break
    if not current.get("finished"):
        if game_index < len(tracks):
            selected = tracks[game_index]
        else:
            selected = max(tracks, key=lambda item: item["updated_at"])
        unfinished_games = [game for game in games if not game.get("finished")]
        if len(unfinished_games) == 1 and len(tracks) > 1:
            active_tracks = [track for track in tracks if time_loss_for(current, track, now_ms) is None]
            latest = max(active_tracks or tracks, key=lambda item: (item.get("updated_at", 0), len(item.get("moves", []))))
            if latest.get("updated_at", 0) >= selected.get("updated_at", 0):
                selected = latest
        return selected, list(selected.get("moves", []))

    if locked_moves:
        compatible = [track for track in tracks if moves_are_compatible(locked_moves, track["moves"])]
        if compatible:
            selected = max(compatible, key=lambda item: (len(item["moves"]), item["updated_at"]))
            if len(selected["moves"]) >= len(locked_moves):
                return selected, list(selected["moves"])
            return selected, locked_moves

    if game_index < len(tracks):
        selected = tracks[game_index]
    elif not current.get("finished"):
        selected = max(tracks, key=lambda item: item["updated_at"])
    else:
        selected = tracks[-1]
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


def current_epoch_ms() -> int:
    return int(time.time() * 1000)


def time_loss_for(current: dict, state: dict, now_ms: int | None = None) -> dict | None:
    if current.get("finished"):
        return None
    running_side = state.get("running_side")
    if running_side == "White":
        clock_key = "wtime"
        remaining = state.get("wtime")
        result = "0-1"
        winner_side = "Black"
        loser_name = current.get("white") or "White"
        winner_name = current.get("black") or "Black"
    elif running_side == "Black":
        clock_key = "btime"
        remaining = state.get("btime")
        result = "1-0"
        winner_side = "White"
        loser_name = current.get("black") or "Black"
        winner_name = current.get("white") or "White"
    else:
        return None
    try:
        effective_remaining = int(remaining)
        updated_at = int(state.get("updated_at"))
        timestamp_now = current_epoch_ms() if now_ms is None else int(now_ms)
        effective_remaining -= max(0, timestamp_now - updated_at)
        if effective_remaining > 0:
            return None
    except (TypeError, ValueError):
        return None
    return {
        "result": result,
        "winner_side": winner_side,
        "clock_key": clock_key,
        "reason": f"{loser_name} ({running_side}) lost on time; {winner_name} ({winner_side}) won",
    }


def game_with_timeout(current: dict, state: dict, now_ms: int | None = None) -> dict:
    time_loss = time_loss_for(current, state, now_ms)
    if time_loss is None:
        return current
    updated = dict(current)
    updated["result"] = time_loss["result"]
    updated["reason"] = time_loss["reason"]
    updated["finished"] = True
    updated["timeout_inferred"] = True
    updated["timeout_clock_key"] = time_loss["clock_key"]
    return updated


def build_game(current: dict, state: dict, now_ms: int | None = None) -> chess.pgn.Game:
    current = game_with_timeout(current, state, now_ms)
    header_state = dict(state)
    timeout_clock_key = current.get("timeout_clock_key")
    if timeout_clock_key:
        header_state[timeout_clock_key] = 0
    game = chess.pgn.Game()
    game.headers["Event"] = "FastChess live mirror"
    game.headers["Site"] = "C:/dev/chess-harness-codex"
    started_at_ms = current.get("started_at_ms")
    if started_at_ms:
        started_at = datetime.fromtimestamp(int(started_at_ms) / 1000)
        game.headers["Date"] = started_at.strftime("%Y.%m.%d")
        game.headers["GameStartTime"] = started_at.strftime("%Y-%m-%d %H:%M:%S")
    game.headers["Round"] = str(current["game"])
    if current.get("total"):
        game.headers["TotalGames"] = str(current["total"])
    game.headers["White"] = current["white"]
    game.headers["Black"] = current["black"]
    game.headers["Result"] = current["result"]
    if current["reason"]:
        game.headers["Termination"] = current["reason"]
    if {"wtime", "btime", "updated_at", "running_side"}.issubset(header_state):
        game.headers["WhiteClockMs"] = str(header_state["wtime"])
        game.headers["BlackClockMs"] = str(header_state["btime"])
        game.headers["ClockUpdatedAtEpochMs"] = str(header_state["updated_at"])
        game.headers["ClockRunningSide"] = "" if current["finished"] else header_state["running_side"]

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


def run_slug_prefix(output_path: Path) -> str:
    slug = output_path.stem
    if slug.endswith("-live"):
        slug = slug.removesuffix("-live")
    return re.sub(r"-live-\d{8}-\d{6}$", "-live", slug)


def game_started_at_ms(current: dict, state: dict, remembered: dict[int, int]) -> int:
    game_no = int(current["game"])
    first_seen = state.get("first_seen") or state.get("updated_at")
    try:
        started_at_ms = int(first_seen)
    except (TypeError, ValueError):
        started_at_ms = remembered.get(game_no) or current_epoch_ms()
    remembered.setdefault(game_no, started_at_ms)
    return remembered[game_no]


def game_output_path(output_path: Path, current: dict, started_at_ms: int) -> Path:
    timestamp = datetime.fromtimestamp(started_at_ms / 1000).strftime("%Y%m%d-%H%M%S")
    game_no = int(current["game"])
    return output_path.with_name(f"{run_slug_prefix(output_path)}-{timestamp}-game-{game_no}-live.pgn")


def status_text(games: list[dict], output_path: Path, locked_game: int | None, control_path: Path | None = None) -> str:
    generated_at = time.time()
    payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(generated_at)),
        "generated_at_epoch": generated_at,
        "output_pgn": str(output_path.resolve()),
        "control_pgn": str((control_path or output_path).resolve()),
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
    pgnout_path = infer_pgnout_path(stdout_path)
    since_ms = run_start_ms(stdout_path)
    previous = None
    previous_status = None
    previous_board_path: Path | None = None
    locked_game: int | None = None
    locked_moves: list[str] | None = None
    last_requested_game: int | None = None
    game_start_times: dict[int, int] = {}
    while True:
        if not once and not run_artifacts_recent(stdout_path, pgnout_path):
            time.sleep(interval)
            continue
        games = parse_games(stdout_path)
        total = int(games[0].get("total") or 0) if games else None
        games = merge_pgnout_games(games, parse_pgnout_games(pgnout_path, total))
        tracks = collect_engine_tracks(log_dir, since_ms)
        games = synthesize_active_game_from_tracks(games, tracks)
        requested_game = read_selected_game(output_path)
        if requested_game is not None and requested_game != last_requested_game:
            locked_game = requested_game
            locked_moves = None
            last_requested_game = requested_game
        previous_locked_game = locked_game
        current, locked_game = select_board_game(games, locked_game)
        if locked_game != previous_locked_game:
            locked_moves = None
        if current is not None:
            now_ms = current_epoch_ms()
            state, locked_moves = select_engine_state(log_dir, games, current, locked_moves, since_ms, tracks, now_ms)
            started_at_ms = game_started_at_ms(current, state, game_start_times)
            current = dict(current)
            current["started_at_ms"] = started_at_ms
            current = game_with_timeout(current, state, now_ms)
            text = pgn_text(build_game(current, state, now_ms))
            board_output_path = game_output_path(output_path, current, started_at_ms)
            if board_output_path != previous_board_path:
                previous = None
                previous_board_path = board_output_path
            if text != previous:
                board_output_path.write_text(text, encoding="utf-8")
                previous = text
        if games:
            status_games = [
                current if current is not None and int(game["game"]) == int(current["game"]) else game
                for game in games
            ]
            status = status_text(status_games, previous_board_path or output_path, locked_game, output_path)
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
