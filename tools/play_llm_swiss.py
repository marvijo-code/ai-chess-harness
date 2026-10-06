"""Swiss tournament with live Elo between LLM players through llm-chess-engine.

Players come from a JSON config (see configs/llm-swiss-ai-players.json): each has a
name plus a `provider`/`model`/`effort` route understood by llm-chess-engine
(`codex`, `claude`, `openrouter-chat`, `opencode-go`, `zai`). Every route gets the
same prompt and the same rules:

- the model picks every move itself (no tools, no code, no engine, no fallback move);
- MaxAttempts replies per move, each retry says why the last one was rejected, then
  `bestmove 0000` = forfeit, recorded as a loss;
- each player has its own game clock (default 10 minutes, optional increment); a flag is a loss.

Pairing is Swiss: score groups first, then Elo, no rematches, one bye per player at
most (a bye scores `byePoints`, default 0, and leaves Elo unchanged), and a look-ahead that keeps the
remaining rounds pairable. Elo starts at `startElo` and updates after every game
(K = `eloK`). Games of one round run at the same time.

Live state goes to out/live/<slug>-tournament.json (read by tools/llm_tournament_viewer.py);
each game also gets out/live/<slug>-r<R>b<B>-live.pgn. The archive is
out/llm-tournaments/<slug>.pgn + .json.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import random
import sys
import threading
import time
from functools import lru_cache
from pathlib import Path

import chess
import chess.pgn

sys.path.insert(0, str(Path(__file__).resolve().parent))
from play_llm_series import LlmEngine, iso_now, move_comment, parse_info, write_text_retry  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
LIVE_DIR = ROOT / "out" / "live"
ARCHIVE_DIR = ROOT / "out" / "llm-tournaments"
DEFAULT_CONFIG = ROOT / "configs" / "llm-swiss-ai-players.json"
HEARTBEAT_SECONDS = 5
# Wall-clock grace on top of the mover's clock before the runner stops waiting for
# `bestmove` (CLI start-up per attempt is not charged to the chess clock).
FLAG_GRACE_SECONDS = 150

DEFAULTS = {
    "title": "AI Chess Swiss",
    "rounds": 5,
    "timeControlMs": 600_000,
    "incrementMs": 0,
    "maxAttempts": 3,
    "attemptTimeoutSeconds": 300,
    "maxPlies": 400,
    "showLegalMoves": True,
    "startElo": 1500,
    "eloK": 32,
    # Points come only from games (1 per win, 0.5 per draw). A bye scores nothing; in a full
    # round robin every player sits out once, so the final order is the same either way (owner 2026-10-06).
    "byePoints": 0.0,
    "seed": None,
    "players": [],
}


def load_config(path: Path) -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))
    cfg.update(json.loads(path.read_text(encoding="utf-8")))
    names = [p["name"] for p in cfg["players"]]
    if len(names) < 2 or len(set(names)) != len(names):
        raise SystemExit(f"config needs at least 2 players with unique names, got {names}")
    for player in cfg["players"]:
        player.setdefault("effort", "high")
    return cfg


# ---------------------------------------------------------------- Elo + standings


def elo_expected(rating: float, opponent: float) -> float:
    return 1.0 / (1.0 + 10 ** ((opponent - rating) / 400.0))


def elo_update(white: float, black: float, white_score: float, k: float) -> tuple[float, float]:
    delta = k * (white_score - elo_expected(white, black))
    return white + delta, black - delta


def points_for(result: str) -> tuple[float, float]:
    return {"1-0": (1.0, 0.0), "0-1": (0.0, 1.0), "1/2-1/2": (0.5, 0.5)}.get(result, (0.0, 0.0))


def compute_standings(state: dict) -> list[dict]:
    cfg = state["config"]
    rows = {
        p["name"]: {"name": p["name"], "points": 0.0, "elo": float(cfg["startElo"]), "played": 0, "wins": 0, "draws": 0,
                    "losses": 0, "forfeits": 0, "flags": 0, "byes": 0, "whites": 0, "opponents": [], "colors": [],
                    "invalid_attempts": 0}
        for p in state["players"]
    }
    for rnd in state["rounds"]:
        if rnd.get("bye"):
            rows[rnd["bye"]]["points"] += cfg["byePoints"]
            rows[rnd["bye"]]["byes"] += 1
        for pairing in rnd["pairings"]:
            game = state["games"].get(pairing["game_id"]) or {}
            white, black = rows[pairing["white"]], rows[pairing["black"]]
            result = game.get("result", "*")
            if result not in {"1-0", "0-1", "1/2-1/2"}:
                continue
            white["opponents"].append(black["name"])
            black["opponents"].append(white["name"])
            white["colors"].append("w")
            black["colors"].append("b")
            white["whites"] += 1
            ws, bs = points_for(result)
            game["elo_before"] = {"white": round(white["elo"], 1), "black": round(black["elo"], 1)}
            white["elo"], black["elo"] = elo_update(white["elo"], black["elo"], ws, cfg["eloK"])
            game["elo_after"] = {"white": round(white["elo"], 1), "black": round(black["elo"], 1)}
            for row, score, side in ((white, ws, "white"), (black, bs, "black")):
                row["points"] += score
                row["played"] += 1
                row["wins" if score == 1 else "losses" if score == 0 else "draws"] += 1
                row["invalid_attempts"] += int((game.get("invalid_attempts") or {}).get(side, 0))
            loser = None if result == "1/2-1/2" else (black if result == "1-0" else white)
            kind = game.get("end_kind", "")
            if loser is not None and kind == "forfeit":
                loser["forfeits"] += 1
            if loser is not None and kind == "flag":
                loser["flags"] += 1
    for row in rows.values():
        row["buchholz"] = sum(rows[name]["points"] for name in row["opponents"])
        row["elo_delta"] = round(row["elo"] - cfg["startElo"], 1)
        row["elo"] = round(row["elo"], 1)
    ordered = sorted(rows.values(), key=lambda r: (-r["points"], -r["buchholz"], -r["elo"], -r["wins"], r["name"]))
    for rank, row in enumerate(ordered, start=1):
        row["rank"] = rank
    return ordered


# ---------------------------------------------------------------- Swiss pairing


def all_matchings(names: tuple[str, ...]):
    if not names:
        yield ()
        return
    first, rest = names[0], names[1:]
    for index, other in enumerate(rest):
        remaining = rest[:index] + rest[index + 1:]
        for tail in all_matchings(remaining):
            yield ((first, other),) + tail


def pair_key(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


def schedule_feasible(names: tuple[str, ...], played: frozenset, byes: frozenset, rounds_left: int) -> bool:
    """True when `rounds_left` more rounds can be paired with no rematch and no second bye."""

    @lru_cache(maxsize=None)
    def ok(played_now: frozenset, byes_now: frozenset, left: int) -> bool:
        if left == 0:
            return True
        bye_options = [None] if len(names) % 2 == 0 else [n for n in names if n not in byes_now]
        for bye in bye_options:
            pool = tuple(n for n in names if n != bye)
            for matching in all_matchings(pool):
                keys = [pair_key(a, b) for a, b in matching]
                if any(k in played_now for k in keys):
                    continue
                if ok(played_now | frozenset(keys), byes_now | ({bye} if bye else set()), left - 1):
                    return True
        return False

    return ok(played, byes, rounds_left)


def max_rounds(count: int) -> int:
    """Rounds a no-rematch, one-bye-each Swiss can hold: a full round robin."""
    return count if count % 2 else count - 1


def make_pairings(state: dict, round_number: int) -> tuple[list[tuple[str, str]], str | None]:
    standings = {row["name"]: row for row in compute_standings(state)}
    order = [row["name"] for row in sorted(standings.values(), key=lambda r: r["rank"])]
    if round_number == 1:
        order = list(state["seed_order"])
    rank = {name: index for index, name in enumerate(order)}
    names = tuple(order)
    played = frozenset(pair_key(p["white"], p["black"]) for rnd in state["rounds"] for p in rnd["pairings"])
    byes = frozenset(rnd["bye"] for rnd in state["rounds"] if rnd.get("bye"))
    rounds_left_after = state["config"]["rounds"] - round_number
    best: tuple[float, list, str | None] | None = None
    bye_options = [None] if len(names) % 2 == 0 else [n for n in reversed(order) if n not in byes]
    for bye in bye_options:
        pool = tuple(n for n in names if n != bye)
        # Standard Swiss: the lowest-ranked player without a bye takes it.
        bye_cost = 0.0 if bye is None else (len(order) - 1 - rank[bye]) * 50.0
        for matching in all_matchings(pool):
            keys = [pair_key(a, b) for a, b in matching]
            if any(k in played for k in keys):
                continue
            cost = bye_cost
            for a, b in matching:
                cost += 100.0 * (standings[a]["points"] - standings[b]["points"]) ** 2
                cost += abs(standings[a]["elo"] - standings[b]["elo"]) / 100.0
                cost += abs(rank[a] - rank[b]) * 0.5
            if best is not None and cost >= best[0]:
                continue
            new_byes = byes | ({bye} if bye else set())
            if not schedule_feasible(names, played | frozenset(keys), frozenset(new_byes), rounds_left_after):
                continue
            best = (cost, list(matching), bye)
    if best is None:
        raise RuntimeError(f"no legal Swiss pairing for round {round_number}")
    _, matching, bye = best
    games = []
    for a, b in sorted(matching, key=lambda pair: min(rank[pair[0]], rank[pair[1]])):
        games.append(assign_colors(a, b, standings, rank))
    return games, bye


def assign_colors(a: str, b: str, standings: dict, rank: dict) -> tuple[str, str]:
    """Return (white, black): fewer whites first, then alternate from the last game, then the higher rank."""
    ra, rb = standings[a], standings[b]
    balance_a = ra["colors"].count("w") - ra["colors"].count("b")
    balance_b = rb["colors"].count("w") - rb["colors"].count("b")
    if balance_a != balance_b:
        return (a, b) if balance_a < balance_b else (b, a)
    last_a = ra["colors"][-1] if ra["colors"] else ""
    last_b = rb["colors"][-1] if rb["colors"] else ""
    if last_a != last_b:
        return (a, b) if last_a == "b" or last_b == "w" else (b, a)
    return (a, b) if rank[a] < rank[b] else (b, a)


# ---------------------------------------------------------------- state file


class TournamentState:
    def __init__(self, state: dict, status_path: Path) -> None:
        self.state = state
        self.status_path = status_path
        self.lock = threading.RLock()

    def save(self) -> None:
        with self.lock:
            self.state["standings"] = compute_standings(self.state)
            self.state["updated_at"] = iso_now()
            self.state["updated_epoch_ms"] = int(time.time() * 1000)
            write_text_retry(self.status_path, json.dumps(self.state, indent=1))


def parse_hurried(lines: list[str]) -> int:
    for line in reversed(lines):
        if line.startswith("info string hurried "):
            try:
                return int(line.split()[3])
            except (IndexError, ValueError):
                return 0
    return 0


def parse_think_ms(lines: list[str]) -> int | None:
    for line in reversed(lines):
        if line.startswith("info string thinkms "):
            try:
                return int(line.split()[3])
            except (IndexError, ValueError):
                return None
    return None


def wait_bestmove(engine: LlmEngine, timeout: float, on_clockstart) -> list[str]:
    """Like LlmEngine.wait_for('bestmove') but reacts to `info string clockstart <used_ms>` lines as they arrive."""
    deadline = time.monotonic() + timeout
    seen: list[str] = []
    with engine.cond:
        while True:
            while engine.lines:
                line = engine.lines.pop(0)
                if line == "__EOF__":
                    raise RuntimeError(f"{engine.name} engine exited")
                seen.append(line)
                if line.startswith("info string clockstart "):
                    try:
                        on_clockstart(int(line.split()[3]))
                    except (IndexError, ValueError):
                        pass
                if line.startswith("bestmove"):
                    return seen
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(f"{engine.name} gave no 'bestmove' within {timeout:.0f}s")
            engine.cond.wait(min(left, 1.0))


def uci_comment(lines: list[str], player: dict) -> str:
    """A UCI engine has no words: show its search depth and evaluation instead."""
    for line in reversed(lines):
        parts = line.split()
        if parts[:1] == ["info"] and "score" in parts and "depth" in parts:
            depth = parts[parts.index("depth") + 1]
            kind, value = parts[parts.index("score") + 1], parts[parts.index("score") + 2]
            score = f"mate in {value}" if kind == "mate" else f"{int(value) / 100:+.2f}"
            return f"Engine search at depth {depth}, evaluation {score} for the side to move."
    return f"Engine move at depth {player.get('depth', '?')}."


def kill_engine_tree(engine: LlmEngine) -> None:
    if os.name == "nt":
        import subprocess

        subprocess.run(["taskkill", "/T", "/F", "/PID", str(engine.proc.pid)], capture_output=True)
    else:
        engine.proc.kill()


# ---------------------------------------------------------------- one game


def play_game(game_id: str, white: LlmEngine, black: LlmEngine, cfg: dict, ts: TournamentState,
              live_pgn: Path, log, replace_engine) -> None:
    state = ts.state
    record = state["games"][game_id]
    board = chess.Board()
    game = chess.pgn.Game()
    headers = game.headers
    headers["Event"] = state["title"]
    headers["Site"] = "chess-harness-codex"
    headers["Date"] = time.strftime("%Y.%m.%d")
    headers["Round"] = f"{record['round']}.{record['board']}"
    headers["White"] = white.name
    headers["Black"] = black.name
    headers["Result"] = "*"
    headers["TournamentId"] = state["id"]
    headers["WhiteProvider"] = f"{white.player['provider']} {white.player['model']} {white.player['effort']}"
    headers["BlackProvider"] = f"{black.player['provider']} {black.player['model']} {black.player['effort']}"
    headers["MaxAttempts"] = str(cfg["maxAttempts"])
    headers["TimeControl"] = f"{cfg['timeControlMs'] // 1000}+{cfg['incrementMs'] // 1000}"
    headers["GameStartTime"] = iso_now()
    clocks = {chess.WHITE: cfg["timeControlMs"], chess.BLACK: cfg["timeControlMs"]}
    invalid = {chess.WHITE: 0, chess.BLACK: 0}
    increment = cfg["incrementMs"]
    node = game
    history: list[str] = []
    with ts.lock:
        record.update({"status": "live", "start": iso_now(), "moves": [], "fen": board.fen(), "result": "*",
                       "termination": "", "end_kind": "", "invalid_attempts": {"white": 0, "black": 0},
                       "pgn_path": str(live_pgn), "thinking": None})

    def publish(running: bool | None, thinking_since: float | None = None) -> None:
        headers["WhiteClockMs"] = str(clocks[chess.WHITE])
        headers["BlackClockMs"] = str(clocks[chess.BLACK])
        headers["WhiteInvalidAttempts"] = str(invalid[chess.WHITE])
        headers["BlackInvalidAttempts"] = str(invalid[chess.BLACK])
        headers["ClockUpdatedAtEpochMs"] = str(int(time.time() * 1000))
        headers["ClockRunningSide"] = "" if running is None else ("White" if running == chess.WHITE else "Black")
        write_text_retry(live_pgn, str(game) + "\n\n")
        with ts.lock:
            record["fen"] = board.fen()
            record["plies"] = len(history)
            record["clocks"] = {"white": clocks[chess.WHITE], "black": clocks[chess.BLACK],
                                "running": "" if running is None else ("white" if running == chess.WHITE else "black"),
                                "updated_epoch_ms": int(time.time() * 1000)}
            record["invalid_attempts"] = {"white": invalid[chess.WHITE], "black": invalid[chess.BLACK]}
            record["thinking"] = None if thinking_since is None else {
                "side": "white" if running == chess.WHITE else "black", "since_epoch_ms": int(thinking_since * 1000)}
        ts.save()

    for engine in (white, black):
        engine.new_game()
    result, termination, end_kind = "*", "", ""
    while True:
        if board.is_game_over(claim_draw=True):
            outcome = board.outcome(claim_draw=True)
            result = board.result(claim_draw=True)
            termination = outcome.termination.name.replace("_", " ").lower() if outcome else "game over"
            end_kind = "board"
            break
        if len(history) >= cfg["maxPlies"]:
            result, termination, end_kind = "1/2-1/2", f"draw adjudicated at the {cfg['maxPlies']}-ply safety cap", "cap"
            break
        side = board.turn
        engine = white if side == chess.WHITE else black
        started = time.time()
        publish(side, None)  # clock frozen until the model actually starts thinking
        go_line = engine.go_command(clocks[chess.WHITE], clocks[chess.BLACK], increment)
        position = "position startpos" + (" moves " + " ".join(history) if history else "")
        wait = clocks[side] / 1000 + FLAG_GRACE_SECONDS

        def clockstart(used_ms: int, side=side) -> None:
            # Display only: the live clock ticks from "now minus thinking already used this move".
            with ts.lock:
                record["thinking"] = {"side": "white" if side == chess.WHITE else "black",
                                      "since_epoch_ms": int(time.time() * 1000) - used_ms}
            ts.save()

        thinking_file = live_pgn.parent / f"{state['id']}-{game_id}-ply{len(history) + 1}.thinking.txt"
        try:
            if not engine.is_uci:
                engine.send(f"setoption name ThinkingFile value {thinking_file}")
            engine.send(position)
            engine.send(go_line)
            lines = wait_bestmove(engine, wait, clockstart)
            uci = lines[-1].split()[1]
        except TimeoutError:
            uci, lines = "flag", []
        except Exception as exc:  # crashed engine: the harness never moves for it
            log(f"{game_id}: {engine.name} engine failure: {exc}")
            uci, lines = "0000", [f"info string engine failure: {exc}"]
        wall_ms = int((time.time() - started) * 1000)
        comment, tries, illegal = parse_info(lines)
        if engine.is_uci:
            # Its own "info string" lines are start-up notes (e.g. "Network replica 1: Shared memory"), not reasons.
            comment = uci_comment(lines, engine.player)
        if engine.is_uci:
            # Stockfish's "thinking" is its search: the info lines it printed for this move.
            search = [line for line in lines if line.startswith("info depth")]
            write_text_retry(thinking_file, "\n".join(search) + "\n")
        think_ms = parse_think_ms(lines)
        # The chess clock charges model thinking time; wall time only when the engine did not report it.
        elapsed_ms = wall_ms if think_ms is None else min(wall_ms, think_ms)
        clocks[side] -= elapsed_ms
        if uci == "flag" or clocks[side] <= 0:
            clocks[side] = 0
            result = "0-1" if side == chess.WHITE else "1-0"
            termination, end_kind = f"{engine.name} lost on time", "flag"
            if uci == "flag":
                replace_engine(engine)  # still thinking: restart it so the next game starts clean
            break
        if uci == "0000" and comment.startswith(("provider unavailable", "engine failure")):
            # A plan limit, lost login or crashed engine process is not chess: void the game; --resume replays it.
            result, termination, end_kind = "*", f"void: {engine.name} {comment}"[:200], "void"
            break
        if uci == "0000":
            invalid[side] += cfg["maxAttempts"]
            result = "0-1" if side == chess.WHITE else "1-0"
            if comment.startswith("engine failure"):
                termination = f"{engine.name} forfeited: {comment}"[:200]
            else:
                termination = f"{engine.name} forfeited after {cfg['maxAttempts']} invalid replies"
            end_kind = "forfeit"
            node.comment = (node.comment + " " if node.comment else "") + f"{engine.name} forfeits: {comment}"[:300]
            break
        invalid[side] += max(0, tries - 1)
        move = chess.Move.from_uci(uci)
        if move not in board.legal_moves:
            result = "0-1" if side == chess.WHITE else "1-0"
            termination, end_kind = f"{engine.name} forfeited with an illegal engine reply {uci}", "forfeit"
            break
        clocks[side] += increment
        san = board.san(move)
        board.push(move)
        history.append(uci)
        node = node.add_variation(move)
        node.comment = move_comment(clocks[side], elapsed_ms, tries, illegal, comment)
        with ts.lock:
            record["moves"].append({"ply": len(history), "side": "white" if side == chess.WHITE else "black",
                                    "san": san, "uci": uci, "elapsed_ms": elapsed_ms, "wall_ms": wall_ms, "tries": tries,
                                    "hurried": parse_hurried(lines),
                                    "illegal": illegal, "comment": comment[:400], "clock_ms": clocks[side]})
        log(f"{game_id} ply {len(history)}: {engine.name} {san} ({elapsed_ms / 1000:.1f}s, tries={tries})")
    headers["Result"] = result
    headers["Termination"] = termination
    headers["GameEndTime"] = iso_now()
    with ts.lock:
        record.update({"status": "void" if end_kind == "void" else "finished", "result": result,
                       "termination": termination, "end_kind": end_kind, "end": iso_now(), "pgn": str(game)})
    publish(None)
    log(f"{game_id} finished: {white.name} {result} {black.name} ({termination})")


# ---------------------------------------------------------------- preflight


def preflight(players: list[dict], cfg: dict, log) -> list[str]:
    """One real move per player (same prompt, clock and rules as the tournament)."""
    from concurrent.futures import ThreadPoolExecutor

    def probe(player: dict) -> str | None:
        engine = None
        started = time.time()
        try:
            engine = LlmEngine(player, cfg)
            engine.new_game()
            engine.send("position startpos moves e2e4")
            engine.send(engine.go_command(cfg["timeControlMs"], cfg["timeControlMs"], cfg["incrementMs"]))
            lines = engine.wait_for("bestmove", engine.move_budget_seconds())
            uci = lines[-1].split()[1]
            board = chess.Board()
            board.push_uci("e2e4")
            if uci == "0000" or chess.Move.from_uci(uci) not in board.legal_moves:
                return f"{player['name']}: no legal move - {parse_info(lines)[0]}"
            log(f"preflight ok: {player['name']} played 1...{board.san(chess.Move.from_uci(uci))} in {time.time() - started:.1f}s")
            return None
        except Exception as exc:
            return f"{player['name']} ({player['provider']} {player['model']}): {exc}"
        finally:
            if engine:
                engine.close()

    with ThreadPoolExecutor(max_workers=len(players)) as pool:
        return [msg for msg in pool.map(probe, players) if msg]


# ---------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--slug")
    parser.add_argument("--rounds", type=int)
    parser.add_argument("--time-control-ms", type=int)
    parser.add_argument("--resume", type=Path, help="Continue from a tournament state JSON; unfinished games restart.")
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--no-preflight", action="store_true")
    args = parser.parse_args(argv)

    def log(message: str) -> None:
        print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)

    if args.resume:
        state = json.loads(args.resume.read_text(encoding="utf-8"))
        cfg = state["config"]
        status_path = args.resume
        for rnd in state["rounds"]:
            for pairing in rnd["pairings"]:
                game = state["games"][pairing["game_id"]]
                if game.get("result", "*") == "*":
                    game.update({"status": "pending", "moves": [], "result": "*", "termination": "", "end_kind": ""})
        state["finished"] = False
        state.pop("paused", None)
    else:
        cfg = load_config(args.config)
        if args.rounds:
            cfg["rounds"] = args.rounds
        if args.time_control_ms:
            cfg["timeControlMs"] = args.time_control_ms
        cfg["rounds"] = max(1, min(cfg["rounds"], max_rounds(len(cfg["players"]))))
        stamp = time.strftime("%Y%m%d-%H%M%S")
        slug = args.slug or f"llm-swiss-{stamp}"
        seed = cfg.get("seed")
        if seed is None:
            seed = int(time.time())
        seed_order = [p["name"] for p in cfg["players"]]
        random.Random(seed).shuffle(seed_order)
        state = {
            "id": slug, "title": cfg["title"], "created_at": iso_now(), "config": {k: v for k, v in cfg.items() if k != "players"},
            "players": cfg["players"], "seed": seed, "seed_order": seed_order, "rounds": [], "games": {},
            "current_round": 0, "finished": False, "winner": None,
        }
        status_path = LIVE_DIR / f"{slug}-tournament.json"

    players = {p["name"]: p for p in state["players"]}
    if not args.no_preflight or args.preflight_only:
        log("preflight: one real move from every player")
        failures = preflight(list(players.values()), cfg, log)
        for failure in failures:
            log(f"PREFLIGHT FAILED: {failure}")
        if failures:
            return 2
        if args.preflight_only:
            return 0

    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    ts = TournamentState(state, status_path)
    ts.save()
    log(f"tournament {state['id']}: {len(players)} players, {cfg['rounds']} rounds, "
        f"{cfg['timeControlMs'] // 60000} min per player, {cfg['maxAttempts']} attempts per move")
    log(f"state: {status_path}")
    stop = threading.Event()

    def beat() -> None:
        while not stop.wait(HEARTBEAT_SECONDS):
            try:
                ts.save()
            except OSError:
                pass

    threading.Thread(target=beat, daemon=True).start()
    engines: dict[str, LlmEngine] = {}
    engine_lock = threading.Lock()

    def engine_for(name: str) -> LlmEngine:
        with engine_lock:
            engine = engines.get(name)
            if engine is None or engine.proc.poll() is not None:
                engine = LlmEngine(players[name], cfg)
                engines[name] = engine
            return engine

    def replace_engine(engine: LlmEngine) -> None:
        with engine_lock:
            try:
                kill_engine_tree(engine)
            except Exception:
                pass
            engines.pop(engine.name, None)

    try:
        for round_number in range(1, cfg["rounds"] + 1):
            existing = next((r for r in state["rounds"] if r["round"] == round_number), None)
            if existing is None:
                pairings, bye = make_pairings(state, round_number)
                rnd = {"round": round_number, "bye": bye, "pairings": [], "status": "live"}
                for board_number, (white, black) in enumerate(pairings, start=1):
                    game_id = f"r{round_number}b{board_number}"
                    rnd["pairings"].append({"board": board_number, "white": white, "black": black, "game_id": game_id})
                    state["games"][game_id] = {"id": game_id, "round": round_number, "board": board_number, "white": white,
                                               "black": black, "status": "pending", "result": "*", "moves": []}
                with ts.lock:
                    state["rounds"].append(rnd)
            else:
                rnd = existing
            state["current_round"] = round_number
            ts.save()
            log(f"round {round_number}: " + ", ".join(f"{p['white']} - {p['black']}" for p in rnd["pairings"])
                + (f"; bye: {rnd['bye']}" if rnd.get("bye") else ""))
            threads = []
            for pairing in rnd["pairings"]:
                if state["games"][pairing["game_id"]].get("result", "*") != "*":
                    continue
                live_pgn = LIVE_DIR / f"{state['id']}-{pairing['game_id']}-live.pgn"
                thread = threading.Thread(
                    target=play_game,
                    args=(pairing["game_id"], engine_for(pairing["white"]), engine_for(pairing["black"]), cfg, ts,
                          live_pgn, log, replace_engine),
                    daemon=True,
                )
                thread.start()
                threads.append(thread)
            for thread in threads:
                thread.join()
            void = [state["games"][p["game_id"]] for p in rnd["pairings"] if state["games"][p["game_id"]].get("end_kind") == "void"]
            if void:
                with ts.lock:
                    state["paused"] = "; ".join(g["termination"] for g in void)
                ts.save()
                log(f"PAUSED in round {round_number}: {state['paused']}. Fix the provider, then run with --resume {status_path}")
                return 3
            rnd["status"] = "finished"
            ts.save()
            write_archive(state)
            leader = state["standings"][0]
            log(f"round {round_number} done; leader {leader['name']} {leader['points']:g} pts, Elo {leader['elo']:.0f}")
        state["finished"] = True
        state["winner"] = state["standings"][0]["name"] if state.get("standings") else None
        ts.save()
        write_archive(state)
        log("final standings: " + "; ".join(f"{r['rank']}. {r['name']} {r['points']:g} pts Elo {r['elo']:.0f}"
                                            for r in state["standings"]))
        return 0
    finally:
        stop.set()
        for engine in list(engines.values()):
            engine.close()


def write_archive(state: dict) -> None:
    pgns = [state["games"][p["game_id"]].get("pgn", "") for rnd in state["rounds"] for p in rnd["pairings"]]
    write_text_retry(ARCHIVE_DIR / f"{state['id']}.pgn", "\n\n".join(p for p in pgns if p) + "\n\n")
    write_text_retry(ARCHIVE_DIR / f"{state['id']}.json", json.dumps(state, indent=1))


if __name__ == "__main__":
    sys.exit(main())
