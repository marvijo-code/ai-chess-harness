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
* Stockfish ladder: every AI player has its own depth against the ladder player, starting at
  `forever.ladderStartDepth`. Two wins without a loss in between raise that player's depth by 1 (a draw keeps the
  streak, a loss, forfeit or flag resets it). ladder.json in the memory repo, kept across tournaments and restarts.
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
from harness_version import HARNESS_VERSION  # noqa: E402
from ai_chess_memory import (GitPusher, MemoryRepo, MEMORY_MAX_BYTES, MEMORY_TARGET_BYTES, MAX_NOTE_FILES,  # noqa: E402
                             NOTE_MAX_BYTES,
                             LADDER_WINS_TO_STEP, LINE_COUNTED_PLIES, LINE_MAX_PLIES, MAX_LINES_PER_FILE, ensure_repo, game_markdown, ladder_depth, ladder_entry, ladder_step, line_allowance, line_shape, parse_reflection, player_folders, slugify,
                             text_sha, validate_edits)
from play_llm_series import LlmEngine, iso_now, parse_info, parse_note, parse_usage, write_text_retry  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "configs" / "ai-chess-vps.json"
POINTER_NAME = "current.json"
FOREVER_DEFAULTS = {"pauseSeconds": 120, "memoryRepo": str(ROOT / "out" / "ai-chess-agent-memory"), "memoryRemote": None,
                    "push": True, "ladderPlayer": "Stockfish 19", "ladderStartDepth": 4, "reflection": True,
                    "reflectionTimeoutSeconds": 600, "marksWaitSeconds": 60, "rosterCheck": True,
                    "rosterCheckTimeoutSeconds": 300, "minPlayers": 3, "rosterRetrySeconds": 900,
                    # Second, metered route of a player (player["fallback"]): 0 = never used. See MeteredBudget.
                    "meteredDailyBudgetUsd": 0.0, "meteredFloorUsd": 0.5, "meteredReserveUsd": 1.0,
                    "routeCheckTimeoutSeconds": 120, "routeRetrySeconds": 600,
                    # The subscription failed while its plan still has credits (timeout, server error): ask it again.
                    "routeErrorRetrySeconds": 60,
                    # A new tournament is refused while the disk is nearly full (diskRefuseMb): look again after this.
                    "diskRetrySeconds": 600}
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
    shape = line_shape(memory)
    lines_allowed, over_allowed = line_allowance(memory)
    line_status = (f"Your MEMORY.md now holds {shape['lines']} such lines (longest {shape['longest']} plies); "
                   f"the new version may hold at most {lines_allowed} lines and {over_allowed} longer than "
                   f"{LINE_MAX_PLIES} plies.")
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
        "Learn like a human player, not like a database. Keep patterns: the check you skipped and the sign that "
        "was on the board, the kind of position you misjudge, how each opponent plays, how you spend your clock, "
        "which openings suit you. Do not store long move sequences: a lost game teaches a check, not a line. "
        f"A line (a run of consecutive moves) may be at most {LINE_MAX_PLIES} plies long, and each file may hold "
        f"at most {MAX_LINES_PER_FILE} lines of {LINE_COUNTED_PLIES}+ plies: keep only the opening moves you "
        f"actually play. {line_status}",
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
                more, rejected = validate_edits(edits, sorted(notes_now), repo.current_texts(name))
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
                              "merging or dropping your weakest lessons; do not only shorten words. Too many or too "
                              "long move sequences: cut each named line to the pattern it teaches.")
        out["seconds"] = round(time.monotonic() - started, 1)
        out["rejected"] = rejected
        out["applied"] = repo.apply_edits(name, accepted)
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"[:300]
    out["memory_after"] = text_sha(repo.memory_text(name))
    return out


# ---------------------------------------------------------------- subscription first, metered second


