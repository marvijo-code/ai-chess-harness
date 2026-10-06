"""Best-of-N series between two LLM players through llm-chess-engine.

Players are `provider:model:effort` specs, e.g. `codex:gpt-6-sol:high` (ChatGPT
subscription through `codex exec`), `claude:claude-sonnet-5-5:high` (Claude
subscription through `claude -p`) or `openrouter:<model id>:low`. Defaults come
from the `llmMatch` section of chess-harness.config.json.

The runner never picks a move for a model: the engine gets MaxAttempts replies
per move and returns `bestmove 0000` after the last failed one, which this
runner records as a forfeit.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import chess
import chess.pgn

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "chess-harness.config.json"
ENGINE_SCRIPT = ROOT / "engines" / "llm-chess-engine" / "llm_chess_uci.py"
ARCHIVE_DIR = ROOT / "out" / "llm-matches"
LIVE_DIR = ROOT / "out" / "live"
STATUS_HEARTBEAT_SECONDS = 20

DEFAULTS = {
    "games": 3,
    "timeControlMs": 3_600_000,
    "incrementMs": 30_000,
    "maxAttempts": 3,
    "attemptTimeoutSeconds": 300,
    "maxPlies": 400,
    "playAll": False,
    "showLegalMoves": True,
    "player1": {"name": "GPT-6 Sol (high)", "provider": "codex", "model": "gpt-6-sol", "effort": "high"},
    "player2": {"name": "Sonnet 5.5 (high)", "provider": "claude", "model": "claude-sonnet-5-5", "effort": "high"},
}


def load_match_config(path: Path = CONFIG_PATH) -> dict:
    merged = json.loads(json.dumps(DEFAULTS))
    try:
        section = json.loads(path.read_text(encoding="utf-8")).get("llmMatch") or {}
    except (OSError, json.JSONDecodeError, AttributeError):
        section = {}
    for key, value in section.items():
        if key in {"player1", "player2"} and isinstance(value, dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


def parse_player_spec(spec: str, name: str | None = None) -> dict:
    parts = spec.split(":")
    if len(parts) < 2:
        raise argparse.ArgumentTypeError(f"player spec must be provider:model[:effort], got {spec!r}")
    provider = parts[0].strip().lower()
    effort = parts[-1].strip() if len(parts) >= 3 else "high"
    model = ":".join(parts[1:-1] if len(parts) >= 3 else parts[1:]).strip()
    player = {"provider": provider, "model": model, "effort": effort}
    player["name"] = name or f"{model} ({effort})"
    return player


def slug_part(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())[:20] or "player"


def iso_now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def pgn_clock(ms: int) -> str:
    total = max(0, int(ms)) // 1000
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}"


def move_comment(clock_ms: int | None, elapsed_ms: int, tries: int, illegal: list[str], text: str) -> str:
    parts = []
    if clock_ms is not None:
        parts.append(f"[%clk {pgn_clock(clock_ms)}]")
    parts.append(f"[%emt {pgn_clock(elapsed_ms)}]")
    if tries > 1:
        parts.append(f"[%tries {tries}]")
    if illegal:
        parts.append(f"[%illegal {';'.join(illegal)}]")
    clean = " ".join(text.replace("{", "(").replace("}", ")").split())[:400]
    if clean:
        parts.append(clean)
    return " ".join(parts)


def series_decided(scores: list[float], played: int, total: int) -> bool:
    return abs(scores[0] - scores[1]) > (total - played)


def engine_env(player: dict, cfg: dict) -> dict[str, str]:
    env = {
        "LLM_PROVIDER": player["provider"],
        "LLM_MODEL": player["model"],
        "LLM_EFFORT": player["effort"],
        "LLM_MAX_ATTEMPTS": str(cfg["maxAttempts"]),
        "LLM_ATTEMPT_TIMEOUT_SECONDS": str(cfg["attemptTimeoutSeconds"]),
        "LLM_SHOW_LEGAL_MOVES": "true" if cfg["showLegalMoves"] else "false",
        "LLM_BOARD_IMAGE": "false" if player.get("image") is False else "true",
    }
    if player.get("maxPrice"):
        env["LLM_MAX_PRICE"] = json.dumps(player["maxPrice"])
    if player["provider"] == "openrouter":
        env.update({
            "OPENROUTER_MODEL": player["model"],
            "OPENROUTER_REASONING_EFFORT": player["effort"],
            "OPENROUTER_MAX_ATTEMPTS": str(cfg["maxAttempts"]),
            "OPENROUTER_TIMEOUT_SECONDS": str(cfg["attemptTimeoutSeconds"]),
        })
    return env


class LlmEngine:
    """One persistent engine process for one player: the llm-chess-engine, or a plain UCI engine
    (`"provider": "uci"`, `"path"`, `"depth"`, optional `"options"`) such as a fixed-depth Stockfish."""

    def __init__(self, player: dict, cfg: dict) -> None:
        self.player = player
        self.name = player["name"]
        self.cfg = cfg
        self.is_uci = player.get("provider") == "uci"
        env = os.environ.copy()
        # The pipe is read as UTF-8: without this a model's dash in a comment arrived as "�" (Windows cp1252).
        env["PYTHONIOENCODING"] = "utf-8"
        if not self.is_uci:
            env.update(engine_env(player, cfg))
        # A relative engine path is relative to the repo, whatever folder the runner was started from
        # (2026-10-06: a detached runner started in System32 could not find Stockfish and crashed).
        exe = Path(player["path"]) if self.is_uci else None
        if exe is not None and not exe.is_absolute():
            exe = ROOT / exe
        argv = [str(exe)] if exe is not None else [sys.executable, str(ENGINE_SCRIPT)]
        self.proc = subprocess.Popen(
            argv,
            cwd=str(exe.parent) if exe is not None else None,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.lines: list[str] = []
        self.cond = threading.Condition()
        threading.Thread(target=self._pump, daemon=True).start()
        self.send("uci")
        self.wait_for("uciok", 30)
        for name, value in (player.get("options") or {}).items():
            self.send(f"setoption name {name} value {value}")
        self.send("isready")
        self.wait_for("readyok", 30)

    def go_command(self, wtime: int, btime: int, inc: int = 0) -> str:
        if self.is_uci and self.player.get("depth"):
            return f"go depth {int(self.player['depth'])}"
        return f"go wtime {wtime} btime {btime} winc {inc} binc {inc}"

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            with self.cond:
                self.lines.append(line.rstrip("\n"))
                self.cond.notify_all()
        with self.cond:
            self.lines.append("__EOF__")
            self.cond.notify_all()

    def send(self, line: str) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()

    def wait_for(self, prefix: str, timeout: float) -> list[str]:
        deadline = time.monotonic() + timeout
        with self.cond:
            while True:
                for index, line in enumerate(self.lines):
                    if line.startswith(prefix):
                        seen = self.lines[: index + 1]
                        del self.lines[: index + 1]
                        return seen
                    if line == "__EOF__":
                        raise RuntimeError(f"{self.name} engine exited")
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError(f"{self.name} gave no {prefix!r} within {timeout:.0f}s")
                self.cond.wait(left)

    def new_game(self) -> None:
        self.send("ucinewgame")
        self.send("isready")
        self.wait_for("readyok", 30)

    def move_budget_seconds(self) -> float:
        return self.cfg["maxAttempts"] * (self.cfg["attemptTimeoutSeconds"] + 30) + 60

    def go(self, history: list[str], go_line: str) -> tuple[str, list[str]]:
        position = "position startpos" + (" moves " + " ".join(history) if history else "")
        self.send(position)
        self.send(go_line)
        lines = self.wait_for("bestmove", self.move_budget_seconds())
        return lines[-1].split()[1], lines

    def close(self) -> None:
        if self.proc.poll() is None:
            try:
                self.send("quit")
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()


def parse_info(lines: list[str]) -> tuple[str, int, list[str]]:
    comment, tries, illegal = "", 1, []
    for line in lines:
        if not line.startswith("info string "):
            continue
        text = line[len("info string "):]
        match = re.match(r"attempts tries=(\d+) illegal=(\S+)", text)
        if text.startswith(("thinkms ", "hurried ", "clockstart ")):
            continue
        if match:
            tries = int(match.group(1))
            illegal = [] if match.group(2) == "-" else match.group(2).split(";")
        else:
            comment = text
    return comment, tries, illegal


class SeriesWriter:
    """Writes the multi-game live PGN plus the status sidecar the viewer reads."""

    def __init__(self, live_pgn: Path, series: dict) -> None:
        self.live_pgn = live_pgn
        self.status_path = live_pgn.with_suffix(".status.json")
        self.series = series
        self.games: list[chess.pgn.Game] = []
        self.lock = threading.Lock()
        live_pgn.parent.mkdir(parents=True, exist_ok=True)

    def write(self) -> None:
        with self.lock:
            text = "\n\n".join(str(game) for game in self.games) + "\n\n"
            write_text_retry(self.live_pgn, text)
            write_text_retry(self.status_path, json.dumps(self.status_payload(), indent=2))

    def heartbeat(self) -> None:
        with self.lock:
            write_text_retry(self.status_path, json.dumps(self.status_payload(), indent=2))

    def status_payload(self) -> dict:
        rows = []
        for index, game in enumerate(self.games, start=1):
            result = game.headers.get("Result", "*")
            rows.append({
                "game": index,
                "total": self.series["games"],
                "white": game.headers.get("White", "White"),
                "black": game.headers.get("Black", "Black"),
                "result": result,
                "reason": game.headers.get("Termination", "") if result != "*" else "",
                "finished": result != "*",
            })
        return {
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "generated_at_epoch": time.time(),
            "output_pgn": str(self.live_pgn),
            "control_pgn": str(self.live_pgn),
            "locked_game": max(1, len(self.games)),
            "games": rows,
            "series": dict(self.series),
        }


def write_text_retry(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    for _ in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:  # the viewer may hold the file open for a moment on Windows
            time.sleep(0.05)
    path.write_text(text, encoding="utf-8")


def play_game(
    number: int,
    white: LlmEngine,
    black: LlmEngine,
    cfg: dict,
    series: dict,
    writer: SeriesWriter,
    log,
) -> dict:
    board = chess.Board()
    game = chess.pgn.Game()
    headers = game.headers
    headers["Event"] = f"LLM Match: {series['player1']} vs {series['player2']}"
    headers["Site"] = "chess-harness-codex"
    headers["Date"] = time.strftime("%Y.%m.%d")
    headers["Round"] = str(number)
    headers["White"] = white.name
    headers["Black"] = black.name
    headers["Result"] = "*"
    headers["SeriesId"] = series["id"]
    headers["SeriesGame"] = str(number)
    headers["SeriesGames"] = str(series["games"])
    headers["SeriesPlayer1"] = series["player1"]
    headers["SeriesPlayer2"] = series["player2"]
    headers["WhiteProvider"] = f"{white.player['provider']} {white.player['model']} {white.player['effort']}"
    headers["BlackProvider"] = f"{black.player['provider']} {black.player['model']} {black.player['effort']}"
    headers["MaxAttempts"] = str(cfg["maxAttempts"])
    headers["TimeControl"] = f"{cfg['timeControlMs'] // 1000}+{cfg['incrementMs'] // 1000}"
    headers["GameStartTime"] = iso_now()
    invalid = {chess.WHITE: 0, chess.BLACK: 0}
    clocks = {chess.WHITE: cfg["timeControlMs"], chess.BLACK: cfg["timeControlMs"]}
    increment = cfg["incrementMs"]

    def sync_headers(running: bool | None) -> None:
        headers["WhiteInvalidAttempts"] = str(invalid[chess.WHITE])
        headers["BlackInvalidAttempts"] = str(invalid[chess.BLACK])
        headers["WhiteClockMs"] = str(clocks[chess.WHITE])
        headers["BlackClockMs"] = str(clocks[chess.BLACK])
        headers["ClockUpdatedAtEpochMs"] = str(int(time.time() * 1000))
        headers["ClockRunningSide"] = "" if running is None else ("White" if running == chess.WHITE else "Black")

    writer.games.append(game)
    white.new_game()
    black.new_game()
    node = game
    history: list[str] = []
    moves: list[dict] = []
    result, termination = "*", ""
    while True:
        if board.is_game_over(claim_draw=True):
            outcome = board.outcome(claim_draw=True)
            result = board.result(claim_draw=True)
            termination = outcome.termination.name.replace("_", " ").lower() if outcome else "game over"
            break
        if len(history) >= cfg["maxPlies"]:
            result, termination = "1/2-1/2", f"draw adjudicated at the {cfg['maxPlies']}-ply safety cap"
            break
        side = board.turn
        engine = white if side == chess.WHITE else black
        sync_headers(side)
        writer.write()
        go_line = f"go wtime {clocks[chess.WHITE]} btime {clocks[chess.BLACK]} winc {increment} binc {increment}"
        started = time.monotonic()
        try:
            uci, lines = engine.go(history, go_line)
        except Exception as exc:  # engine hang or crash: the harness never moves for it
            log(f"game {number}: {engine.name} engine failure: {exc}")
            uci, lines = "0000", [f"info string engine failure: {exc}"]
        elapsed_ms = int((time.monotonic() - started) * 1000)
        comment, tries, illegal = parse_info(lines)
        invalid[side] += max(0, tries - 1) if uci != "0000" else cfg["maxAttempts"]
        clocks[side] -= elapsed_ms
        if clocks[side] <= 0:
            clocks[side] = 0
            result = "0-1" if side == chess.WHITE else "1-0"
            termination = f"{engine.name} lost on time"
            break
        if uci == "0000":
            result = "0-1" if side == chess.WHITE else "1-0"
            if comment.startswith("engine failure"):
                termination = f"{engine.name} forfeited: {comment}"[:200]
            else:
                termination = f"{engine.name} forfeited after {cfg['maxAttempts']} invalid replies"
            node.comment = (node.comment + " " if node.comment else "") + f"{engine.name} forfeits: {comment}"[:300]
            break
        move = chess.Move.from_uci(uci)
        if move not in board.legal_moves:
            result = "0-1" if side == chess.WHITE else "1-0"
            termination = f"{engine.name} forfeited with an illegal engine reply {uci}"
            break
        clocks[side] += increment
        san = board.san(move)
        board.push(move)
        history.append(uci)
        node = node.add_variation(move)
        node.comment = move_comment(clocks[side], elapsed_ms, tries, illegal, comment)
        moves.append({"ply": len(history), "player": engine.name, "san": san, "uci": uci, "elapsed_ms": elapsed_ms,
                      "tries": tries, "illegal": illegal, "comment": comment, "clock_ms": clocks[side]})
        log(f"game {number} ply {len(history)}: {engine.name} {san} ({elapsed_ms / 1000:.1f}s, tries={tries})")
    headers["Result"] = result
    headers["Termination"] = termination
    headers["GameEndTime"] = iso_now()
    sync_headers(None)
    writer.write()
    log(f"game {number} finished: {result} ({termination})")
    return {"game": number, "white": white.name, "black": black.name, "result": result, "termination": termination,
            "plies": len(history), "invalid_attempts": {"white": invalid[chess.WHITE], "black": invalid[chess.BLACK]},
            "moves": moves}


def preflight(players: list[dict], cfg: dict) -> list[str]:
    """One real move per player from the start position. Returns failure messages."""

    def probe(player: dict) -> str | None:
        engine = None
        try:
            engine = LlmEngine(player, cfg)
            engine.new_game()
            uci, lines = engine.go([], "go wtime 600000 btime 600000")
            if uci == "0000" or chess.Move.from_uci(uci) not in chess.Board().legal_moves:
                return f"{player['name']} ({player['provider']} {player['model']}): no legal move - {parse_info(lines)[0]}"
            return None
        except Exception as exc:
            return f"{player['name']} ({player['provider']} {player['model']}): {exc}"
        finally:
            if engine:
                engine.close()

    with ThreadPoolExecutor(max_workers=len(players)) as pool:
        return [msg for msg in pool.map(probe, players) if msg]


def main(argv: list[str] | None = None) -> int:
    cfg = load_match_config()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--player1", help="provider:model:effort (default from llmMatch.player1)")
    parser.add_argument("--player1-name")
    parser.add_argument("--player2", help="provider:model:effort (default from llmMatch.player2)")
    parser.add_argument("--player2-name")
    parser.add_argument("--games", type=int, default=cfg["games"])
    parser.add_argument("--play-all", action="store_true", default=bool(cfg["playAll"]), help="Play every game even after the series is decided.")
    parser.add_argument("--time-control-ms", type=int, default=cfg["timeControlMs"])
    parser.add_argument("--increment-ms", type=int, default=cfg["incrementMs"])
    parser.add_argument("--max-attempts", type=int, default=cfg["maxAttempts"])
    parser.add_argument("--attempt-timeout-seconds", type=int, default=cfg["attemptTimeoutSeconds"])
    parser.add_argument("--max-plies", type=int, default=cfg["maxPlies"])
    parser.add_argument("--slug")
    parser.add_argument("--live-pgn", type=Path)
    parser.add_argument("--preflight-only", action="store_true", help="Probe one move per player and exit.")
    parser.add_argument("--no-preflight", action="store_true")
    args = parser.parse_args(argv)

    p1 = parse_player_spec(args.player1, args.player1_name) if args.player1 else dict(cfg["player1"])
    p2 = parse_player_spec(args.player2, args.player2_name) if args.player2 else dict(cfg["player2"])
    if args.player1_name:
        p1["name"] = args.player1_name
    if args.player2_name:
        p2["name"] = args.player2_name
    if p1["name"] == p2["name"]:
        p2["name"] += " (2)"
    cfg.update({
        "games": max(1, args.games), "playAll": args.play_all, "timeControlMs": args.time_control_ms,
        "incrementMs": args.increment_ms, "maxAttempts": max(1, min(9, args.max_attempts)),
        "attemptTimeoutSeconds": max(10, args.attempt_timeout_seconds), "maxPlies": max(2, args.max_plies),
    })

    def log(message: str) -> None:
        print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)

    if not args.no_preflight or args.preflight_only:
        log(f"preflight: one move each from {p1['name']} and {p2['name']}")
        failures = preflight([p1, p2], cfg)
        for failure in failures:
            log(f"PREFLIGHT FAILED: {failure}")
        if failures:
            return 2
        log("preflight ok")
        if args.preflight_only:
            return 0

    stamp = time.strftime("%Y%m%d-%H%M%S")
    slug = args.slug or f"llm-match-{slug_part(p1['name'])}-vs-{slug_part(p2['name'])}-{stamp}"
    live_pgn = args.live_pgn or LIVE_DIR / f"{slug}-live.pgn"
    series = {"id": slug, "games": cfg["games"], "player1": p1["name"], "player2": p2["name"],
              "score": {"player1": 0.0, "player2": 0.0}, "current_game": 1, "finished": False, "winner": None}
    writer = SeriesWriter(live_pgn, series)
    stop = threading.Event()

    def beat() -> None:
        while not stop.wait(STATUS_HEARTBEAT_SECONDS):
            try:
                writer.heartbeat()
            except OSError:
                pass

    threading.Thread(target=beat, daemon=True).start()
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    archive_pgn = ARCHIVE_DIR / f"{slug}.pgn"
    archive_json = ARCHIVE_DIR / f"{slug}.json"
    log(f"series {slug}: {p1['name']} vs {p2['name']}, best of {cfg['games']}")
    log(f"live PGN: {live_pgn}")
    engines = {1: LlmEngine(p1, cfg), 2: LlmEngine(p2, cfg)}
    scores = [0.0, 0.0]
    results = []
    try:
        for number in range(1, cfg["games"] + 1):
            series["current_game"] = number
            white_key, black_key = (1, 2) if number % 2 == 1 else (2, 1)
            summary = play_game(number, engines[white_key], engines[black_key], cfg, series, writer, log)
            points = {"1-0": (1.0, 0.0), "0-1": (0.0, 1.0), "1/2-1/2": (0.5, 0.5)}.get(summary["result"], (0.0, 0.0))
            scores[white_key - 1] += points[0]
            scores[black_key - 1] += points[1]
            series["score"] = {"player1": scores[0], "player2": scores[1]}
            results.append(summary)
            for key in (1, 2):  # a crashed engine process is replaced for the next game
                if engines[key].proc.poll() is not None:
                    engines[key] = LlmEngine(engines[key].player, cfg)
            decided = series_decided(scores, number, cfg["games"])
            if decided and not cfg["playAll"] and number < cfg["games"]:
                log(f"series decided after game {number}; remaining games not needed")
                break
            writer.write()
            write_text_retry(archive_pgn, "\n\n".join(str(game) for game in writer.games) + "\n\n")
        series["finished"] = True
        if scores[0] != scores[1]:
            series["winner"] = p1["name"] if scores[0] > scores[1] else p2["name"]
        writer.write()
        write_text_retry(archive_pgn, "\n\n".join(str(game) for game in writer.games) + "\n\n")
        archive_json.write_text(json.dumps({"series": series, "config": cfg, "players": [p1, p2], "games": results,
                                            "live_pgn": str(live_pgn), "archive_pgn": str(archive_pgn)}, indent=2),
                                encoding="utf-8")
        log(f"series finished: {p1['name']} {scores[0]:g} - {scores[1]:g} {p2['name']}; winner: {series['winner'] or 'tied'}")
        log(f"archive: {archive_pgn}")
        return 0
    finally:
        stop.set()
        for engine in engines.values():
            engine.close()


if __name__ == "__main__":
    sys.exit(main())
