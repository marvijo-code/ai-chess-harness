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
import traceback
from functools import lru_cache
from pathlib import Path

import chess
import chess.pgn

sys.path.insert(0, str(Path(__file__).resolve().parent))
import disk_guard  # noqa: E402
from play_llm_series import (LlmEngine, iso_now, move_comment, parse_info, parse_compacted, parse_memory, parse_note, parse_usage,  # noqa: E402
                             write_text_retry)

ROOT = Path(__file__).resolve().parents[1]
LIVE_DIR = ROOT / "out" / "live"
ARCHIVE_DIR = ROOT / "out" / "llm-tournaments"
DEFAULT_CONFIG = ROOT / "configs" / "llm-swiss-ai-players.json"
HEARTBEAT_SECONDS = 5
# Wall-clock grace on top of the mover's clock before the runner stops waiting for
# `bestmove` (CLI start-up per attempt is not charged to the chess clock).
FLAG_GRACE_SECONDS = 150
# A state save that fails (disk full) is tried again after these waits; after that the heartbeat keeps trying
# every HEARTBEAT_SECONDS. While saves fail, one log line a minute.
SAVE_RETRY_DELAYS = (0.5, 2.0, 5.0)
SAVE_LOG_EVERY_SECONDS = 60
# A game whose thread stopped on an error is replayed from the moves it already has, at most this often,
# after these waits (seconds). Then the game is void and the tournament pauses instead of closing the round.
MAX_GAME_RESTARTS = 3
GAME_RESTART_WAIT_SECONDS = (5, 20, 60)

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
    # "swiss" (default) or "round-robin+knockout": everyone plays everyone once (rounds = players - 1),
    # then the top knockoutSize play semifinals and a final; a drawn knockout game goes to an Armageddon decider.
    "format": "swiss",
    "knockoutSize": 4,
    "armageddonWhiteMs": 600_000,
    "armageddonBlackMs": 450_000,
    # Free disk (MB) on the disk of the live folder, checked before a tournament and before every round:
    # under diskWarnMb a warning, under diskRefuseMb no new tournament starts. 0 = that check is off.
    "diskWarnMb": disk_guard.DISK_WARN_MB,
    "diskRefuseMb": disk_guard.DISK_REFUSE_MB,
    "players": [],
}
KO_STAGES = ("semifinals", "final")
# Moves of a game that count as "warm-up" for the cache hit rate (the first request of a session is all new).
CACHE_WARMUP_MOVES = 3


class GameHooks:
    """Extension points for a runner that wraps the tournament (tools/ai_chess_forever.py).

    before_game(ts, game_id, white, black): after both engines got `ucinewgame`, before the first move.
    after_game(ts, game_id): after the game record is final (result, PGN), before the round moves on.
    Both run on the game's own thread; an exception is logged and never stops the game or the round."""

    def before_game(self, ts: "TournamentState", game_id: str, white: LlmEngine, black: LlmEngine) -> None:
        pass

    def after_game(self, ts: "TournamentState", game_id: str) -> None:
        pass

    def player_for_game(self, player: dict) -> dict:
        """The player record the next game of this player runs on (asked once per game, before its engine is
        taken). A runner may return a copy with another provider and model: a second route while the first
        one is down. May block while no route is allowed."""
        return player


HOOKS: GameHooks | None = None


def route_of(player: dict) -> tuple[str, str]:
    """(provider, model): what an engine process is bound to."""
    return str(player.get("provider") or ""), str(player.get("model") or "")
# At most this many games at the same time (config maxConcurrentGames; None = every board of the round).
GAME_SLOTS: threading.Semaphore | None = None


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


def loss_or_dead_draw(board: chess.Board, loser: chess.Color) -> str:
    """Result when `loser` flags or forfeits: a loss, unless the opponent cannot checkmate by any legal sequence.

    FIDE Laws 6.9 (flag fall) and 7.5.5 (illegal-move forfeit): then the game is drawn. python-chess only says
    "insufficient" when mate is impossible from the material alone (bare king, K+B, K+N), as Lichess does.
    """
    if board.has_insufficient_material(not loser):
        return "1/2-1/2"
    return "0-1" if loser == chess.WHITE else "1-0"


def dead_draw_note(engine_name: str, how: str, opponent_name: str) -> str:
    return f"{engine_name} {how}, but {opponent_name} cannot checkmate: draw (insufficient material)"


def compute_standings(state: dict) -> list[dict]:
    cfg = state["config"]
    rows = {
        p["name"]: {"name": p["name"], "points": 0.0, "elo": float(cfg["startElo"]), "played": 0, "wins": 0, "draws": 0,
                    "losses": 0, "forfeits": 0, "flags": 0, "byes": 0, "whites": 0, "opponents": [], "colors": [],
                    "invalid_attempts": 0, "beat": [], "drew": []}
        for p in state["players"]
    }
    for rnd in state["rounds"]:
        if rnd.get("stage") in KO_STAGES:
            continue  # knockout games crown the champion; the table is the round robin / Swiss only
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
            for row, other, score in ((white, black, ws), (black, white, bs)):
                (row["beat"] if score == 1 else row["drew"] if score == 0.5 else []).append(other["name"])
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
    for row in rows.values():
        # Sonneborn-Berger: points of the players you beat, plus half of those you drew (round-robin tie-break).
        row["sb"] = sum(rows[n]["points"] for n in row.pop("beat")) + 0.5 * sum(rows[n]["points"] for n in row.pop("drew"))
        row["elo_delta"] = round(row["elo"] - cfg["startElo"], 1)
        row["elo"] = round(row["elo"], 1)
    ordered = sorted(rows.values(), key=lambda r: (-r["points"], -r["sb"], -r["buchholz"], -r["wins"], -r["elo"], r["name"]))
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