class MeteredBudget:
    """Daily cap for the metered second routes (owner approval 2026-10-12: R170 a day for DeepSeek and GLM).

    What was spent today comes from OpenRouter itself (`/key` usage_daily of the key in use, UTC day), so every
    call counts: moves, notes, retries and route checks. Other use of the same key counts as well, which only
    makes the cap stricter. `/credits` keeps `floor_usd` of the balance untouched, so a game never runs into an
    empty account. No answer from OpenRouter = nothing may be spent."""

    API = "https://openrouter.ai/api/v1"
    CACHE_SECONDS = 60

    def __init__(self, daily_usd: float, floor_usd: float = 0.5, fetch=None, clock=time.monotonic) -> None:
        self.daily_usd = float(daily_usd or 0)
        self.floor_usd = float(floor_usd or 0)
        self.fetch = fetch or self._fetch
        self.clock = clock
        self._seen: tuple[float, float, str] | None = None   # (when, remaining, note)
        self._lock = threading.Lock()

    def _fetch(self, path: str) -> dict:
        import urllib.request

        import subscription_providers as sp

        key = os.environ.get("OPENROUTER_API_KEY") or sp._user_env("OPENROUTER_API_KEY")
        if not key:
            raise RuntimeError("OPENROUTER_API_KEY is not set")
        request = urllib.request.Request(self.API + path, headers={"Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(request, timeout=20) as response:
            return (json.loads(response.read().decode("utf-8")) or {}).get("data") or {}

    def remaining(self) -> float:
        """USD that may still be spent today (0 = none, also when OpenRouter does not answer)."""
        with self._lock:
            now = self.clock()
            if self._seen and now - self._seen[0] < self.CACHE_SECONDS:
                return self._seen[1]
            if self.daily_usd <= 0:
                left, note = 0.0, "metered routes are off (meteredDailyBudgetUsd 0)"
            else:
                try:
                    key = self.fetch("/key")
                    credits = self.fetch("/credits")
                    spent = float(key.get("usage_daily") or 0)
                    balance = float(credits.get("total_credits") or 0) - float(credits.get("total_usage") or 0)
                    parts = [self.daily_usd - spent, balance - self.floor_usd]
                    if key.get("limit_remaining") is not None:
                        parts.append(float(key["limit_remaining"]))
                    left = max(0.0, min(parts))
                    note = f"spent today ${spent:.2f} of ${self.daily_usd:.2f}, account balance ${balance:.2f}"
                except Exception as exc:
                    left, note = 0.0, f"OpenRouter did not answer ({type(exc).__name__}: {str(exc)[:120]})"
            self._seen = (now, left, note)
            return left

    def note(self) -> str:
        self.remaining()
        return self._seen[2] if self._seen else ""


BUDGET: MeteredBudget | None = None   # set by main() when the config allows a metered second route


def fallbacks_of(player: dict) -> list[dict]:
    """The player's fallback routes in order: the "fallbacks" list when given (free routes first, metered
    last), otherwise the older single "fallback" object, which was always the metered route."""
    routes = player.get("fallbacks")
    if isinstance(routes, list):
        return [r for r in routes if isinstance(r, dict)]
    single = player.get("fallback")
    if not isinstance(single, dict):
        return []
    return [dict(single, metered=single.get("metered", True))]


def player_on_route(player: dict, route: dict) -> dict:
    """The player on one fallback route: same name, memory and clock, another provider and model."""
    out = {k: v for k, v in player.items() if k not in ("fallback", "fallbacks")}
    out.update(route)
    out["metered"] = bool(route.get("metered"))
    return out


def fallback_player(player: dict) -> dict:
    """The player on its metered fallback route (the last route when none is marked metered)."""
    routes = [r for r in fallbacks_of(player) if r.get("metered")] or fallbacks_of(player)
    return player_on_route(player, routes[-1]) if routes else dict(player)


def first_route_is_metered(player: dict) -> bool:
    """True when the player's FIRST route spends money: an explicit metered flag, or OpenRouter credit."""
    return bool(player.get("metered")) or player.get("provider") == "openrouter-chat"


def players_with_metered_route(cfg: dict) -> list[str]:
    """The players that can spend money, on their first route or on a metered fallback: only they need the
    daily budget (2026-10-25: a metered FIRST route was never asked, so its player ignored the cap)."""
    return [p["name"] for p in cfg.get("players") or []
            if first_route_is_metered(p) or any(r.get("metered") for r in fallbacks_of(p))]


def cap_note(budget: MeteredBudget | None) -> str:
    return budget.note() if budget else "metered routes are off (meteredDailyBudgetUsd 0)"


def probe_with_fallback(player: dict, cfg: dict, live_dir: Path, timeout: float, budget: MeteredBudget | None,
                        prober) -> dict:
    """Roster check of one player: the subscription route first; when it is down and today's cap leaves
    room for a tournament, the metered route. The row says which route answered.

    A player whose first route is metered is held back here too, before a single call: with no room left the
    owner's daily cap would be spent by a tournament the budget never saw."""
    reserve = float(cfg["forever"].get("meteredReserveUsd") or 0)
    if first_route_is_metered(player):
        left = budget.remaining() if budget is not None else 0.0
        if left < max(reserve, 0.01):
            return {"name": player["name"], "ok": False, "route": player.get("route"), "metered": True,
                    "reason": "the metered first route needs today's cap: " + cap_note(budget),
                    "kind": "daily metered cap reached"}
    row = prober(player, cfg, live_dir, timeout)
    routes = fallbacks_of(player)
    if row.get("ok") or not routes:
        return row
    if not plan_out_of_credits(player, row):
        row["metered"] = "second route not used: the plan still has credits (a fallback is for an empty plan)"
        return row
    for route in routes:
        if route.get("metered"):
            if budget is None or budget.remaining() < max(reserve, 0.01):
                row["metered"] = "second route not used: " + cap_note(budget)
                continue
        second = prober(player_on_route(player, route), cfg, live_dir, timeout)
        if second.get("ok"):
            second.update(name=player["name"], primary_reason=row.get("reason", ""))
            if route.get("metered"):
                second["metered"] = True
            return second
        row["metered"] = f"second route failed too: {second.get('reason', '')}"[:240]
    return row


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
        # Every AI in the config gets its own ladder record now (depth start_depth, streak 0 when first seen).
        known = set(self.ladder.get("players") or {})
        for p in cfg.get("players") or []:
            if p.get("provider") != "uci":
                ladder_entry(self.ladder, p["name"])
        if set(self.ladder.get("players") or {}) != known:
            repo.save_ladder(self.ladder)
        self.lock = threading.RLock()
        self.context_dir = live_dir / "contexts"
        # sha of each player's MEMORY.md right after its last reflection: its next game must start from it.
        learning = repo.read_json("tournaments/learning.json") or {}
        self.last_written: dict[str, str] = dict(learning.get("last_written") or {})
        self.prober = None        # (player, cfg, live_dir, timeout) -> row; replaceable in tests
        self.sleep = time.sleep   # the wait while no route is allowed; replaceable in tests

    def player_for_game(self, player: dict) -> dict:
        """Asked once before every game. A player with a second route: the subscription is checked with one
        real move and used when it answers; when it is down the metered route plays this game, as long as
        today's cap has room. With no route allowed the game waits here and both are checked again.

        A player whose FIRST route is metered is not asked here: the cap is asked when the roster is checked,
        so a running tournament is never stalled between moves, and one tournament of overspend is the most
        a spent cap can cost."""
        routes = fallbacks_of(player)
        if not routes or player.get("provider") == "uci":
            return player
        if BUDGET is None and not [r for r in routes if not r.get("metered")]:
            return player
        fv = self.cfg["forever"]
        prober = self.prober or probe_player
        name = player["name"]
        check_timeout = float(fv.get("routeCheckTimeoutSeconds") or 120)
        while True:
            row = prober(player, self.cfg, self.live_dir, check_timeout)
            if row.get("ok"):
                log(f"ROUTE {name}: the subscription route {player.get('provider')} answers, it plays this game")
                return player
            if not plan_out_of_credits(player, row):
                wait = float(fv.get("routeErrorRetrySeconds") or 60)
                log(f"ROUTE {name}: subscription route failed but the plan still has credits "
                    f"({row.get('reason', '')[:120]}); a fallback is only for an empty plan, "
                    f"asking the subscription again in {wait:.0f}s")
                self.sleep(wait)
                continue
            reason = row.get("kind") or row.get("reason", "")
            for route in routes:
                if route.get("metered"):
                    left = BUDGET.remaining() if BUDGET is not None else 0.0
                    if left <= 0:
                        continue
                    second = player_on_route(player, route)
                    log(f"ROUTE {name}: subscription route down ({reason}); metered route "
                        f"{second.get('provider')} {second.get('model')} plays this game "
                        f"(${left:.2f} of today's cap left)")
                    return second
                second = player_on_route(player, route)
                check = prober(second, self.cfg, self.live_dir, check_timeout)
                if check.get("ok"):
                    log(f"ROUTE {name}: subscription route down ({reason}); free route "
                        f"{route.get('provider')} {route.get('model')} answers, it plays this game")
                    return second
                log(f"ROUTE {name}: free route {route.get('provider')} {route.get('model')} did not answer "
                    f"({check.get('reason', '')[:120]})")
            wait = float(fv.get("routeRetrySeconds") or 600)
            log(f"ROUTE {name}: subscription route down and no fallback allowed ({BUDGET.note()}); "
                f"checking every route again in {wait:.0f}s")
            self.sleep(wait)

    @staticmethod
    def route_player(player: dict, game: dict) -> dict:
        """The player on the route this game was played on (its notes call uses the same one)."""
        side = "white" if game.get("white") == player.get("name") else "black"
        route = (game.get("route") or {}).get(side)
        if not route or (route.get("provider"), route.get("model")) == (player.get("provider"), player.get("model")):
            return player
        return player_on_route(player, route)

    def ladder_view(self) -> dict:
        with self.lock:
            players = {name: {"depth": int(p.get("depth") or 0), "streak": int(p.get("streak") or 0)}
                       for name, p in sorted((self.ladder.get("players") or {}).items())}
            return {"player": self.ladder["player"], "start_depth": self.ladder.get("start_depth"),
                    "wins_to_step": LADDER_WINS_TO_STEP, "players": players,
                    "last_step": self.ladder.get("last_step")}

    def before_game(self, ts, game_id: str, white: LlmEngine, black: LlmEngine) -> None:
        state = ts.state
        game = state["games"][game_id]
        with ts.lock:
            for engine, opponent in ((white, black), (black, white)):
                if engine.is_uci and engine.name == self.ladder_player and not opponent.is_uci:
                    # Stockfish plays each AI at that AI's own depth.
                    with self.lock:
                        depth = ladder_depth(self.ladder, opponent.name)
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
            # the player's memory for this game: on a full disk keep trying for about a minute
            write_text_retry(path, json.dumps(context, indent=1), delays=(1, 3, 10, 20, 30))
            engine.send(f"setoption name GameContextFile value {path}")
            with ts.lock:
                used = {k: engine.player.get(k) for k in ("provider", "model", "metered", "effort", "maxPrice")
                        if engine.player.get(k) is not None}
                used["metered"] = bool(engine.player.get("metered"))
                game.setdefault("route", {})[side] = used
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
                self.ladder["last_step"] = step
                self.repo.save_ladder(self.ladder)
        if step:
            ai, sf = step["player"], self.ladder_player
            if step["stepped"]:
                log(f"LADDER: {ai} now plays {sf} at depth {step['to']} (won twice at depth {step['from']} "
                    f"without a loss, last in {game_id})")
                messages.append(f"Ladder: {ai} now plays {sf} at depth {step['to']} after 2 wins at depth {step['from']}")
            elif step["outcome"] == "win":
                log(f"LADDER: {ai} beat {sf} at depth {step['from']} (streak {step['streak']} of {LADDER_WINS_TO_STEP}) in {game_id}")
                messages.append(f"Ladder: {ai} beat {sf} at depth {step['from']}, streak {step['streak']} of {LADDER_WINS_TO_STEP}")
            elif step["outcome"] == "loss":
                log(f"LADDER: {ai} lost to {sf} at depth {step['from']}; streak reset to 0 in {game_id}")
            else:
                log(f"LADDER: {ai} drew {sf} at depth {step['from']}; streak stays {step['streak']} of {LADDER_WINS_TO_STEP} in {game_id}")
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
                    reflections[name] = reflect(self.route_player(players[name], game), state, game, self.repo,
                                                self.cfg, marks, self.client_factory)
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


def opencode_go_usage(timeout: float = 15) -> dict | None:
    """The OpenCode Go plan's usage windows {"rolling"|"weekly"|"monthly": {"status", "resetsAt"}}, or None."""
    import urllib.request

    import subscription_providers as sp

    key = os.environ.get("OPENCODE_GO_API_KEY") or sp._user_env("OPENCODE_GO_API_KEY")
    if not key:
        return None
    request = urllib.request.Request(OPENCODE_GO_USAGE_URL, headers={"Authorization": f"Bearer {key}", "User-Agent": sp.BROWSER_UA})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            usage = (json.loads(response.read().decode("utf-8")) or {}).get("usage")
    except Exception:
        return None
    return usage if isinstance(usage, dict) else None


def opencode_go_reset(timeout: float = 15) -> str | None:
    """The resetsAt of the OpenCode Go window that is rate limited (rolling, weekly or monthly), or None."""
    usage = opencode_go_usage(timeout) or {}
    limited = [w.get("resetsAt") for w in usage.values() if isinstance(w, dict) and w.get("status") != "ok" and w.get("resetsAt")]
    return max(limited) if limited else None


def plan_out_of_credits(player: dict, row: dict) -> bool:
    """True only when the subscription plan itself has no credits left (owner 2026-10-16: the metered route
    is for an empty plan, not for a timeout or a server error). OpenCode Go: its own usage page decides when
    it answers; otherwise the error must be a funds or usage-limit error."""
    import subscription_providers as sp

    reason = row.get("reason") or ""
    if bench_kind(reason) == "no subscription funds or plan":
        return True
    if player.get("provider") == "opencode-go":
        usage = opencode_go_usage()
        if usage is not None:
            return any(isinstance(w, dict) and w.get("status") != "ok" for w in usage.values())
    return sp.limit_error(reason)


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
        rows = list(pool.map(lambda pl: probe_with_fallback(pl, cfg, live_dir, timeout, BUDGET, prober), players))
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


def attempt(what: str, action) -> bool:
    """Run a file write the loop can live without; a failure is one log line, never the end of the runner."""
    try:
        action()
        return True
    except Exception as exc:
        log(f"WRITE FAILED ({what}): {swiss.disk_guard.describe_error(exc)}; the runner goes on")
        return False


def tournament_slug(number: int) -> str:
    return f"aichess-{number:04d}-{time.strftime('%Y%m%d-%H%M%S')}"


def tournament_roster(cfg: dict, live_dir: Path, number: int) -> tuple[dict, list[dict]]:
    """An operator hold expires by tournament number, without changing the configured players."""
    path = live_dir / "roster-holds.json"
    holds = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if not isinstance(holds, dict) or any(type(n) is not int or n < 1 for n in holds.values()):
        raise ValueError(f"{path}: expected player names mapped to positive tournament numbers")
    playing, benched = [], []
    for player in cfg["players"]:
        through = holds.get(player["name"], 0)
        if number <= through:
            benched.append({"name": player["name"], "route": player.get("route"),
                            "kind": f"temporary hold through tournament #{through}",
                            "reason": f"Owner requested a break; eligible again in tournament #{through + 1}",
                            "resume_after_tournament": through, "checked_at": iso_now()})
        else:
            playing.append(player)
    return dict(cfg, players=playing), benched


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
            # A full disk stops state saves: no new tournament starts on one (a running one is never stopped).
            if swiss.check_disk(live_dir, cfg, log, "before a new tournament", starting=True)["level"] == "critical":
                wait = float(cfg["forever"].get("diskRetrySeconds") or 600)
                log(f"NOT STARTING a new tournament: the disk is nearly full; checking again in {wait:.0f}s")
                sleep(wait)
                continue
            number = repo.next_tournament_number()
            if pointer and pointer.get("number"):
                number = max(number, int(pointer["number"]) + 1)
            roster_cfg, held = tournament_roster(cfg, live_dir, number)
            roster, benched = list(roster_cfg["players"]), []
            if cfg["forever"].get("rosterCheck", True):
                log("roster check: one real move from every player")
                roster, benched, rows = check_roster(roster_cfg, live_dir)
                for row in rows:
                    log("ROSTER " + json.dumps({k: row.get(k) for k in ("name", "ok", "route", "metered", "move", "seconds",
                                                                         "kind", "resets_at", "reason")}))
            benched.extend(held)
            need = int(cfg["forever"].get("minPlayers") or 3)
            if len(roster) < need:
                wait = float(cfg["forever"].get("rosterRetrySeconds") or 900)
                log(f"only {len(roster)} players can play (need {need}); checking again in {wait:.0f}s")
                sleep(wait)
                continue
            slug = tournament_slug(number)
            tcfg = dict(cfg, players=roster)
            if tcfg.get("format") == "round-robin+knockout":
                tcfg["rounds"] = swiss.max_rounds(len(roster))
            state = swiss.new_state(tcfg, slug, title=f"{cfg['title']} #{number}")
            state["number"] = number
            state["harness_version"] = HARNESS_VERSION
            state["benched"] = benched
            state["format"] = {"type": tcfg.get("format"), "rr_rounds": tcfg["rounds"],
                               "ko_size": min(int(tcfg.get("knockoutSize", 4)), len(roster))}
            status_path = live_dir / f"{slug}-tournament.json"
            log(f"starting Tournament #{number} ({slug}): " + ", ".join(p["name"] for p in roster)
                + ("; " + "; ".join(f"{b['name']} {bench_label(b)}" for b in benched) if benched else ""))
        state["ladder"] = hooks.ladder_view()
        swiss.TournamentState(state, status_path, log).save()
        # A failed write here (a full disk) never ends the runner: the tournament is played from memory.
        attempt("the pointer file", lambda: write_pointer(live_dir, status_path, state))
        attempt("the tournament index", lambda: repo.record_tournament(state))
        if pusher is not None:
            pusher.request(f"Tournament #{state.get('number')} started ({state['id']})")
        code = swiss.run_tournament(state, state["config"], status_path, log)
        if code != 0:
            log(f"Tournament #{state.get('number')} paused ({state.get('paused')}); trying again in {PAUSED_RETRY_SECONDS}s")
            sleep(PAUSED_RETRY_SECONDS)
            continue
        played += 1
        attempt("the tournament index", lambda: repo.record_tournament(state))
        attempt("the cache table", lambda: repo.record_cache_stats(state))
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
    global BUDGET
    second = players_with_metered_route(cfg)
    if second and float(fv.get("meteredDailyBudgetUsd") or 0) > 0:
        BUDGET = MeteredBudget(fv["meteredDailyBudgetUsd"], fv.get("meteredFloorUsd"))
        log(f"metered routes for {', '.join(second)}: at most ${BUDGET.daily_usd:.2f} a day, used only while "
            f"a subscription or free route is down, or by a metered first route ({BUDGET.note()})")

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
