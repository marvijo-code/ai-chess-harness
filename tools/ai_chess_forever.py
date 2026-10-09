"""Non-stop AI chess: one round-robin + knockout tournament after another, with agent memory and a
Stockfish depth ladder (configs/ai-chess-vps.json, runs on the VPS).

    python tools/ai_chess_forever.py [--config configs/ai-chess-vps.json]          # forever
    python tools/ai_chess_forever.py --preflight                                   # one real move per AI player
    python tools/ai_chess_forever.py --segment "Sonnet 5.5,GPT-6.1 Sol" --plies 10 --memory-repo /tmp/x --no-push

* Tournament #N: fresh Elo (startElo) for everyone, new slug `aichess-NNNN-<stamp>`. When it finishes, the
  next one starts after `forever.pauseSeconds`. out/live/current.json {"state_path", "id", "number"} always
  names the live state; after a restart the runner resumes the tournament it names (unfinished games restart).
* Memory (tools/ai_chess_memory.py): every AI player's MEMORY.md is read at the start of each game, frozen,
  and sent with every move; after every game one reflection call per AI player (same subscription route)
  returns file edits that are validated and applied; the memory repo is committed and pushed in the background.
* Stockfish ladder: the ladder player starts at `forever.ladderStartDepth`; each AI win against it raises the
  depth by 1 for every later game (ladder.json in the memory repo, kept across tournaments and restarts).
* Usage limits never end the loop: the engines wait and ask the same move again (limitWait).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

import chess

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engines" / "llm-chess-engine"))
import play_llm_swiss as swiss  # noqa: E402
from ai_chess_memory import (GitPusher, MemoryRepo, MEMORY_MAX_BYTES, MEMORY_TARGET_BYTES, MAX_NOTE_FILES,  # noqa: E402
                             NOTE_MAX_BYTES,
                             ensure_repo, game_markdown, ladder_step, parse_reflection, player_folders, slugify,
                             text_sha, validate_edits)
from play_llm_series import LlmEngine, iso_now, parse_info, parse_note, parse_usage, write_text_retry  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "configs" / "ai-chess-vps.json"
POINTER_NAME = "current.json"
FOREVER_DEFAULTS = {"pauseSeconds": 120, "memoryRepo": str(ROOT / "out" / "ai-chess-agent-memory"), "memoryRemote": None,
                    "push": True, "ladderPlayer": "Stockfish 19", "ladderStartDepth": 4, "reflection": True,
                    "reflectionTimeoutSeconds": 600, "marksWaitSeconds": 60, "rosterCheck": True,
                    "rosterCheckTimeoutSeconds": 300, "minPlayers": 3, "rosterRetrySeconds": 900}
PAUSED_RETRY_SECONDS = 300
MAX_REFLECTION_RETRIES = 2
OPENCODE_GO_USAGE_URL = "https://opencode.ai/zen/go/v1/usage"

REFLECTION_SYSTEM = (
    "You are an AI chess player in a non-stop tournament against other AI players and a Stockfish ladder. "
    "After every game you maintain your own memory, like a careful professional keeps a notebook. "
    "You answer with only one JSON object and nothing else."
)


def log(message: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def load_forever_config(path: Path) -> dict:
    cfg = swiss.load_config(path)
    cfg["players"] = [p for p in cfg["players"] if p.get("enabled", True) is not False]
    cfg["forever"] = {**FOREVER_DEFAULTS, **(cfg.get("forever") or {})}
    cfg.setdefault("conversation", True)
    cfg.setdefault("limitWait", True)
    if cfg.get("format") == "round-robin+knockout":
        cfg["rounds"] = swiss.max_rounds(len(cfg["players"]))
    return cfg


# ---------------------------------------------------------------- pointer


def write_pointer(live_dir: Path, state_path: Path, state: dict) -> Path:
    pointer = live_dir / POINTER_NAME
    payload = {"state_path": str(Path(state_path).resolve()), "id": state["id"], "number": state.get("number"),
               "title": state.get("title"), "updated_epoch_ms": int(time.time() * 1000)}
    live_dir.mkdir(parents=True, exist_ok=True)
    write_text_retry(pointer, json.dumps(payload, indent=1))
    return pointer


def read_pointer(pointer: Path) -> dict | None:
    try:
        data = json.loads(Path(pointer).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("state_path"):
        return None
    path = Path(data["state_path"])
    if not path.is_absolute():
        path = Path(pointer).parent / path
    data["state_path"] = str(path)
    return data


# ---------------------------------------------------------------- prompts


def game_header(state: dict, game: dict, side: str, cfg: dict, ladder: dict | None) -> str:
    opponent = game["black"] if side == "white" else game["white"]
    label = game.get("label") or f"Round {game.get('round')}, board {game.get('board')}"
    vs = opponent
    if ladder and opponent == ladder.get("player") and game.get("stockfish_depth"):
        vs = f"{opponent} (the Stockfish 19 chess engine at search depth {game['stockfish_depth']})"
    start_ms = cfg["timeControlMs"]
    if game.get("armageddon"):
        start_ms = cfg["armageddonWhiteMs"] if side == "white" else cfg["armageddonBlackMs"]
    lines = [f"Tournament #{state.get('number')} ({state['id']}), {label}.",
             f"You play {side.capitalize()} against {vs}.",
             f"Your clock: {start_ms // 60000} min + {cfg['incrementMs'] // 1000} s per move. "
             f"Up to {cfg['maxAttempts']} tries per move."]
    if game.get("armageddon"):
        lines.append("Armageddon decider: a draw counts as a win for Black.")
    return "\n".join(lines)


def reflection_prompt(player: str, state: dict, game: dict, memory: str, notes: list[tuple[str, str]],
                      marks: dict[str, str], folder: str | None = None,
                      others: list[tuple[str, str]] | None = None) -> str:
    side = "white" if game["white"] == player else "black"
    opponent = game["black"] if side == "white" else game["white"]
    score = {"1-0": "won" if side == "white" else "lost", "0-1": "won" if side == "black" else "lost",
             "1/2-1/2": "drew"}.get(game.get("result"), "did not finish")
    own_notes = [n for n in game.get("notes") or [] if n.get("player") == player]
    own_marks = []
    for move in game.get("moves") or []:
        mark = marks.get(str(move.get("ply")))
        if mark and move.get("side") == side:
            own_marks.append(f"ply {move['ply']} {move['san']}{mark}")
    opp_marks = [f"ply {m['ply']} {m['san']}{marks[str(m['ply'])]}" for m in game.get("moves") or []
                 if marks.get(str(m.get("ply"))) and m.get("side") != side]
    parts = [
        f"You are {player}. Your game just ended: you {score} as {side.capitalize()} against {opponent} "
        f"({game.get('result')}, {game.get('termination')}). Tournament #{state.get('number')} ({state['id']}), "
        f"{game.get('label') or 'round ' + str(game.get('round'))}.",
        "",
        "Update your memory so you play better in later games. "
        f"Your folder is agents/{folder or slugify(player)}/ in the public memory repo. You can READ every player's "
        "memory (shown below); you can WRITE only your own files. Your memory files:",
        f"- MEMORY.md: an index plus your key lessons. Read at the start of every game and shown with every move. "
        f"Hard cap {MEMORY_MAX_BYTES} bytes, aim for {MEMORY_TARGET_BYTES} or less (now {len(memory.encode('utf-8'))} "
        "bytes; a file over the cap is rejected whole, so to add a lesson, drop or merge an old one). Keep it concise: merge, sharpen or drop old lessons instead of only "
        "appending. List your note files in it, one line each.",
        f"- notes/<topic>.md (lowercase letters, digits and hyphens): optional topic notes, at most {NOTE_MAX_BYTES} "
        f"bytes each and at most {MAX_NOTE_FILES} files. They are not shown during games; MEMORY.md is.",
        "Write only what helps your chess: openings that worked or failed, recurring tactical misses, time use, "
        "how specific opponents play. No praise, no filler. You may adopt a lesson from another player's memory "
        "when it is good chess; write it in your own words in your own files.",
        "",
        "Reply with ONLY this JSON object:",
        '{"summary": "<one or two sentences: what you learned from this game>", "edits": ['
        '{"path": "MEMORY.md", "content": "<the full new MEMORY.md>"}, '
        '{"path": "notes/<topic>.md", "content": "<the full file>"}, {"path": "notes/<topic>.md", "delete": true}]}',
        "An edit replaces the whole file. Leave out files you do not change. An empty edits list is allowed.",
        "",
        "YOUR CURRENT MEMORY.md",
        memory.strip() or "(empty: this was one of your first games)",
        "",
        "YOUR NOTE FILES",
    ]
    parts += [f"--- {rel} ---\n{text.strip()}" for rel, text in notes] or ["(none)"]
    parts += ["", "OTHER PLAYERS' MEMORY (read-only for you: agents/<folder>/MEMORY.md of every other AI player)"]
    parts += [f"--- agents/{name}/MEMORY.md ---\n{text.strip()}" for name, text in others or []] or ["(none yet)"]
    parts += ["", "YOUR NOTES DURING THIS GAME"]
    parts += [f"after ply {n['ply']} ({n.get('san', '')}): {n['note']}" for n in own_notes] or ["(none)"]
    parts += ["", "STOCKFISH MOVE MARKS (?? blunder, ? mistake, ?! inaccuracy, ! only good move; viewer analysis "
              "after the game, never shown during play)"]
    if marks:
        parts.append("Your moves: " + (", ".join(own_marks) or "no marks"))
        parts.append("Opponent moves: " + (", ".join(opp_marks) or "no marks"))
    else:
        parts.append("(not available for this game)")
    parts += ["", "GAME PGN", (game.get("pgn") or "").strip()]
    return "\n".join(parts)


def stockfish_marks(live_dir: Path, state: dict, game: dict) -> dict[str, str]:
    """The viewer's ?? ? ?! ! marks for this game from <slug>-annotations.json (positions it analysed)."""
    try:
        from llm_tournament_viewer import annotate_plies  # viewer-only module, imported lazily
    except Exception:
        return {}
    try:
        saved = json.loads((live_dir / f"{state['id']}-annotations.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    known = saved.get("positions") or {}
    board = chess.Board()
    fens = [board.fen()]
    ucis = []
    for move in game.get("moves") or []:
        board.push_uci(move["uci"])
        ucis.append(move["uci"])
        fens.append(board.fen())
    return annotate_plies(ucis, [known.get(f) for f in fens])


def marks_coverage(live_dir: Path, state: dict, game: dict) -> float:
    try:
        saved = json.loads((live_dir / f"{state['id']}-annotations.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0.0
    known = saved.get("positions") or {}
    board = chess.Board()
    fens = [board.fen()]
    for move in game.get("moves") or []:
        board.push_uci(move["uci"])
        fens.append(board.fen())
    return sum(1 for f in fens if f in known) / max(1, len(fens))


# ---------------------------------------------------------------- reflection


def make_client(player: dict, cfg: dict):
    import subscription_providers as sp

    client = sp.SubscriptionChessClient(player["provider"], lambda m: log(f"reflection {player['name']}: {m}"))
    client.model = player["model"]
    client.effort = player.get("effort") or "high"
    client.limit_wait = bool(cfg.get("limitWait", True))
    return client


def reflect(player: dict, state: dict, game: dict, repo: MemoryRepo, cfg: dict, marks: dict[str, str],
            client_factory=make_client) -> dict:
    """One reflection call for one player; validated edits are applied. Never raises."""
    name = player["name"]
    out = {"player": name, "summary": "", "applied": [], "rejected": [], "usage": None, "error": "", "retries": 0,
           "attempts": []}
    try:
        others = repo.other_memories(name)
        out["read_others"] = [folder for folder, _ in others]
        prompt = reflection_prompt(name, state, game, repo.memory_text(name), repo.notes_text(name), marks,
                                   repo.folder_of(name), others)
        client = client_factory(player, cfg)
        started = time.monotonic()
        timeout = int(cfg["forever"]["reflectionTimeoutSeconds"])
        attempt_prompt, accepted, rejected = prompt, [], []
        for attempt in range(MAX_REFLECTION_RETRIES + 1):
            text = client.ask_text(REFLECTION_SYSTEM, attempt_prompt, timeout)
            if attempt == 0:
                out["usage"] = (client.last_report or {}).get("usage")
            data, why = parse_reflection(text)
            sizes = {}
            if data is not None:
                if not out["summary"]:
                    out["summary"] = " ".join(str(data.get("summary") or "").split())[:500]
                done = {e["path"] for e in accepted}
                edits = data.get("edits") or []
                edits = [e for e in edits if not (isinstance(e, dict) and e.get("path") in done)] if isinstance(edits, list) else edits
                sizes = {e["path"]: len(str(e.get("content") or "").encode("utf-8")) for e in edits
                         if isinstance(e, dict) and isinstance(e.get("path"), str) and "content" in e}
                notes_now = set(repo.note_paths(name)) | {e["path"] for e in accepted if not e.get("delete")}
                notes_now -= {e["path"] for e in accepted if e.get("delete")}
                more, rejected = validate_edits(edits, sorted(notes_now))
                accepted += more
            out["attempts"].append({"bytes": sizes, "rejected": len(rejected), "error": "" if data is not None else why})
            if data is not None and not rejected:
                break
            if data is None and attempt == MAX_REFLECTION_RETRIES:
                if not accepted:
                    out["error"] = why
                break
            if attempt == MAX_REFLECTION_RETRIES:
                break
            # Retry: a lesson lost to a few bytes over a cap or to broken JSON is still a lost lesson.
            out["retries"] = attempt + 1
            problems = [why] if data is None else rejected
            attempt_prompt = (prompt + "\n\nYOUR REPLY WAS NOT FULLY ACCEPTED (try " + str(attempt + 1) + ")\n"
                              + "\n".join(problems) + "\n"
                              "Reply again with ONLY the JSON object. Resend only the files that were rejected (or all "
                              "of them if the JSON did not parse). Count bytes: cut at least the amount stated, by "
                              "merging or dropping your weakest lessons; do not only shorten words.")
        out["seconds"] = round(time.monotonic() - started, 1)
        out["rejected"] = rejected
        out["applied"] = repo.apply_edits(name, accepted)
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"[:300]
    out["memory_after"] = text_sha(repo.memory_text(name))
    return out


# ---------------------------------------------------------------- hooks


class ForeverHooks(swiss.GameHooks):
    def __init__(self, cfg: dict, repo: MemoryRepo, pusher: GitPusher | None, live_dir: Path,
                 client_factory=make_client) -> None:
        self.cfg = cfg
        self.repo = repo
        self.pusher = pusher
        self.live_dir = live_dir
        self.client_factory = client_factory
        fv = cfg["forever"]
        self.ladder_player = fv["ladderPlayer"]
        self.ladder = repo.load_ladder(self.ladder_player, int(fv["ladderStartDepth"]))
        self.lock = threading.RLock()
        self.context_dir = live_dir / "contexts"
        # sha of each player's MEMORY.md right after its last reflection: its next game must start from it.
        learning = repo.read_json("tournaments/learning.json") or {}
        self.last_written: dict[str, str] = dict(learning.get("last_written") or {})

    def ladder_view(self) -> dict:
        with self.lock:
            return {"player": self.ladder["player"], "depth": self.ladder["depth"],
                    "start_depth": self.ladder.get("start_depth"), "steps": len(self.ladder.get("steps") or []),
                    "last_step": (self.ladder.get("steps") or [None])[-1]}

    def before_game(self, ts, game_id: str, white: LlmEngine, black: LlmEngine) -> None:
        state = ts.state
        game = state["games"][game_id]
        with ts.lock:
            for engine in (white, black):
                if engine.is_uci and engine.name == self.ladder_player:
                    with self.lock:
                        depth = int(self.ladder["depth"])
                    engine.player["depth"] = depth
                    game["stockfish_depth"] = depth
            state["ladder"] = self.ladder_view()
        self.context_dir.mkdir(parents=True, exist_ok=True)
        for side, engine in (("white", white), ("black", black)):
            if engine.is_uci:
                continue
            memory = self.repo.memory_text(engine.name)
            context = {"memory": memory, "header": game_header(state, game, side, self.cfg, state.get("ladder"))}
            path = self.context_dir / f"{state['id']}-{game_id}-{side}.json"
            write_text_retry(path, json.dumps(context, indent=1))
            engine.send(f"setoption name GameContextFile value {path}")
            with ts.lock:
                game.setdefault("memory_bytes", {})[side] = len(memory.encode("utf-8"))
                game.setdefault("memory_read", {})[side] = {
                    "player": engine.name, "folder": self.repo.folder_of(engine.name),
                    "bytes": len(memory.encode("utf-8")), "sha": text_sha(memory),
                    "expected": self.last_written.get(engine.name)}
        ts.save()

    def after_game(self, ts, game_id: str) -> None:
        state = ts.state
        game = state["games"][game_id]
        number = state.get("number")
        messages = []
        with self.lock:
            step = ladder_step(self.ladder, game, self.ladder_player, state["id"], number, iso_now())
            if step:
                self.repo.save_ladder(self.ladder)
        if step:
            log(f"LADDER: {step['winner']} beat {self.ladder_player} at depth {step['from']} in {game_id}; "
                f"depth is now {step['to']} for every later game")
            messages.append(f"Ladder: {self.ladder_player} depth {step['from']} to {step['to']} after {step['winner']} won {game_id}")
        with ts.lock:
            state["ladder"] = self.ladder_view()
        ts.save()
        players = {p["name"]: p for p in state["players"]}
        ai = [name for name in (game["white"], game["black"]) if players.get(name, {}).get("provider") != "uci"]
        reflections: dict[str, dict] = {}
        if ai and self.cfg["forever"].get("reflection", True):
            marks = self.wait_for_marks(state, game)
            threads = []
            for name in ai:
                def run(name=name):
                    reflections[name] = reflect(players[name], state, game, self.repo, self.cfg, marks, self.client_factory)
                thread = threading.Thread(target=run, daemon=True)
                thread.start()
                threads.append(thread)
            for thread in threads:
                thread.join()
            for name, result in reflections.items():
                self.last_written[name] = result["memory_after"]
                log(f"{game_id} reflection {name}: applied {result['applied'] or 'nothing'}"
                    + (f"; rejected {result['rejected']}" if result["rejected"] else "")
                    + (f"; error {result['error']}" if result["error"] else ""))
            with ts.lock:
                game["reflection"] = {name: {k: v for k, v in r.items() if k != "player"} for name, r in reflections.items()}
            ts.save()
        for name in ai:
            side = "white" if game["white"] == name else "black"
            usage = self.game_usage(game, side)
            text = game_markdown(state, game, name, reflections.get(name), usage)
            self.repo.write_file(f"agents/{self.repo.folder_of(name)}/games/{state['id']}-{game_id}.md", text)
        self.repo.record_tournament(state)
        self.repo.record_learning(state, self.last_written)
        self.repo.record_cache_stats(state)
        result_line = f"Tournament #{number} {game_id}: {game['white']} {game['result']} {game['black']}"
        if self.pusher is not None:
            self.pusher.request("; ".join([result_line] + messages))

    @staticmethod
    def game_usage(game: dict, side: str) -> dict:
        total = {"input": 0, "cached": 0, "calls": 0}
        for move in game.get("moves") or []:
            if move.get("side") == side and move.get("usage"):
                for key in total:
                    total[key] += int(move["usage"].get(key) or 0)
        return total

    def wait_for_marks(self, state: dict, game: dict) -> dict[str, str]:
        """The viewer analyses moves in the background: give it a short while to catch up."""
        deadline = time.monotonic() + float(self.cfg["forever"].get("marksWaitSeconds") or 0)
        while time.monotonic() < deadline and marks_coverage(self.live_dir, state, game) < 0.9:
            time.sleep(5)
        return stockfish_marks(self.live_dir, state, game)


# ---------------------------------------------------------------- the loop


# ---------------------------------------------------------------- roster check (benching)


def bench_kind(reason: str) -> str:
    import subscription_providers as sp

    lowered = (reason or "").lower()
    if any(m in lowered for m in ("insufficient balance", "1113", "unpurchased", "payment required", "credit balance",
                                  "insufficient_quota", "http 402")):
        return "no subscription funds or plan"
    if any(m in lowered for m in ("not logged in", "/login", "unauthorized", "invalid api key", "http 401", "is not set")):
        return "login or key problem"
    if sp.limit_error(reason) or sp.provider_unavailable(reason):
        return "subscription limit"
    return "preflight failed"


def opencode_go_reset(timeout: float = 15) -> str | None:
    """The resetsAt of the OpenCode Go window that is rate limited (rolling, weekly or monthly), or None."""
    import urllib.request

    import subscription_providers as sp

    key = os.environ.get("OPENCODE_GO_API_KEY") or sp._user_env("OPENCODE_GO_API_KEY")
    if not key:
        return None
    request = urllib.request.Request(OPENCODE_GO_USAGE_URL, headers={"Authorization": f"Bearer {key}", "User-Agent": sp.BROWSER_UA})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            usage = (json.loads(response.read().decode("utf-8")) or {}).get("usage") or {}
    except Exception:
        return None
    limited = [w.get("resetsAt") for w in usage.values() if isinstance(w, dict) and w.get("status") != "ok" and w.get("resetsAt")]
    return max(limited) if limited else None


def reset_time(player: dict, reason: str) -> str | None:
    """ISO UTC reset time of a limit when the error or the provider states it."""
    import datetime as dt

    import subscription_providers as sp

    if player.get("provider") == "opencode-go":
        found = opencode_go_reset()
        if found:
            return found
    epoch = sp.limit_reset_epoch(reason)
    if epoch:
        return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    return None


def probe_player(player: dict, cfg: dict, live_dir: Path, timeout: float) -> dict:
    """One real move (1.e4 ...?) on the player's route. A usage-limit wait ends the probe at once: the player is
    benched, not waited for. Returns {"name", "ok", "move", "seconds", "reason", "kind", "resets_at"}."""
    started = time.monotonic()
    row = {"name": player["name"], "route": f"{player.get('provider')} {player.get('model')}", "ok": False}
    engine = None
    try:
        engine = LlmEngine(player, cfg)
        engine.new_game()
        if not engine.is_uci:
            path = live_dir / "contexts" / f"rostercheck-{slugify(player['name'])}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            write_text_retry(path, json.dumps({"memory": "", "header": "Roster check before the tournament. You play Black."}))
            engine.send(f"setoption name GameContextFile value {path}")
        engine.send("position startpos moves e2e4")
        engine.send(engine.go_command(cfg["timeControlMs"], cfg["timeControlMs"], cfg["incrementMs"]))
        deadline = time.monotonic() + timeout
        seen: list[str] = []
        outcome = None
        with engine.cond:
            while outcome is None:
                while engine.lines and outcome is None:
                    line = engine.lines.pop(0)
                    seen.append(line)
                    if line == "__EOF__":
                        outcome = ("error", "engine exited")
                    elif line.startswith("info string limitwait "):
                        parts = line.split(maxsplit=4)
                        outcome = ("limit", parts[4] if len(parts) > 4 else "usage limit")
                    elif line.startswith("bestmove"):
                        outcome = ("move", line.split()[1])
                if outcome is None:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        outcome = ("error", f"no answer within {timeout:.0f}s")
                    else:
                        engine.cond.wait(min(left, 1.0))
        row["seconds"] = round(time.monotonic() - started, 1)
        board = chess.Board()
        board.push_uci("e2e4")
        if outcome[0] == "move" and outcome[1] != "0000" and chess.Move.from_uci(outcome[1]) in board.legal_moves:
            row.update(ok=True, move="1..." + board.san(chess.Move.from_uci(outcome[1])), usage=parse_usage(seen))
            return row
        reason = outcome[1] if outcome[0] != "move" else (parse_info(seen)[0] or "no legal move")
        row["reason"] = " ".join(str(reason).split())[:240]
    except Exception as exc:
        row["reason"] = f"{type(exc).__name__}: {exc}"[:240]
    finally:
        if engine is not None:
            try:
                swiss.kill_engine_tree(engine) if not row.get("ok") else engine.close()
            except Exception:
                pass
    row["kind"] = bench_kind(row.get("reason", ""))
    row["resets_at"] = reset_time(player, row.get("reason", "")) if row["kind"] == "subscription limit" else None
    return row


def check_roster(cfg: dict, live_dir: Path, timeout: float | None = None, prober=None) -> tuple[list[dict], list[dict], list[dict]]:
    """(playing players, benched entries, probe rows). Every enabled player gets one real move at the same time."""
    from concurrent.futures import ThreadPoolExecutor

    timeout = float(timeout or cfg["forever"].get("rosterCheckTimeoutSeconds") or 300)
    prober = prober or probe_player
    players = list(cfg["players"])
    with ThreadPoolExecutor(max_workers=max(1, len(players))) as pool:
        rows = list(pool.map(lambda pl: prober(pl, cfg, live_dir, timeout), players))
    playing, benched = [], []
    for player, row in zip(players, rows):
        if row.get("ok"):
            playing.append(player)
        else:
            benched.append({"name": player["name"], "route": row.get("route"), "kind": row.get("kind") or "preflight failed",
                            "reason": row.get("reason", ""), "resets_at": row.get("resets_at"), "checked_at": iso_now()})
    return playing, benched, rows


def bench_label(entry: dict) -> str:
    text = f"benched: {entry.get('kind')}"
    if entry.get("resets_at"):
        text += f", resets {entry['resets_at']}"
    return text


def tournament_slug(number: int) -> str:
    return f"aichess-{number:04d}-{time.strftime('%Y%m%d-%H%M%S')}"


def run_forever(cfg: dict, live_dir: Path, repo: MemoryRepo, pusher: GitPusher | None, hooks: ForeverHooks,
                max_tournaments: int | None = None, sleep=time.sleep) -> int:
    pointer_path = live_dir / POINTER_NAME
    live_dir.mkdir(parents=True, exist_ok=True)
    played = 0
    while max_tournaments is None or played < max_tournaments:
        pointer = read_pointer(pointer_path)
        state = None
        if pointer and Path(pointer["state_path"]).is_file():
            try:
                saved = json.loads(Path(pointer["state_path"]).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                saved = None
            if saved and not saved.get("finished"):
                state = swiss.prepare_resume(saved)
                status_path = Path(pointer["state_path"])
                log(f"resuming Tournament #{state.get('number')} ({state['id']})")
        if state is None:
            roster, benched = list(cfg["players"]), []
            if cfg["forever"].get("rosterCheck", True):
                log("roster check: one real move from every player")
                roster, benched, rows = check_roster(cfg, live_dir)
                for row in rows:
                    log("ROSTER " + json.dumps({k: row.get(k) for k in ("name", "ok", "move", "seconds", "kind", "resets_at", "reason")}))
            need = int(cfg["forever"].get("minPlayers") or 3)
            if len(roster) < need:
                wait = float(cfg["forever"].get("rosterRetrySeconds") or 900)
                log(f"only {len(roster)} players can play (need {need}); checking again in {wait:.0f}s")
                sleep(wait)
                continue
            number = repo.next_tournament_number()
            if pointer and pointer.get("number"):
                number = max(number, int(pointer["number"]) + 1)
            slug = tournament_slug(number)
            tcfg = dict(cfg, players=roster)
            if tcfg.get("format") == "round-robin+knockout":
                tcfg["rounds"] = swiss.max_rounds(len(roster))
            state = swiss.new_state(tcfg, slug, title=f"{cfg['title']} #{number}")
            state["number"] = number
            state["benched"] = benched
            state["format"] = {"type": tcfg.get("format"), "rr_rounds": tcfg["rounds"],
                               "ko_size": min(int(tcfg.get("knockoutSize", 4)), len(roster))}
            status_path = live_dir / f"{slug}-tournament.json"
            log(f"starting Tournament #{number} ({slug}): " + ", ".join(p["name"] for p in roster)
                + ("; " + "; ".join(f"{b['name']} {bench_label(b)}" for b in benched) if benched else ""))
        state["ladder"] = hooks.ladder_view()
        swiss.TournamentState(state, status_path).save()
        write_pointer(live_dir, status_path, state)
        repo.record_tournament(state)
        if pusher is not None:
            pusher.request(f"Tournament #{state.get('number')} started ({state['id']})")
        code = swiss.run_tournament(state, state["config"], status_path, log)
        if code != 0:
            log(f"Tournament #{state.get('number')} paused ({state.get('paused')}); trying again in {PAUSED_RETRY_SECONDS}s")
            sleep(PAUSED_RETRY_SECONDS)
            continue
        played += 1
        repo.record_tournament(state)
        repo.record_cache_stats(state)
        if pusher is not None:
            champion = (state.get("knockout") or {}).get("champion") or state.get("winner")
            pusher.request(f"Tournament #{state.get('number')} finished: champion {champion}")
        if max_tournaments is not None and played >= max_tournaments:
            break
        pause = float(cfg["forever"]["pauseSeconds"])
        log(f"next tournament in {pause:.0f}s")
        sleep(pause)
    return 0


# ---------------------------------------------------------------- preflight and measured segment


def preflight_moves(cfg: dict, repo: MemoryRepo, live_dir: Path) -> list[dict]:
    """One real move from every AI player through the real route, with the forever prompt (memory + header)."""
    from concurrent.futures import ThreadPoolExecutor

    ai = [p for p in cfg["players"] if p.get("provider") != "uci"]
    live_dir.mkdir(parents=True, exist_ok=True)

    def probe(player: dict) -> dict:
        engine = None
        started = time.monotonic()
        row = {"player": player["name"], "route": f"{player['provider']} {player['model']}"}
        try:
            engine = LlmEngine(player, cfg)
            engine.new_game()
            path = live_dir / f"preflight-{slugify(player['name'])}.json"
            write_text_retry(path, json.dumps({"memory": repo.memory_text(player["name"]),
                                               "header": "Preflight game. You play Black against a test opponent."}))
            engine.send(f"setoption name GameContextFile value {path}")
            engine.send("position startpos moves e2e4")
            engine.send(engine.go_command(cfg["timeControlMs"], cfg["timeControlMs"], cfg["incrementMs"]))
            lines = engine.wait_for("bestmove", engine.move_budget_seconds() + 3600)
            uci = lines[-1].split()[1]
            board = chess.Board()
            board.push_uci("e2e4")
            row["seconds"] = round(time.monotonic() - started, 1)
            row["usage"] = parse_usage(lines)
            row["note"] = parse_note(lines)
            if uci == "0000" or chess.Move.from_uci(uci) not in board.legal_moves:
                row["ok"] = False
                row["error"] = parse_info(lines)[0]
            else:
                row["ok"] = True
                row["move"] = "1..." + board.san(chess.Move.from_uci(uci))
        except Exception as exc:
            row.update(ok=False, error=f"{type(exc).__name__}: {exc}")
        finally:
            if engine:
                engine.close()
        return row

    with ThreadPoolExecutor(max_workers=len(ai)) as pool:
        return list(pool.map(probe, ai))


def run_segment(cfg: dict, names: list[str], plies: int, repo: MemoryRepo, pusher: GitPusher | None,
                live_dir: Path, hooks_cls=ForeverHooks) -> dict:
    """A short real game between two players (adjudicated at `plies`), with the forever hooks: memory in the
    prompt, notes, usage per move, and the post-game reflection into `repo`."""
    players = {p["name"]: p for p in cfg["players"]}
    seg_cfg = dict(cfg, maxPlies=plies)
    state = swiss.new_state(dict(seg_cfg, players=[players[n] for n in names]), f"segment-{time.strftime('%Y%m%d-%H%M%S')}",
                            title="Preflight segment")
    state["number"] = 0
    game_id = "r1b1"
    state["rounds"] = [{"round": 1, "bye": None, "pairings": [{"board": 1, "white": names[0], "black": names[1], "game_id": game_id}],
                        "status": "live"}]
    state["games"][game_id] = {"id": game_id, "round": 1, "board": 1, "white": names[0], "black": names[1],
                               "status": "pending", "result": "*", "moves": []}
    hooks = hooks_cls(seg_cfg, repo, pusher, live_dir)
    swiss.HOOKS = hooks
    status_path = live_dir / f"{state['id']}-tournament.json"
    ts = swiss.TournamentState(state, status_path)
    white, black = LlmEngine(players[names[0]], seg_cfg), LlmEngine(players[names[1]], seg_cfg)
    try:
        swiss.play_game(game_id, white, black, seg_cfg, ts, live_dir / f"{state['id']}-{game_id}-live.pgn", log,
                        lambda engine: None)
    finally:
        white.close()
        black.close()
        swiss.HOOKS = None
    ts.save()
    return state


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--live-dir", type=Path, default=swiss.LIVE_DIR)
    parser.add_argument("--memory-repo", type=Path, help="default: forever.memoryRepo from the config")
    parser.add_argument("--no-push", action="store_true", help="commit to the memory repo but never push")
    parser.add_argument("--max-tournaments", type=int, help="stop after this many finished tournaments (tests)")
    parser.add_argument("--preflight", action="store_true", help="one real move per AI player, then exit")
    parser.add_argument("--segment", help="'White name,Black name': a short real game, then exit")
    parser.add_argument("--plies", type=int, default=10)
    # Test overrides (a short scratch tournament): never needed by the service.
    parser.add_argument("--max-plies", type=int, help="override maxPlies (games are adjudicated drawn at this ply)")
    parser.add_argument("--pause-seconds", type=float, help="override forever.pauseSeconds")
    parser.add_argument("--no-reflection", action="store_true", help="skip the post-game memory reflection calls")
    args = parser.parse_args(argv)

    cfg = load_forever_config(args.config)
    fv = cfg["forever"]
    if args.max_plies:
        cfg["maxPlies"] = args.max_plies
    if args.pause_seconds is not None:
        fv["pauseSeconds"] = args.pause_seconds
    if args.no_reflection:
        fv["reflection"] = False
    live_dir = args.live_dir.resolve()
    repo_root = (args.memory_repo or Path(fv["memoryRepo"])).resolve()
    ensure_repo(repo_root, None if args.memory_repo else fv.get("memoryRemote"), log)
    repo = MemoryRepo(repo_root, log, player_folders(cfg["players"]))
    moved = repo.migrate_folders()
    push = bool(fv.get("push", True)) and not args.no_push
    pusher = GitPusher(repo, push=push, log=log)
    if moved:
        pusher.request("Memory folders by model family: " + "; ".join(moved))
    log(f"memory repo {repo_root} (push {'on' if push else 'off'}), live dir {live_dir}")

    if args.preflight:
        # The same check every new tournament starts with: one real move per enabled player, limits bench.
        playing, benched, rows = check_roster(cfg, live_dir)
        for row in rows:
            log("PREFLIGHT " + json.dumps(row))
        log(f"would play: {[p['name'] for p in playing]}; benched: {[(b['name'], bench_label(b)) for b in benched]}")
        return 0 if len(playing) >= int(fv.get("minPlayers") or 3) else 2
    if args.segment:
        names = [n.strip() for n in args.segment.split(",")]
        state = run_segment(cfg, names, args.plies, repo, pusher, live_dir)
        game = state["games"]["r1b1"]
        for move in game["moves"]:
            u = move.get("usage") or {}
            hit = f"{u['cached'] / u['input']:.1%}" if u.get("input") else "-"
            log(f"ply {move['ply']:>3} {move['side']:<5} {move['san']:<7} {move['elapsed_ms'] / 1000:6.1f}s "
                f"input={u.get('input', 0):>7} cached={u.get('cached', 0):>7} hit={hit}"
                + (f" note={move['note']!r}" if move.get("note") else ""))
        log("CACHE " + json.dumps(state.get("cache_stats")))
        log("REFLECTION " + json.dumps(game.get("reflection"))[:2000])
        pusher.flush(120)
        return 0
    hooks = ForeverHooks(cfg, repo, pusher, live_dir)
    swiss.HOOKS = hooks
    return run_forever(cfg, live_dir, repo, pusher, hooks, args.max_tournaments)


if __name__ == "__main__":
    sys.exit(main())