def print_log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


class TournamentState:
    def __init__(self, state: dict, status_path: Path, log=None, sleep=time.sleep) -> None:
        self.state = state
        self.status_path = status_path
        self.lock = threading.RLock()
        self.log = log or print_log
        self.sleep = sleep
        self.failed_saves = 0                      # failed saves in a row (0 = the file on disk is current)
        self.failing_since: float | None = None    # time.monotonic() of the first failed save of this run
        self._reported_at = 0.0

    def save(self) -> bool:
        """Write the state file. Never raises, so a save can never end a game thread.

        A failed write (a full disk) is tried again with a backoff and then reported in ONE clear log line.
        The state stays in memory and the games go on; every later save tries again (the heartbeat does so
        every HEARTBEAT_SECONDS) and one line says when it works again. True = the file on disk is current."""
        delays = SAVE_RETRY_DELAYS if self.failing_since is None else ()   # back off once, not on every move
        error: Exception | None = None
        for wait in (*delays, None):
            with self.lock:
                try:
                    self.state["standings"] = compute_standings(self.state)
                    self.state["cache_stats"] = compute_cache_stats(self.state)
                    self.state["updated_at"] = iso_now()
                    self.state["updated_epoch_ms"] = int(time.time() * 1000)
                    write_text_retry(self.status_path, json.dumps(self.state, indent=1), delays=())
                    error = None
                except Exception as exc:
                    error = exc
            if error is None:
                self._saved()
                return True
            if wait is not None:
                self.sleep(wait)
        self._save_failed(error, tries=len(delays) + 1)
        return False

    def _saved(self) -> None:
        with self.lock:
            failed, since = self.failed_saves, self.failing_since
            self.failed_saves, self.failing_since = 0, None
        if since is not None:
            self._say(f"STATE SAVE works again after {failed} failed saves in {time.monotonic() - since:.0f} s: "
                      f"{self.status_path.name} is current, nothing was lost")

    def _save_failed(self, error: Exception, tries: int) -> None:
        now = time.monotonic()
        with self.lock:
            first = self.failing_since is None
            if first:
                self.failing_since = now
            self.failed_saves += 1
            due = first or now - self._reported_at >= SAVE_LOG_EVERY_SECONDS
            if due:
                self._reported_at = now
            failed, since = self.failed_saves, self.failing_since
        if not due:
            return
        why = disk_guard.describe_error(error)
        free = disk_guard.size_text(disk_guard.disk_status(self.status_path.parent).get("free_mb"))
        if first:
            self._say(f"STATE SAVE FAILED after {tries} tries: {why}, {free} free; {self.status_path.name} is stale. "
                      f"The state is kept in memory, the games go on, and it is tried again every {HEARTBEAT_SECONDS} s.")
        else:
            self._say(f"STATE SAVE still failing: {failed} failed saves in {now - since:.0f} s ({why}, {free} free); "
                      "the games go on from memory")

    def _say(self, message: str) -> None:
        try:
            self.log(message)
        except Exception:
            pass


_quiet_reported: dict[str, float] = {}


def write_quiet(path: Path, text: str, log, what: str) -> bool:
    """A file a game can live without (its live PGN, a thinking text): a failed write is logged (one line a
    minute per kind of file), never raised."""
    try:
        write_text_retry(path, text, delays=(0.2,))
        return True
    except Exception as exc:
        now = time.monotonic()
        if now - _quiet_reported.get(what, -1e9) >= SAVE_LOG_EVERY_SECONDS:
            _quiet_reported[what] = now
            try:
                log(f"WRITE FAILED ({what}, {Path(path).name}): {disk_guard.describe_error(exc)}; the game goes on without it")
            except Exception:
                pass
        return False


def check_disk(path: Path, cfg: dict, log, when: str, starting: bool = False) -> dict:
    """Free-disk check with one log line when space is short. `starting`: the gate before a NEW tournament;
    it speaks only when it refuses (level "critical"), the round 1 check a moment later gives the warning."""
    status = disk_guard.disk_status(path, cfg.get("diskWarnMb"), cfg.get("diskRefuseMb"))
    free = disk_guard.size_text(status.get("free_mb"))
    if starting and status["level"] != "critical":
        return status
    if status["level"] == "critical":
        tail = ("No new tournament starts until space is freed." if starting else
                "The running tournament goes on with its state in memory; free space now.")
        log(f"DISK FULL ({when}): {free} free on the disk of {path}, under the {status['refuse_mb']:.0f} MB floor. {tail}")
    elif status["level"] == "low":
        log(f"DISK LOW ({when}): {free} free on the disk of {path}, under the {status['warn_mb'] / 1024:.1f} GB "
            "warning level. A full disk stops state saves: free space soon.")
    elif status["level"] == "unknown":
        log(f"DISK CHECK failed ({when}): {status.get('error', 'no reading')}")
    return status


