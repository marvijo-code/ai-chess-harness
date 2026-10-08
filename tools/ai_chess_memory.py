"""Agent memory, Stockfish ladder and background git push for the forever AI-chess tournament.

The memory lives in its own git repository (public: github.com/marvijo-code/ai-chess-agent-memory):

    agents/<slug>/MEMORY.md                    index + key lessons, written by the player itself (capped)
    agents/<slug>/notes/<topic>.md             optional topic notes (capped, limited count)
    agents/<slug>/games/<tournament>-<game>.md one file per game: result, PGN, notes, memory edits, cache use
    ladder.json                                Stockfish depth ladder (current depth + every step)
    tournaments/<slug>.md                      standings of one tournament
    tournaments/index.md                       every tournament, number and champion
    tournaments/cache-stats.md (+ .json)       input cache hit rate per player

Only this module writes there. Every file is written atomically under one lock; GitPusher commits and pushes
from a background thread with retries, so a slow or failing push never blocks or crashes a game.
"""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import threading
import time
from pathlib import Path

MEMORY_FILE = "MEMORY.md"
MEMORY_MAX_BYTES = 6144
NOTE_MAX_BYTES = 4096
MAX_NOTE_FILES = 8
MAX_EDITS = 6
NOTE_PATH = re.compile(r"^notes/[a-z0-9][a-z0-9-]{0,47}\.md$")
# Em dash, en dash and horizontal bar (built from code points so this file holds none of them).
DASHES = re.compile("[ \t]*[" + chr(0x2013) + chr(0x2014) + chr(0x2015) + "][ \t]*")
FIGURE_DASH = chr(0x2012)
LADDER_FILE = "ladder.json"


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "player"


def clean_text(text: str) -> str:
    """Plain repository text: no NUL, LF line ends, no em or en dashes (owner rule for everything public)."""
    text = str(text).replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = DASHES.sub(" - ", text).replace(FIGURE_DASH, "-")
    return "\n".join(line.rstrip() for line in text.split("\n")).strip("\n") + "\n"


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------- memory edit validation


def validate_edits(edits: object, existing_notes: list[str]) -> tuple[list[dict], list[str]]:
    """Check the edits a reflection returned. Returns (accepted, rejected reasons).

    Allowed: {"path": "MEMORY.md", "content": str} (replace), {"path": "notes/<topic>.md", "content": str}
    (upsert) and {"path": "notes/<topic>.md", "delete": true}. Anything else is rejected, never repaired:
    other paths, traversal, oversize content (the agent must keep its files concise), too many note files."""
    accepted: list[dict] = []
    rejected: list[str] = []
    if not isinstance(edits, list):
        return [], ["edits is not a list"]
    notes = set(existing_notes)
    for index, edit in enumerate(edits[: MAX_EDITS * 2]):
        if len(accepted) >= MAX_EDITS:
            rejected.append(f"edit {index}: more than {MAX_EDITS} edits in one reflection")
            continue
        if not isinstance(edit, dict):
            rejected.append(f"edit {index}: not an object")
            continue
        path = edit.get("path")
        if not isinstance(path, str) or not (path == MEMORY_FILE or NOTE_PATH.match(path)):
            rejected.append(f"edit {index}: path {str(path)[:60]!r} is not MEMORY.md or notes/<lowercase-topic>.md")
            continue
        if edit.get("delete") is True:
            if path == MEMORY_FILE:
                rejected.append(f"edit {index}: MEMORY.md cannot be deleted")
                continue
            notes.discard(path)
            accepted.append({"path": path, "delete": True})
            continue
        content = edit.get("content")
        if not isinstance(content, str) or not content.strip():
            rejected.append(f"edit {index}: {path} has no text content")
            continue
        text = clean_text(content)
        cap = MEMORY_MAX_BYTES if path == MEMORY_FILE else NOTE_MAX_BYTES
        size = len(text.encode("utf-8"))
        if size > cap:
            rejected.append(f"edit {index}: {path} is {size} bytes, over the {cap}-byte cap")
            continue
        if path != MEMORY_FILE and path not in notes and len(notes) >= MAX_NOTE_FILES:
            rejected.append(f"edit {index}: {path} would be note file {len(notes) + 1}, over the limit of {MAX_NOTE_FILES}")
            continue
        if path != MEMORY_FILE:
            notes.add(path)
        accepted.append({"path": path, "content": text})
    if len(edits) > MAX_EDITS * 2:
        rejected.append(f"{len(edits) - MAX_EDITS * 2} more edits ignored")
    return accepted, rejected


