"""Spoken commentary for the live LLM tournament viewer (OpenRouter, metered).

A background thread watches the tournament state JSON. It roams the boards like a TV commentator:
each line goes to the board that matters most right now (tournament leaders playing, fresh blunders
or strong moves, checks, captures, time trouble, a result just in), with a nudge to rotate boards.
A board the viewer pins (focus mode) keeps the commentary. At most one line every MIN_GAP_SECONDS,
written from the recent moves, the players' own move comments and the Stockfish move marks, then
voiced with an OpenRouter speech model. Clips are WAV files in out_dir; the viewer polls `clips()` and plays
them one at a time. A hard spending cap stops all calls once reached.

Measured 2026-10-06: Gemini 3.8 Flash TTS, a 12.5 s line = $0.0028 (PCM only, 24 kHz mono).
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path

API = "https://openrouter.ai/api/v1"
TEXT_MODEL = "google/gemini-3.1-flash-lite"
TTS_MODEL = "google/gemini-3.8-flash-tts"
TTS_VOICE = "Puck"
TTS_STYLE = "Speak like an excited but clear chess commentator."
SAMPLE_RATE = 24000
MIN_GAP_SECONDS = 12.0
# Only spend while someone listens: an unmuted page polls clips every 2 s (COMMENTARY_ALWAYS=1 overrides).
LISTENER_SECONDS = 20.0
DEFAULT_BUDGET_USD = 1.5
# Spoken names: say the version numbers the way a commentator would.
SPOKEN = {"GPT-6.1 Sol": "GPT six point one Sol", "Grok 4.7": "Grok four point seven",
          "Sonnet 5.5": "Sonnet five point five", "Opus 5.5": "Opus five point five",
          "DeepSeek V4.1 Flash": "DeepSeek Vee four point one Flash", "GLM 5.3 Flash": "GLM five point three Flash",
          "Stockfish 19 (depth 4)": "Stockfish at depth four"}
COMMENTATOR_PROMPT = (
    "You are a lively, sharp chess commentator for a YouTube tournament between AI models. "
    "Write ONE spoken line of 15 to 35 words about the latest moves on this board: name who moved, what it means, "
    "and react to any move mark (?? blunder, ? mistake, ?! inaccuracy, ! strong move). You may quote the player's own "
    "reason in a few words. Plain words only: no markdown, no lists, no dashes, no emojis, no move numbers, "
    "write moves the way they are spoken (Knight takes e5, castles short). Do not invent moves or evaluations. "
    "When the notes say the commentary just moved to this board, start by naming the board (for example "
    "'Over on board three'). Mention the tournament standings only when the notes give them and it adds drama."
)
MARK_SCORE = {"??": 6.0, "?": 4.0, "?!": 1.5, "!": 3.0}
RECENT_RESULT_SECONDS = 180


def interest(game: dict, marks: dict, ranks: dict, done_ply: int, last_game: str | None, idle_seconds: float) -> float:
    """How much a board deserves the next line. Higher = more interesting right now."""
    score = 0.0
    for name in (game.get("white"), game.get("black")):
        rank = ranks.get(name)
        if rank:
            score += max(0, 4 - rank) * 1.5  # the top three are the story
    fresh = (game.get("moves") or [])[done_ply:]
    for move in fresh:
        san = move.get("san", "")
        score += MARK_SCORE.get(str(marks.get(str(move["ply"])) or ""), 0.0)
        score += 10.0 if "#" in san else 1.0 if "+" in san else 0.0
        score += 0.7 if "x" in san else 0.0
    score += min(len(fresh), 4) * 0.3
    clocks = game.get("clocks") or {}
    if game.get("status") == "live" and min(clocks.get("white", 10 ** 9), clocks.get("black", 10 ** 9)) < 60000:
        score += 3.0  # time trouble
    score += min(idle_seconds / 60.0, 3.0)  # rotate: boards nobody talked about lately climb
    if game.get("id") == last_game:
        score -= 2.0
    return score


def spoken(name: str) -> str:
    return SPOKEN.get(name, name)


def _key() -> str | None:
    value = os.environ.get("OPENROUTER_API_KEY")
    if value:
        return value
    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as handle:
                return str(winreg.QueryValueEx(handle, "OPENROUTER_API_KEY")[0])
        except OSError:
            return None
    return None


def build_context(game: dict, annotations: dict, from_ply: int, switched: bool = False, leaders: str = "") -> str:
    """The facts the commentator may use: players, the last few moves with marks and the movers' own comments."""
    moves = game.get("moves") or []
    lines = [f"Board {game.get('board', '?')}, round {game.get('round', '?')}."
             + (" The commentary just moved to this board." if switched else ""),
             f"White: {spoken(game.get('white', '?'))}. Black: {spoken(game.get('black', '?'))}.",
             f"Moves played so far: {len(moves)} half-moves."]
    if leaders:
        lines.append(f"Tournament standings now: {leaders}.")
    for move in moves[max(0, from_ply - 2):]:
        mover = spoken(game.get(move["side"], move["side"]))
        mark = (annotations or {}).get(str(move["ply"])) or ""
        reason = (move.get("comment") or "").strip()
        lines.append(f"- {mover} ({move['side']}) played {move['san']}{mark}"
                     + (f" ({mark} from Stockfish)" if mark else "")
                     + (f". Its own reason: {reason[:220]}" if reason else ""))
    if game.get("status") != "live" and game.get("termination"):
        lines.append(f"The game is over: {game.get('result')} - {game['termination']}.")
    return "\n".join(lines)