def compute_cache_stats(state: dict) -> dict:
    """Per player: input tokens, cached input tokens and the hit rate (cached / input) over every move request
    in this tournament, plus the warm rate that leaves out each game's first CACHE_WARMUP_MOVES moves of the
    player (a session's first request has nothing cached yet)."""
    stats: dict[str, dict] = {}
    for game in (state.get("games") or {}).values():
        seen = {"white": 0, "black": 0}
        for move in game.get("moves") or []:
            usage = move.get("usage")
            side = move.get("side")
            if not usage or side not in seen:
                continue
            name = game.get(side)
            row = stats.setdefault(name, {"calls": 0, "input": 0, "cached": 0, "output": 0, "warm_input": 0, "warm_cached": 0})
            seen[side] += 1
            row["calls"] += int(usage.get("calls") or 0)
            row["input"] += int(usage.get("input") or 0)
            row["cached"] += int(usage.get("cached") or 0)
            row["output"] += int(usage.get("output") or 0)
            if seen[side] > CACHE_WARMUP_MOVES:
                row["warm_input"] += int(usage.get("input") or 0)
                row["warm_cached"] += int(usage.get("cached") or 0)
    for row in stats.values():
        row["hit_rate"] = round(row["cached"] / row["input"], 4) if row["input"] else None
        row["warm_hit_rate"] = round(row["warm_cached"] / row["warm_input"], 4) if row["warm_input"] else None
    return stats


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


def wait_bestmove(engine: LlmEngine, timeout: float, on_clockstart, on_limitwait=None) -> list[str]:
    """Like LlmEngine.wait_for('bestmove') but reacts to `info string clockstart <used_ms>` lines as they arrive.

    `info string limitwait <seconds> <reason>`: the engine waits out a usage limit and then asks the same
    move again. The deadline moves out by that wait (it is not thinking time), so a limit is never a flag."""
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
                if line.startswith("info string limitwait "):
                    parts = line.split(maxsplit=4)
                    try:
                        wait_s = int(parts[3])
                    except (IndexError, ValueError):
                        wait_s = 60
                    deadline += wait_s + 60
                    if on_limitwait is not None:
                        try:
                            on_limitwait(wait_s, parts[4] if len(parts) > 4 else "")
                        except Exception:
                            pass
                if line.startswith("info string infrawait "):
                    # Not thinking and not a usage limit (a compaction finishing): only the deadline moves out.
                    try:
                        deadline += int(line.split()[3]) + 60
                    except (IndexError, ValueError):
                        deadline += 360
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
              live_pgn: Path, log, replace_engine, start_ms: dict | None = None, resume: bool = False) -> None:
    """One game. With GAME_SLOTS set, the game waits (status pending) until a board is free."""
    slots = GAME_SLOTS
    more = {"resume": True} if resume else {}
    if slots is None:
        return _play_game(game_id, white, black, cfg, ts, live_pgn, log, replace_engine, start_ms, **more)
    with slots:
        return _play_game(game_id, white, black, cfg, ts, live_pgn, log, replace_engine, start_ms, **more)


def game_is_final(record: dict) -> bool:
    """The game has its result (or is void, which pauses the tournament): nothing more to play."""
    return record.get("end_kind") == "void" or record.get("result", "*") != "*"


def replay_saved_moves(moves: list[dict], clocks: dict) -> tuple[chess.Board, list[str], dict]:
    """Board, UCI history and clocks after the moves a game already has in its record.

    Used when a game goes on after its thread stopped. Every move here was made by a player; nothing is
    chosen. ValueError when the saved moves are not one legal game from the start position."""
    board, history, clocks = chess.Board(), [], dict(clocks)
    for index, entry in enumerate(moves, start=1):
        side = chess.WHITE if entry.get("side") == "white" else chess.BLACK
        if board.turn != side or entry.get("ply") != index:
            raise ValueError(f"ply {index} is out of order")
        try:
            move = chess.Move.from_uci(str(entry.get("uci")))
        except ValueError:
            raise ValueError(f"ply {index} is not a move: {entry.get('uci')!r}") from None
        if move not in board.legal_moves:
            raise ValueError(f"ply {index} {move.uci()} is not legal")
        board.push(move)
        history.append(move.uci())
        if isinstance(entry.get("clock_ms"), (int, float)):
            clocks[side] = int(entry["clock_ms"])
    return board, history, clocks


def play_game_guarded(game_id: str, white: LlmEngine, black: LlmEngine, cfg: dict, ts: TournamentState,
                      live_pgn: Path, log, replace_engine, start_ms: dict | None = None, engine_for=None,
                      sleep=time.sleep) -> None:
    """One game to a final record, whatever happens on its thread.

    An error that escapes a game used to end its thread without a word: the board stayed "live" with the
    moves it had and the round went on without it. Here that error is logged in one line and the game is
    replayed from the moves it already has, on fresh engine processes; every further move still comes from
    the players. After MAX_GAME_RESTARTS the game is void, which pauses the tournament (a resume replays the
    game) instead of closing the round without it."""
    record = ts.state["games"][game_id]
    resume = False
    for restart in range(MAX_GAME_RESTARTS + 1):
        try:
            if resume and engine_for is not None:
                # the old processes may still be busy with the move that was lost
                names = (white.name, black.name)
                replace_engine(white)
                replace_engine(black)
                white, black = engine_for(names[0]), engine_for(names[1])
            play_game(game_id, white, black, cfg, ts, live_pgn, log, replace_engine, start_ms,
                      **({"resume": True} if resume else {}))
            return
        except Exception as exc:
            why = f"{type(exc).__name__}: {exc}"[:240]
            frame = traceback.extract_tb(exc.__traceback__)[-1]
            where = f"{Path(frame.filename).name}:{frame.lineno} in {frame.name}"
            with ts.lock:
                final = game_is_final(record)
                plies = len(record.get("moves") or [])
            if final:
                log(f"GAME THREAD ERROR {game_id}: {why} at {where}, after the result was recorded "
                    f"({record.get('result')}); the result stands")
                return
            if restart >= MAX_GAME_RESTARTS:
                with ts.lock:
                    record.update({"status": "void", "result": "*", "end_kind": "void", "end": iso_now(), "thinking": None,
                                   "termination": f"void: the game stopped {restart + 1} times ({why})"[:200]})
                    record.pop("waiting", None)
                ts.save()
                log(f"GAME THREAD DIED {game_id}: {why} at {where}. Stopped {restart + 1} times: the game is void, "
                    "the tournament pauses and replays it; the round is not closed without it.")
                return
            wait = GAME_RESTART_WAIT_SECONDS[min(restart, len(GAME_RESTART_WAIT_SECONDS) - 1)]
            with ts.lock:
                record.setdefault("restarts", []).append({"at": iso_now(), "plies": plies, "error": why[:200]})
                record["thinking"] = None
                record.pop("waiting", None)
            ts.save()
            log(f"GAME THREAD DIED {game_id}: {why} at {where}. The game goes on from its {plies} saved moves on "
                f"fresh engines in {wait} s (restart {restart + 1} of {MAX_GAME_RESTARTS}); no move is made for a player.")
            sleep(wait)
            resume = True