def parse_reflection(text: str) -> tuple[dict | None, str]:
    """The JSON object of a reflection reply, or (None, reason)."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None, "the reply has no JSON object"
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        return None, f"the JSON object does not parse: {exc}"
    if not isinstance(data, dict):
        return None, "the reply is not a JSON object"
    return data, ""


# ---------------------------------------------------------------- Stockfish ladder


def new_ladder(player: str, start_depth: int) -> dict:
    return {"player": player, "depth": int(start_depth), "start_depth": int(start_depth), "steps": [],
            "updated_at": None}


def ladder_step(ladder: dict, game: dict, ladder_player: str, tournament: str, number: int | None,
                at: str) -> dict | None:
    """Raise the depth by 1 when an AI player beat the ladder player in this finished game.

    Returns the step record (also appended to ladder["steps"]) or None. A draw, a Stockfish win or an
    unfinished game changes nothing. The depth used in that game is recorded with the step."""
    result = game.get("result")
    if ladder_player not in (game.get("white"), game.get("black")) or result not in ("1-0", "0-1"):
        return None
    winner = game["white"] if result == "1-0" else game["black"]
    if winner == ladder_player:
        return None
    before = int(ladder.get("depth") or ladder.get("start_depth") or 1)
    step = {"from": before, "to": before + 1, "tournament": tournament, "number": number, "game": game.get("id"),
            "winner": winner, "color": "white" if winner == game.get("white") else "black",
            "played_at_depth": game.get("stockfish_depth", before), "termination": game.get("termination", ""),
            "at": at}
    ladder["depth"] = before + 1
    ladder.setdefault("steps", []).append(step)
    ladder["updated_at"] = at
    return step


# ---------------------------------------------------------------- the repository


class MemoryRepo:
    def __init__(self, root: Path, log=print) -> None:
        self.root = Path(root)
        self.log = log
        self.lock = threading.RLock()

    # -- paths
    def agent_dir(self, name: str) -> Path:
        return self.root / "agents" / slugify(name)

    def memory_text(self, name: str) -> str:
        try:
            return (self.agent_dir(name) / MEMORY_FILE).read_text(encoding="utf-8")
        except OSError:
            return ""

    def note_paths(self, name: str) -> list[str]:
        folder = self.agent_dir(name) / "notes"
        return sorted(f"notes/{p.name}" for p in folder.glob("*.md")) if folder.is_dir() else []

    def notes_text(self, name: str, limit: int = MAX_NOTE_FILES) -> list[tuple[str, str]]:
        out = []
        for rel in self.note_paths(name)[:limit]:
            try:
                out.append((rel, (self.agent_dir(name) / rel).read_text(encoding="utf-8")))
            except OSError:
                continue
        return out

    # -- writes
    def apply_edits(self, name: str, edits: list[dict]) -> list[str]:
        """Apply already-validated edits. Returns the changed paths relative to the agent folder."""
        changed = []
        with self.lock:
            base = self.agent_dir(name)
            for edit in edits:
                target = (base / edit["path"]).resolve()
                if base.resolve() not in target.parents:
                    continue  # validate_edits already refuses this; never write outside the agent folder
                if edit.get("delete"):
                    if target.exists():
                        target.unlink()
                        changed.append(edit["path"])
                    continue
                atomic_write(target, edit["content"])
                changed.append(edit["path"])
        return changed

    def write_file(self, rel: str, text: str) -> Path:
        with self.lock:
            path = self.root / rel
            atomic_write(path, clean_text(text))
            return path

    def write_json(self, rel: str, data: dict) -> None:
        with self.lock:
            atomic_write(self.root / rel, json.dumps(data, indent=1, ensure_ascii=False) + "\n")

    def read_json(self, rel: str) -> dict | None:
        try:
            data = json.loads((self.root / rel).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    # -- ladder
    def load_ladder(self, player: str, start_depth: int) -> dict:
        with self.lock:
            data = self.read_json(LADDER_FILE)
            if not data or data.get("player") != player:
                data = new_ladder(player, start_depth)
                self.write_json(LADDER_FILE, data)
            return data

    def save_ladder(self, ladder: dict) -> None:
        self.write_json(LADDER_FILE, ladder)

    # -- tournaments
    def next_tournament_number(self) -> int:
        index = self.read_json("tournaments/index.json") or {}
        numbers = [int(t.get("number") or 0) for t in index.get("tournaments") or []]
        return max(numbers, default=0) + 1

    def record_tournament(self, state: dict) -> None:
        """tournaments/<slug>.md plus the index (json + md). Called after every game and at the end."""
        with self.lock:
            index = self.read_json("tournaments/index.json") or {"tournaments": []}
            rows = [t for t in index.get("tournaments") or [] if t.get("id") != state.get("id")]
            ko = state.get("knockout") or {}
            rows.append({"number": state.get("number"), "id": state.get("id"), "title": state.get("title"),
                         "created_at": state.get("created_at"), "finished": bool(state.get("finished")),
                         "champion": ko.get("champion") or (state.get("winner") if state.get("finished") else None),
                         "updated_at": state.get("updated_at")})
            rows.sort(key=lambda t: int(t.get("number") or 0))
            index["tournaments"] = rows
            self.write_json("tournaments/index.json", index)
            lines = ["# Tournaments", "", "| # | Tournament | Started | Champion | Status |", "| --- | --- | --- | --- | --- |"]
            for t in reversed(rows):
                lines.append(f"| {t.get('number')} | [{t.get('id')}]({t.get('id')}.md) | {t.get('created_at') or ''} | "
                             f"{t.get('champion') or ''} | {'finished' if t.get('finished') else 'live'} |")
            self.write_file("tournaments/index.md", "\n".join(lines))
            self.write_file(f"tournaments/{state['id']}.md", tournament_markdown(state))

    def record_cache_stats(self, state: dict) -> None:
        """Per player: this tournament and all tournaments together (cumulative json, rendered md)."""
        with self.lock:
            data = self.read_json("tournaments/cache-stats.json") or {"tournaments": {}}
            data.setdefault("tournaments", {})[state["id"]] = {"number": state.get("number"),
                                                                "players": state.get("cache_stats") or {}}
            self.write_json("tournaments/cache-stats.json", data)
            self.write_file("tournaments/cache-stats.md", cache_stats_markdown(data, state["id"]))


def pct(value: float | None) -> str:
    return "" if value is None else f"{value * 100:.1f}%"


def cache_stats_markdown(data: dict, current: str) -> str:
    totals: dict[str, dict] = {}
    for entry in (data.get("tournaments") or {}).values():
        for name, row in (entry.get("players") or {}).items():
            t = totals.setdefault(name, {"calls": 0, "input": 0, "cached": 0, "warm_input": 0, "warm_cached": 0})
            for key in t:
                t[key] += int(row.get(key) or 0)
    lines = ["# Input cache hit rate", "",
             "Hit rate = cached input tokens / all input tokens of the move requests, read from every provider "
             "response (claude cache_read_input_tokens, codex cached_input_tokens, OpenAI-compatible "
             "prompt_tokens_details.cached_tokens). Warm = without each game's first 3 moves of the player.", ""]
    current_rows = ((data.get("tournaments") or {}).get(current) or {}).get("players") or {}
    for title, rows in ((f"Current tournament ({current})", current_rows), ("All tournaments", totals)):
        lines += [f"## {title}", "", "| Player | Requests | Input tokens | Cached | Hit rate | Warm hit rate |",
                  "| --- | ---: | ---: | ---: | ---: | ---: |"]
        for name, row in sorted(rows.items()):
            hit = row["cached"] / row["input"] if row.get("input") else None
            warm = row["warm_cached"] / row["warm_input"] if row.get("warm_input") else None
            lines.append(f"| {name} | {row.get('calls', 0)} | {row.get('input', 0)} | {row.get('cached', 0)} | {pct(hit)} | {pct(warm)} |")
        lines.append("")
    return "\n".join(lines)


def tournament_markdown(state: dict) -> str:
    ko = state.get("knockout") or {}
    ladder = state.get("ladder") or {}
    lines = [f"# {state.get('title')} ({state.get('id')})", "",
             f"Started {state.get('created_at')}. Status: {'finished' if state.get('finished') else 'live'}."]
    if ko.get("champion"):
        lines.append(f"Champion: **{ko['champion']}**, runner-up {ko.get('runner_up')}, third {ko.get('third')}.")
    if ladder:
        lines.append(f"Stockfish ladder: {ladder.get('player')} now plays at depth {ladder.get('depth')}.")
    lines += ["", "## Round robin table", "", "| # | Player | Pts | Elo | W/D/L | Forfeits | Flags |",
              "| ---: | --- | ---: | ---: | --- | ---: | ---: |"]
    for r in state.get("standings") or []:
        lines.append(f"| {r['rank']} | {r['name']} | {r['points']:g} | {r['elo']:.0f} | {r['wins']}/{r['draws']}/{r['losses']} | "
                     f"{r['forfeits']} | {r['flags']} |")
    lines += ["", "## Games", "", "| Game | White | Black | Result | How |", "| --- | --- | --- | --- | --- |"]
    for rnd in state.get("rounds") or []:
        for p in rnd.get("pairings") or []:
            g = (state.get("games") or {}).get(p["game_id"]) or {}
            label = p.get("label") or f"Round {rnd['round']}"
            depth = g.get("stockfish_depth")
            names = [g.get("white", p["white"]), g.get("black", p["black"])]
            if depth:
                names = [f"{n} (depth {depth})" if n == ladder.get("player") else n for n in names]
            lines.append(f"| {p['game_id']} {label} | {names[0]} | {names[1]} | {g.get('result', '*')} | "
                         f"{(g.get('termination') or g.get('status') or '').replace('|', '/')} |")
    return "\n".join(lines)


def game_markdown(state: dict, game: dict, player: str, reflection: dict | None, usage: dict | None) -> str:
    color = "white" if game.get("white") == player else "black"
    opponent = game.get("black") if color == "white" else game.get("white")
    lines = [f"# {state.get('title')} - {game.get('label') or 'Round ' + str(game.get('round'))} ({game.get('id')})", "",
             f"- Me: {player} ({color})", f"- Opponent: {opponent}"
             + (f" at depth {game['stockfish_depth']}" if game.get("stockfish_depth") and opponent == (state.get('ladder') or {}).get('player') else ""),
             f"- Result: {game.get('result')} ({game.get('termination')})", f"- Tournament: {state.get('id')}",
             f"- Played: {game.get('start')} to {game.get('end')}"]
    if usage and usage.get("input"):
        lines.append(f"- Input tokens: {usage['input']}, cached {usage['cached']} ({pct(usage['cached'] / usage['input'])})")
    notes = [n for n in game.get("notes") or [] if n.get("player") == player]
    lines += ["", "## My notes during the game", ""]
    lines += [f"- ply {n['ply']} ({n.get('san', '')}): {n['note']}" for n in notes] or ["(none)"]
    if reflection:
        lines += ["", "## After the game", "", reflection.get("summary") or "(no summary)"]
        if reflection.get("applied"):
            lines.append("")
            lines.append("Memory files changed: " + ", ".join(reflection["applied"]))
        if reflection.get("rejected"):
            lines.append("")
            lines.append("Rejected edits: " + "; ".join(reflection["rejected"]))
    lines += ["", "## PGN", "", "```", (game.get("pgn") or "").strip(), "```"]
    return "\n".join(lines)


# ---------------------------------------------------------------- git push in the background


class GitPusher:
    """Commits and pushes the memory repo from one background thread.

    request(message) queues a commit; the worker does `git add -A`, commits when anything changed, then
    pushes with retries (30 s doubling to 10 min). It pulls with rebase when the push is rejected. Nothing
    here raises into the caller; failures are logged and retried on the next request or retry tick."""

    def __init__(self, repo: MemoryRepo, push: bool = True, log=print, remote: str = "origin") -> None:
        self.repo = repo
        self.push_enabled = push
        self.log = log
        self.remote = remote
        self.queue: queue.Queue[str | None] = queue.Queue()
        self.pending_push = False
        self.last_error = ""
        self.pushed = 0
        self.commits = 0
        self.thread = threading.Thread(target=self._loop, daemon=True, name="memory-git")
        self.thread.start()

    def git(self, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
        return subprocess.run(["git", "-C", str(self.repo.root), *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout, env=env)

    def request(self, message: str) -> None:
        self.queue.put(message)

    def flush(self, timeout: float = 120) -> bool:
        """Wait until the queue is empty and nothing is waiting to be pushed (tests and shutdown)."""
        done = threading.Event()
        self.queue.put(None)
        self._flush_event = done
        return done.wait(timeout)

    def _loop(self) -> None:
        delay = 30.0
        next_retry = None
        while True:
            try:
                wait = None if next_retry is None else max(0.5, next_retry - time.monotonic())
                message = self.queue.get(timeout=wait)
            except queue.Empty:
                message = ""
            try:
                if message:
                    messages = [message]
                    while True:  # several finished games at once: one commit
                        try:
                            more = self.queue.get_nowait()
                        except queue.Empty:
                            break
                        if more is None:
                            self.queue.put(None)
                            break
                        messages.append(more)
                    self._commit(messages)
                if self.push_enabled and self.pending_push:
                    if self._push():
                        delay, next_retry = 30.0, None
                    else:
                        next_retry = time.monotonic() + delay
                        delay = min(600.0, delay * 2)
                if message is None:
                    event = getattr(self, "_flush_event", None)
                    if event is not None:
                        event.set()
            except Exception as exc:  # never let the worker die
                self.last_error = f"{type(exc).__name__}: {exc}"[:300]
                self.log(f"memory git: {self.last_error}")
                next_retry = time.monotonic() + delay

    def _commit(self, messages: list[str]) -> None:
        with self.repo.lock:
            self.git("add", "-A")
            status = self.git("status", "--porcelain")
            if not status.stdout.strip():
                return
            subject = messages[0] if len(messages) == 1 else f"{messages[0]} (+{len(messages) - 1} more)"
            body = "\n".join(messages[1:]) if len(messages) > 1 else ""
            args = ["commit", "-q", "-m", clean_text(subject).strip()]
            if body:
                args += ["-m", clean_text(body).strip()]
            result = self.git(*args)
        if result.returncode == 0:
            self.commits += 1
            self.pending_push = True
        else:
            self.last_error = (result.stderr or result.stdout).strip()[:300]
            self.log(f"memory git commit failed: {self.last_error}")

    def _push(self) -> bool:
        result = self.git("push", "-q", self.remote, "HEAD", timeout=180)
        if result.returncode != 0:
            text = (result.stderr or result.stdout).strip()
            if "rejected" in text or "fetch first" in text or "non-fast-forward" in text:
                pull = self.git("pull", "-q", "--rebase", self.remote, timeout=180)
                if pull.returncode == 0:
                    result = self.git("push", "-q", self.remote, "HEAD", timeout=180)
                    text = (result.stderr or result.stdout).strip()
        if result.returncode == 0:
            self.pending_push = False
            self.pushed += 1
            self.last_error = ""
            return True
        self.last_error = text[:300]
        self.log(f"memory git push failed (will retry): {self.last_error}")
        return False


def ensure_repo(root: Path, remote_url: str | None, log=print) -> None:
    """Clone the memory repo when it is missing; set a commit identity when the repo has none."""
    root = Path(root)
    if not (root / ".git").exists():
        if remote_url:
            log(f"memory repo: cloning {remote_url} into {root}")
            subprocess.run(["git", "clone", "-q", remote_url, str(root)], check=True, timeout=300,
                           env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))
        else:
            root.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
    for key, value in (("user.name", "AI Chess Tournament"),
                       ("user.email", "ai-chess-tournament@users.noreply.github.com")):
        have = subprocess.run(["git", "-C", str(root), "config", key], capture_output=True, text=True)
        if not have.stdout.strip():
            subprocess.run(["git", "-C", str(root), "config", key, value], check=True)