def clean_line(text: str) -> str:
    text = re.sub(r"\s*[–—]\s*", ", ", re.sub(r"[*_#`]", "", text or ""))
    return " ".join(text.split())[:400]


class Commentator:
    def __init__(self, state_path: Path, out_dir: Path, log=print, budget_usd: float | None = None) -> None:
        self.state_path = Path(state_path)
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.log = log
        self.budget_usd = float(os.environ.get("COMMENTARY_BUDGET_USD",
                                             DEFAULT_BUDGET_USD if budget_usd is None else budget_usd))
        self.ledger = self.out_dir / "commentary-ledger.json"
        saved = self._load_ledger()
        self.spent_usd = float(saved.get("spent_usd", 0.0))
        self._clips: dict[str, list[dict]] = saved.get("clips", {})
        self._seq = max([c["seq"] for clips in self._clips.values() for c in clips] or [0])
        self._done_ply: dict[str, int] = {g: max(c["ply"] for c in clips) for g, clips in self._clips.items() if clips}
        self._focus: str | None = None
        self._last_game: str | None = None
        self._last_poll = 0.0
        self.always = os.environ.get("COMMENTARY_ALWAYS", "").strip() in {"1", "true", "yes"}
        self._last_said: dict[str, float] = {}
        self._busy_until = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.http = self._http  # replaceable in tests

    # ------------------------------------------------------------ viewer API
    def start(self) -> None:
        if not _key():
            self.log("commentary: OPENROUTER_API_KEY is not set; commentary stays off")
            return
        threading.Thread(target=self._loop, daemon=True).start()
        self.log(f"commentary: on ({TEXT_MODEL} + {TTS_MODEL}, budget ${self.budget_usd:.2f}, spent ${self.spent_usd:.4f})")

    def stop(self) -> None:
        self._stop.set()

    def focus(self, game_id: str | None, pinned: bool = False) -> None:
        """A pinned board (viewer focus mode) keeps the commentary; None or an unpinned hint = roam."""
        with self._lock:
            if not game_id:
                self._focus = None
            elif pinned:
                self._focus = game_id

    def clips(self, game_id: str, after_seq: int = 0) -> list[dict]:
        self._last_poll = time.time()
        with self._lock:
            return [dict(c, game=game_id) for c in self._clips.get(game_id, []) if c["seq"] > after_seq]

    def clips_all(self, after_seq: int = 0) -> list[dict]:
        self._last_poll = time.time()
        with self._lock:
            found = [dict(c, game=g) for g, clips in self._clips.items() for c in clips if c["seq"] > after_seq]
        return sorted(found, key=lambda c: c["seq"])

    def audio_path(self, name: str) -> Path | None:
        if not re.fullmatch(r"clip-\d+\.wav", name or ""):
            return None
        path = self.out_dir / name
        return path if path.exists() else None

    # ------------------------------------------------------------ worker
    def _loop(self) -> None:
        while not self._stop.wait(1.0):
            try:
                self.tick()
            except Exception as exc:  # never take the viewer down
                self.log(f"commentary: {type(exc).__name__}: {exc}")
                time.sleep(5)

    def tick(self) -> dict | None:
        if self.spent_usd >= self.budget_usd or time.time() < self._busy_until:
            return None
        if not self.always and time.time() - self._last_poll > LISTENER_SECONDS:
            return None  # nobody is listening
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        games = state.get("games") or {}
        all_marks = self._annotations(state)
        game_id = self.pick(state, all_marks)
        if game_id is None:
            return None
        game = games[game_id]
        plies = len(game.get("moves") or [])
        done = self._done_ply.get(game_id, 0)
        finished = game.get("status") != "live" and game.get("result", "*") != "*"
        annotations = all_marks.get(game_id, {})
        top = [r for r in (state.get("standings") or []) if r.get("played")][:3]
        leaders = "; ".join(f"{spoken(r['name'])} {r['points']:g} point{'' if r['points'] == 1 else 's'}" for r in top)
        context = build_context(game, annotations, done + 1, switched=game_id != self._last_game, leaders=leaders)
        text = clean_line(self._write_line(context))
        if not text:
            return None
        pcm, cost = self._speak(text)
        with self._lock:
            self._seq += 1
            name = f"clip-{self._seq}.wav"
            with wave.open(str(self.out_dir / name), "wb") as out:
                out.setnchannels(1)
                out.setsampwidth(2)
                out.setframerate(SAMPLE_RATE)
                out.writeframes(pcm)
            seconds = len(pcm) / (2 * SAMPLE_RATE)
            clip = {"seq": self._seq, "ply": plies, "text": text, "audio": name, "seconds": round(seconds, 1),
                    "final": finished}
            self._clips.setdefault(game_id, []).append(clip)
            self._done_ply[game_id] = plies
            self._last_game = game_id
            self._last_said[game_id] = time.time()
            self.spent_usd += cost
            self._busy_until = time.time() + max(MIN_GAP_SECONDS, seconds + 1.0)
            self._save_ledger()
        self.log(f"commentary {game_id} ply {plies}: {text} (${cost:.4f}, total ${self.spent_usd:.4f})")
        return clip

    def pick(self, state: dict, all_marks: dict) -> str | None:
        """The board for the next line: the pinned one, else the most interesting board with something new."""
        games = state.get("games") or {}
        with self._lock:
            pinned = self._focus if self._focus in games else None
        ranks = {r["name"]: r.get("rank") for r in state.get("standings") or [] if r.get("played")}
        now = time.time()
        best, best_score = None, float("-inf")
        for game_id, game in games.items():
            if pinned and game_id != pinned:
                continue
            plies = len(game.get("moves") or [])
            done = self._done_ply.get(game_id, 0)
            finished = game.get("status") != "live" and game.get("result", "*") != "*"
            recent_end = finished and self._ended_recently(game, now)
            result_due = recent_end and not self._said_result(game_id)
            if not (game.get("status") == "live" and plies > done) and not result_due:
                continue
            idle = now - self._last_said.get(game_id, now - 180)
            score = interest(game, all_marks.get(game_id, {}), ranks, done, self._last_game, idle)
            if result_due:
                score += 8.0 if game.get("result") in ("1-0", "0-1") else 5.0
            if score > best_score:
                best, best_score = game_id, score
        return best

    @staticmethod
    def _ended_recently(game: dict, now: float) -> bool:
        try:
            import datetime as dt

            ended = dt.datetime.fromisoformat(str(game.get("end"))).timestamp()
        except (TypeError, ValueError):
            return True  # no end stamp: still worth one closing line
        return now - ended < RECENT_RESULT_SECONDS

    def _said_result(self, game_id: str) -> bool:
        return any(c.get("final") for c in self._clips.get(game_id, []))

    def _annotations(self, state: dict) -> dict:
        sidecar = self.state_path.with_name(f"{state.get('id', '')}-annotations.json")
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
            return data.get("annotations", data) if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_line(self, context: str) -> str:
        body = {"model": TEXT_MODEL, "max_tokens": 120, "temperature": 0.8,
                "messages": [{"role": "system", "content": COMMENTATOR_PROMPT}, {"role": "user", "content": context}]}
        data, _headers = self.http("/chat/completions", body)
        reply = json.loads(data)
        self.spent_usd += float((reply.get("usage") or {}).get("cost") or 0.0)
        return ((reply.get("choices") or [{}])[0].get("message") or {}).get("content") or ""

    def _speak(self, text: str) -> tuple[bytes, float]:
        # Only the line itself is spoken; the delivery style goes in `instructions` (2026-10-06: a style prefix
        # inside `input` was sometimes read aloud, "say it like an excited chess commentator...").
        body = {"model": TTS_MODEL, "input": text, "instructions": TTS_STYLE, "voice": TTS_VOICE, "response_format": "pcm"}
        pcm, headers = self.http("/audio/speech", body)
        # The speech endpoint returns raw audio; its cost is read back from the generation record.
        return pcm, self._generation_cost(headers.get("X-Generation-Id") or headers.get("x-generation-id"))

    def _generation_cost(self, generation_id: str | None) -> float:
        if not generation_id:
            return 0.004  # conservative per-line estimate when the record is missing
        for _ in range(3):
            time.sleep(1.5)
            try:
                data, _ = self.http(f"/generation?id={generation_id}", None)
                return float(json.loads(data)["data"].get("total_cost") or 0.0)
            except Exception:
                continue
        return 0.004

    def _http(self, path: str, body: dict | None) -> tuple[bytes, dict]:
        headers = {"Authorization": f"Bearer {_key()}", "X-Title": "ai-chess-harness commentary"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(API + path, data=data, headers=headers, method="POST" if body is not None else "GET")
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.read(), dict(response.headers.items())

    def _load_ledger(self) -> dict:
        try:
            return json.loads(self.ledger.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_ledger(self) -> None:
        self.ledger.write_text(json.dumps({"spent_usd": round(self.spent_usd, 6), "clips": self._clips}, indent=1),
                               encoding="utf-8")