def _play_game(game_id: str, white: LlmEngine, black: LlmEngine, cfg: dict, ts: TournamentState,
               live_pgn: Path, log, replace_engine, start_ms: dict | None = None, resume: bool = False) -> None:
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
    if start_ms:
        clocks = {chess.WHITE: int(start_ms["white"]), chess.BLACK: int(start_ms["black"])}
        headers["WhiteStartMs"], headers["BlackStartMs"] = str(clocks[chess.WHITE]), str(clocks[chess.BLACK])
    if record.get("armageddon"):
        headers["Armageddon"] = "Black has draw odds"
    invalid = {chess.WHITE: 0, chess.BLACK: 0}
    increment = cfg["incrementMs"]
    node = game
    history: list[str] = []
    saved: list[dict] = []
    if resume:
        # The game goes on after its thread stopped: the moves it already has are put back on the board.
        with ts.lock:
            saved = [dict(m) for m in record.get("moves") or []]
            kept_invalid = dict(record.get("invalid_attempts") or {})
        try:
            board, history, clocks = replay_saved_moves(saved, clocks)
        except ValueError as exc:
            log(f"{game_id}: its saved moves cannot be replayed ({exc}); the game starts again from move 1")
            saved = []
    if saved:
        for entry, uci in zip(saved, history):
            node = node.add_variation(chess.Move.from_uci(uci))
            node.comment = move_comment(entry.get("clock_ms"), int(entry.get("elapsed_ms") or 0),
                                        int(entry.get("tries") or 1), list(entry.get("illegal") or []),
                                        str(entry.get("comment") or ""))
        invalid = {chess.WHITE: int(kept_invalid.get("white") or 0), chess.BLACK: int(kept_invalid.get("black") or 0)}
        headers["GameStartTime"] = record.get("start") or headers["GameStartTime"]
        with ts.lock:
            record.update({"status": "live", "fen": board.fen(), "result": "*", "termination": "", "end_kind": "",
                           "pgn_path": str(live_pgn), "thinking": None})
            record.pop("waiting", None)
        log(f"{game_id}: goes on from ply {len(history) + 1} ({len(history)} saved moves put back)")
    else:
        with ts.lock:
            record.update({"status": "live", "start": iso_now(), "moves": [], "fen": board.fen(), "result": "*",
                           "termination": "", "end_kind": "", "invalid_attempts": {"white": 0, "black": 0},
                           "pgn_path": str(live_pgn), "thinking": None})
            if resume:
                record["notes"] = []
                record.pop("waiting", None)

    def publish(running: bool | None, thinking_since: float | None = None) -> None:
        headers["WhiteClockMs"] = str(clocks[chess.WHITE])
        headers["BlackClockMs"] = str(clocks[chess.BLACK])
        headers["WhiteInvalidAttempts"] = str(invalid[chess.WHITE])
        headers["BlackInvalidAttempts"] = str(invalid[chess.BLACK])
        headers["ClockUpdatedAtEpochMs"] = str(int(time.time() * 1000))
        headers["ClockRunningSide"] = "" if running is None else ("White" if running == chess.WHITE else "Black")
        write_quiet(live_pgn, str(game) + "\n\n", log, "live PGN")
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
    if HOOKS is not None:
        try:
            HOOKS.before_game(ts, game_id, white, black)
        except Exception as exc:  # a hook never stops a game
            log(f"{game_id}: before_game hook failed: {type(exc).__name__}: {exc}")
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
        opponent = black if side == chess.WHITE else white
        publish(side, None)  # clock frozen until the model actually starts thinking
        started = time.time()  # after the save: a slow disk is never charged to a player's clock
        go_line = engine.go_command(clocks[chess.WHITE], clocks[chess.BLACK], increment)
        position = "position startpos" + (" moves " + " ".join(history) if history else "")
        wait = clocks[side] / 1000 + FLAG_GRACE_SECONDS

        def clockstart(used_ms: int, side=side) -> None:
            # Display only: the live clock ticks from "now minus thinking already used this move".
            with ts.lock:
                record["thinking"] = {"side": "white" if side == chess.WHITE else "black",
                                      "since_epoch_ms": int(time.time() * 1000) - used_ms}
                record.pop("waiting", None)
            ts.save()

        def limitwait(wait_s: int, reason: str, side=side, engine=engine) -> None:
            # A usage limit: the clock is stopped (no thinking shown) until the engine asks again.
            due = time.time() + wait_s
            check = f"{time.strftime('%H:%M', time.gmtime(due + 7200))} SAST ({time.strftime('%H:%M', time.gmtime(due))} UTC)"
            log(f"{game_id}: {engine.name} waits {wait_s}s for a usage limit, next check at {check} "
                f"({reason[:120]}); clock stopped")
            with ts.lock:
                record["thinking"] = None
                record["waiting"] = {"side": "white" if side == chess.WHITE else "black", "reason": reason[:200],
                                     "until_epoch_ms": int((time.time() + wait_s) * 1000)}
                waits = record.setdefault("limit_waits", [])
                waits.append({"ply": len(history) + 1, "player": engine.name, "seconds": wait_s, "at": iso_now(),
                              "reason": reason[:200]})
                del waits[:-50]
            ts.save()

        thinking_file = live_pgn.parent / f"{state['id']}-{game_id}-ply{len(history) + 1}.thinking.txt"
        try:
            if not engine.is_uci:
                engine.send(f"setoption name ThinkingFile value {thinking_file}")
            engine.send(position)
            engine.send(go_line)
            lines = wait_bestmove(engine, wait, clockstart, limitwait)
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
            write_quiet(thinking_file, "\n".join(search) + "\n", log, "engine search text")
        think_ms = parse_think_ms(lines)
        # The chess clock charges model thinking time; wall time only when the engine did not report it.
        elapsed_ms = wall_ms if think_ms is None else min(wall_ms, think_ms)
        clocks[side] -= elapsed_ms
        if uci == "flag" or clocks[side] <= 0:
            clocks[side] = 0
            result = loss_or_dead_draw(board, side)
            termination, end_kind = f"{engine.name} lost on time", "flag"
            if result == "1/2-1/2":
                termination = dead_draw_note(engine.name, "ran out of time", opponent.name)
            if uci == "flag":
                replace_engine(engine)  # still thinking: restart it so the next game starts clean
            break
        if uci == "0000" and comment.startswith(("provider unavailable", "engine failure")):
            # A plan limit, lost login or crashed engine process is not chess: void the game; --resume replays it.
            result, termination, end_kind = "*", f"void: {engine.name} {comment}"[:200], "void"
            break
        if uci == "0000":
            invalid[side] += cfg["maxAttempts"]
            result = loss_or_dead_draw(board, side)
            if comment.startswith("engine failure"):
                termination = f"{engine.name} forfeited: {comment}"[:200]
            else:
                termination = f"{engine.name} forfeited after {cfg['maxAttempts']} invalid replies"
            if result == "1/2-1/2":
                termination = dead_draw_note(engine.name, "forfeited", opponent.name)
            end_kind = "forfeit"
            node.comment = (node.comment + " " if node.comment else "") + f"{engine.name} forfeits: {comment}"[:300]
            break
        invalid[side] += max(0, tries - 1)
        move = chess.Move.from_uci(uci)
        if move not in board.legal_moves:
            result = loss_or_dead_draw(board, side)
            termination, end_kind = f"{engine.name} forfeited with an illegal engine reply {uci}", "forfeit"
            if result == "1/2-1/2":
                termination = dead_draw_note(engine.name, "made an illegal move", opponent.name)
            break
        clocks[side] += increment
        san = board.san(move)
        board.push(move)
        history.append(uci)
        node = node.add_variation(move)
        node.comment = move_comment(clocks[side], elapsed_ms, tries, illegal, comment)
        note = parse_note(lines)
        usage = parse_usage(lines)
        side_name = "white" if side == chess.WHITE else "black"
        with ts.lock:
            entry = {"ply": len(history), "side": side_name,
                     "san": san, "uci": uci, "elapsed_ms": elapsed_ms, "wall_ms": wall_ms, "tries": tries,
                     "hurried": parse_hurried(lines),
                     "illegal": illegal, "comment": comment[:400], "clock_ms": clocks[side]}
            if usage:
                entry["usage"] = usage
            memory_sha = parse_memory(lines)
            if memory_sha:
                entry["memory_sha"] = memory_sha
            compacted = parse_compacted(lines)
            if compacted:
                entry["compacted"] = compacted
                log(f"{game_id} ply {len(history)}: {engine.name} goes on in a fresh session from a summary of its "
                    f"thinking up to ply {compacted.get('plies')} (was {compacted.get('from_tokens')} input tokens)")
            if note:
                entry["note"] = note[:200]
                record.setdefault("notes", []).append({"ply": len(history), "side": side_name, "player": engine.name,
                                                       "san": san, "note": note[:200]})
                state.setdefault("latest_notes", {})[engine.name] = {"note": note[:200], "game": game_id,
                                                                      "ply": len(history), "san": san, "at": iso_now()}
            record["moves"].append(entry)
            record.pop("waiting", None)
        log(f"{game_id} ply {len(history)}: {engine.name} {san} ({elapsed_ms / 1000:.1f}s, tries={tries})")
    headers["Result"] = result
    headers["Termination"] = termination
    headers["GameEndTime"] = iso_now()
    with ts.lock:
        record.update({"status": "void" if end_kind == "void" else "finished", "result": result,
                       "termination": termination, "end_kind": end_kind, "end": iso_now(), "pgn": str(game)})
        record.pop("waiting", None)
    publish(None)
    log(f"{game_id} finished: {white.name} {result} {black.name} ({termination})")
    if HOOKS is not None and end_kind != "void":
        try:
            HOOKS.after_game(ts, game_id)
        except Exception as exc:  # memory, ladder and git never stop the tournament
            log(f"{game_id}: after_game hook failed: {type(exc).__name__}: {exc}")


def void_unfinished(state: dict, ts: "TournamentState", rnd: dict, log) -> list[str]:
    """The gate before a round or stage is closed: a game of it that still has no result (it is "live" or
    "pending" although its thread is gone) becomes void, so the tournament pauses and replays it. Returns
    the reasons (empty = every game of the round is final)."""
    reasons = []
    with ts.lock:
        for pairing in rnd["pairings"]:
            game = state["games"][pairing["game_id"]]
            if game_is_final(game):
                continue
            was = game.get("status")
            reason = f"void: {pairing['game_id']} had no result when its round ended (status {was})"
            game.update({"status": "void", "result": "*", "end_kind": "void", "termination": reason, "thinking": None})
            game.pop("waiting", None)
            reasons.append(reason)
    for reason in reasons:
        log(f"ROUND GATE: {reason}; the round stays open and the game is replayed")
    return reasons


# ---------------------------------------------------------------- knockouts


def knockout_seeds(state: dict, size: int) -> list[dict]:
    return [{"seed": i + 1, "name": r["name"], "points": r["points"]} for i, r in enumerate(compute_standings(state)[:size])]


def match_winner(match: dict, games: dict) -> tuple[str | None, str | None]:
    """(winner, decided_by) from the match's finished games: a decisive game wins; a drawn Armageddon is Black's."""
    for game_id in match["games"]:
        game = games.get(game_id) or {}
        result = game.get("result", "*")
        if result in ("1-0", "0-1"):
            return (game["white"] if result == "1-0" else game["black"]), ("armageddon" if game.get("armageddon") else "game")
        if result == "1/2-1/2" and game.get("armageddon"):
            return game["black"], "armageddon"
    return None, None


def new_ko_game(state: dict, rnd: dict, match: dict, white: str, black: str, armageddon: bool) -> str:
    board = len(rnd["pairings"]) + 1
    game_id = f"r{rnd['round']}b{board}"
    label = f"{match['label']} - Armageddon decider" if armageddon else (
        f"{match['label']} - Game 1" if match["stage"] == "semifinals" else match["label"])
    pairing = {"board": board, "white": white, "black": black, "game_id": game_id, "match": match["id"], "label": label}
    game = {"id": game_id, "round": rnd["round"], "board": board, "white": white, "black": black, "status": "pending",
            "result": "*", "moves": [], "match": match["id"], "label": label}
    if armageddon:
        pairing["armageddon"] = game["armageddon"] = True
        game["draw_odds"] = "black"
    rnd["pairings"].append(pairing)
    state["games"][game_id] = game
    match["games"].append(game_id)
    return game_id


def run_knockout_stage(state: dict, ts: "TournamentState", cfg: dict, stage: str, round_number: int, label: str,
                       matches: list[dict], engine_for, replace_engine, log) -> bool:
    """Play every match of one stage to a winner (game, then an Armageddon decider after a draw). False = paused."""
    with ts.lock:
        rnd = next((r for r in state["rounds"] if r["round"] == round_number), None)
        if rnd is None:
            rnd = {"round": round_number, "stage": stage, "label": label, "bye": None, "pairings": [], "status": "live"}
            state["rounds"].append(rnd)
        state["current_round"], state["stage"] = round_number, stage
        state["disk"] = disk_guard.public_status(check_disk(ts.status_path.parent, cfg, log, label))
    ts.save()
    log(f"{label}: " + ", ".join(f"{m['label']}: {m['a']} - {m['b']}" for m in matches))
    paused: list[str] = []

    def play_match(match: dict) -> None:
        try:
            run_match(match)
        except Exception as exc:  # never a silent end: the stage is not closed without this match
            why = f"{type(exc).__name__}: {exc}"[:200]
            log(f"MATCH THREAD DIED {match['label']}: {why}; the tournament pauses and plays the match again")
            paused.append(f"{match['label']} stopped ({why})")

    def run_match(match: dict) -> None:
        while True:
            winner, decided_by = match_winner(match, state["games"])
            if winner:
                with ts.lock:
                    match["winner"], match["decided_by"] = winner, decided_by
                ts.save()
                log(f"{match['label']}: {winner} wins ({decided_by})")
                return
            with ts.lock:
                pending = next((g for g in match["games"] if state["games"][g].get("result", "*") == "*"), None)
                if pending is None:
                    if match["games"]:  # the regular game was drawn: Armageddon, colours swapped
                        first = state["games"][match["games"][0]]
                        pending = new_ko_game(state, rnd, match, first["black"], first["white"], True)
                    else:  # higher seed (a) has White in the regular game
                        pending = new_ko_game(state, rnd, match, match["a"], match["b"], False)
            game = state["games"][pending]
            start = {"white": cfg["armageddonWhiteMs"], "black": cfg["armageddonBlackMs"]} if game.get("armageddon") else None
            live_pgn = LIVE_DIR / f"{state['id']}-{pending}-live.pgn"
            play_game_guarded(pending, engine_for(game["white"]), engine_for(game["black"]), cfg, ts, live_pgn, log,
                              replace_engine, start, engine_for=engine_for)
            if state["games"][pending].get("end_kind") == "void":
                paused.append(state["games"][pending].get("termination", "void"))
                return
            if not game_is_final(state["games"][pending]):
                paused.append(f"{pending} has no result")
                return

    threads = [threading.Thread(target=play_match, args=(m,), daemon=True) for m in matches if not m.get("winner")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    # The stage is closed only when every match has its winner and no game is left "live".
    paused += void_unfinished(state, ts, rnd, log)
    if not paused:
        paused += [f"{m['label']} has no winner" for m in matches if not m.get("winner")]
    if paused:
        with ts.lock:
            state["paused"] = "; ".join(paused)
        ts.save()
        return False
    rnd["status"] = "finished"
    ts.save()
    write_archive(state, log)
    return True


def run_knockouts(state: dict, ts: "TournamentState", cfg: dict, engine_for, replace_engine, log) -> bool:
    ko = state.setdefault("knockout", {})
    if not ko.get("seeds"):
        # Fewer than 4 players (benched players sit out): the top two meet in the final, no semifinals.
        size = 4 if min(int(cfg.get("knockoutSize", 4)), len(state["players"])) >= 4 else 2
        seeds = knockout_seeds(state, size)
        names = [s["name"] for s in seeds]
        if size >= 4:
            matches = [
                {"id": "sf1", "stage": "semifinals", "label": "Semifinal 1", "a": names[0], "b": names[3], "games": [],
                 "winner": None, "decided_by": None},
                {"id": "sf2", "stage": "semifinals", "label": "Semifinal 2", "a": names[1], "b": names[2], "games": [],
                 "winner": None, "decided_by": None}]
        else:
            matches = [{"id": "final", "stage": "final", "label": "Final", "a": names[0], "b": names[1], "games": [],
                        "winner": None, "decided_by": None}]
        with ts.lock:
            ko.update(seeds=seeds, champion=None, runner_up=None, third=None, matches=matches)
        log("knockout seeds: " + ", ".join(f"{s['seed']}. {s['name']} ({s['points']:g})" for s in seeds))
    rr = int(cfg["rounds"])
    semis = [m for m in ko["matches"] if m["stage"] == "semifinals"]
    if not semis:
        finals = [m for m in ko["matches"] if m["stage"] == "final"]
        if not run_knockout_stage(state, ts, cfg, "final", rr + 1, "Final", finals, engine_for, replace_engine, log):
            return False
        final = finals[0]
        with ts.lock:
            ko["champion"] = final["winner"]
            ko["runner_up"] = final["b"] if final["winner"] == final["a"] else final["a"]
            ko["third"] = None
            state["stage"] = "finished"
        ts.save()
        log(f"CHAMPION: {ko['champion']} (runner-up {ko['runner_up']})")
        return True
    if not run_knockout_stage(state, ts, cfg, "semifinals", rr + 1, "Semifinals", semis, engine_for, replace_engine, log):
        return False
    if not any(m["stage"] == "final" for m in ko["matches"]):
        losers = [m["b"] if m["winner"] == m["a"] else m["a"] for m in semis]
        with ts.lock:
            ko["matches"] += [
                {"id": "final", "stage": "final", "label": "Final", "a": semis[0]["winner"], "b": semis[1]["winner"],
                 "games": [], "winner": None, "decided_by": None},
                {"id": "third", "stage": "final", "label": "Third place", "a": losers[0], "b": losers[1], "games": [],
                 "winner": None, "decided_by": None}]
    finals = [m for m in ko["matches"] if m["stage"] == "final"]
    if not run_knockout_stage(state, ts, cfg, "final", rr + 2, "Final", finals, engine_for, replace_engine, log):
        return False
    final, third = finals
    with ts.lock:
        ko["champion"] = final["winner"]
        ko["runner_up"] = final["b"] if final["winner"] == final["a"] else final["a"]
        ko["third"] = third["winner"]
        state["stage"] = "finished"
    ts.save()
    log(f"CHAMPION: {ko['champion']} (runner-up {ko['runner_up']}, third {ko['third']})")
    return True


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
        state = prepare_resume(json.loads(args.resume.read_text(encoding="utf-8")))
        cfg = state["config"]
        status_path = args.resume
    else:
        cfg = load_config(args.config)
        if args.rounds:
            cfg["rounds"] = args.rounds
        if args.time_control_ms:
            cfg["timeControlMs"] = args.time_control_ms
        if cfg.get("format") == "round-robin+knockout":
            cfg["rounds"] = max_rounds(len(cfg["players"]))  # everyone plays everyone once
        cfg["rounds"] = max(1, min(cfg["rounds"], max_rounds(len(cfg["players"]))))
        if check_disk(LIVE_DIR, cfg, log, "before a new tournament", starting=True)["level"] == "critical":
            return 4
        stamp = time.strftime("%Y%m%d-%H%M%S")
        slug = args.slug or f"llm-swiss-{stamp}"
        state = new_state(cfg, slug)
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

    return run_tournament(state, cfg, status_path, log)


def prepare_resume(state: dict) -> dict:
    """Unfinished games of a saved state start again from move 1 (the --resume rule)."""
    for rnd in state["rounds"]:
        for pairing in rnd["pairings"]:
            game = state["games"][pairing["game_id"]]
            if game.get("result", "*") == "*":
                game.update({"status": "pending", "moves": [], "result": "*", "termination": "", "end_kind": "",
                             "notes": [], "thinking": None})
                game.pop("waiting", None)
    state["finished"] = False
    state.pop("paused", None)
    return state


def new_state(cfg: dict, slug: str, title: str | None = None, seed: int | None = None) -> dict:
    """A fresh tournament state (Elo starts at startElo for everyone)."""
    if seed is None:
        seed = cfg.get("seed")
    if seed is None:
        seed = int(time.time())
    seed_order = [p["name"] for p in cfg["players"]]
    random.Random(seed).shuffle(seed_order)
    return {
        "id": slug, "title": title or cfg["title"], "created_at": iso_now(),
        "config": {k: v for k, v in cfg.items() if k != "players"},
        "players": cfg["players"], "seed": seed, "seed_order": seed_order, "rounds": [], "games": {},
        "current_round": 0, "finished": False, "winner": None,
    }


def run_tournament(state: dict, cfg: dict, status_path: Path, log) -> int:
    """Play a (new or resumed) tournament to the end. 0 = finished, 3 = paused by a void game."""
    global GAME_SLOTS
    players = {p["name"]: p for p in state["players"]}
    cap = cfg.get("maxConcurrentGames")
    GAME_SLOTS = threading.Semaphore(int(cap)) if cap else None
    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    ts = TournamentState(state, status_path, log)
    ts.save()
    log(f"tournament {state['id']}: {len(players)} players, {cfg['rounds']} rounds, "
        f"{cfg['timeControlMs'] // 60000} min per player, {cfg['maxAttempts']} attempts per move")
    log(f"state: {status_path}")
    stop = threading.Event()

    def beat() -> None:
        while not stop.wait(HEARTBEAT_SECONDS):
            ts.save()  # never raises; while the disk is full this is the retry

    threading.Thread(target=beat, daemon=True).start()
    engines: dict[str, LlmEngine] = {}
    engine_lock = threading.Lock()

    def engine_for(name: str) -> LlmEngine:
        with engine_lock:
            player = players[name]
            if HOOKS is not None:
                try:
                    player = HOOKS.player_for_game(player) or player
                except Exception as exc:
                    log(f"{name}: player_for_game hook failed: {type(exc).__name__}: {exc}")
            engine = engines.get(name)
            if engine is not None and engine.proc.poll() is None and route_of(engine.player) != route_of(player):
                # another route for this game: the old engine process still talks to the old provider
                log(f"{name}: route changes from {' '.join(route_of(engine.player))} to {' '.join(route_of(player))}")
                try:
                    kill_engine_tree(engine)
                except Exception:
                    pass
                engine = None
            if engine is None or engine.proc.poll() is not None:
                engine = LlmEngine(player, cfg)
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
            if cfg.get("format") == "round-robin+knockout":
                state["stage"] = "round-robin"
                state["format"] = {"type": cfg["format"], "rr_rounds": cfg["rounds"], "ko_size": cfg.get("knockoutSize", 4)}
            state["disk"] = disk_guard.public_status(check_disk(
                status_path.parent, cfg, log, f"start of {state['id']}, round 1" if round_number == 1 else f"round {round_number}"))
            ts.save()
            log(f"round {round_number}: " + ", ".join(f"{p['white']} - {p['black']}" for p in rnd["pairings"])
                + (f"; bye: {rnd['bye']}" if rnd.get("bye") else ""))
            threads = []
            for pairing in rnd["pairings"]:
                if state["games"][pairing["game_id"]].get("result", "*") != "*":
                    continue
                live_pgn = LIVE_DIR / f"{state['id']}-{pairing['game_id']}-live.pgn"
                thread = threading.Thread(
                    target=play_game_guarded,
                    args=(pairing["game_id"], engine_for(pairing["white"]), engine_for(pairing["black"]), cfg, ts,
                          live_pgn, log, replace_engine),
                    kwargs={"engine_for": engine_for},
                    daemon=True,
                )
                thread.start()
                threads.append(thread)
            for thread in threads:
                thread.join()
            # A round is closed only when every game of it is final: a game left "live" becomes void here.
            void_unfinished(state, ts, rnd, log)
            void = [state["games"][p["game_id"]] for p in rnd["pairings"] if state["games"][p["game_id"]].get("end_kind") == "void"]
            if void:
                with ts.lock:
                    state["paused"] = "; ".join(g["termination"] for g in void)
                ts.save()
                log(f"PAUSED in round {round_number}: {state['paused']}. Fix the provider, then run with --resume {status_path}")
                return 3
            rnd["status"] = "finished"
            ts.save()
            write_archive(state, log)
            leader = state["standings"][0]
            log(f"round {round_number} done; leader {leader['name']} {leader['points']:g} pts, Elo {leader['elo']:.0f}")
        if cfg.get("format") == "round-robin+knockout":
            if not run_knockouts(state, ts, cfg, engine_for, replace_engine, log):
                log(f"PAUSED in the knockouts: {state.get('paused')}. Fix the provider, then run with --resume {status_path}")
                return 3
        state["finished"] = True
        champion = (state.get("knockout") or {}).get("champion")
        state["winner"] = champion or (state["standings"][0]["name"] if state.get("standings") else None)
        ts.save()
        write_archive(state, log)
        log("final standings: " + "; ".join(f"{r['rank']}. {r['name']} {r['points']:g} pts Elo {r['elo']:.0f}"
                                            for r in state["standings"]))
        return 0
    finally:
        stop.set()
        for engine in list(engines.values()):
            engine.close()


def write_archive(state: dict, log=None) -> None:
    """The archive copy (PGN + JSON), written again after every round. A failed write (a full disk) is one
    log line: the next round end writes it again, and the live state file holds the same games."""
    pgns = [state["games"][p["game_id"]].get("pgn", "") for rnd in state["rounds"] for p in rnd["pairings"]]
    write_quiet(ARCHIVE_DIR / f"{state['id']}.pgn", "\n\n".join(p for p in pgns if p) + "\n\n", log or print_log, "archive PGN")
    write_quiet(ARCHIVE_DIR / f"{state['id']}.json", json.dumps(state, indent=1), log or print_log, "archive JSON")


if __name__ == "__main__":
    sys.exit(main())
