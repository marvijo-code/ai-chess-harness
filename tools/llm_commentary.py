"""Spoken commentary for the live LLM tournament viewer (OpenRouter, metered).

A background thread watches the tournament state JSON. For the board the viewer focuses (or the
first live board) it writes one short commentator line every MIN_GAP_SECONDS at most, from the
recent moves, the players' own move comments and the Stockfish move marks, then voices it with
an OpenRouter speech model. Clips are WAV files in out_dir; the viewer polls `clips()` and plays
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
TTS_STYLE = "Say like an excited but clear chess commentator"
SAMPLE_RATE = 24000
MIN_GAP_SECONDS = 12.0
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
    "write moves the way they are spoken (Knight takes e5, castles short). Do not invent moves or evaluations."
)


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


def build_context(game: dict, annotations: dict, from_ply: int) -> str:
    """The facts the commentator may use: players, the last few moves with marks and the movers' own comments."""
    moves = game.get("moves") or []
    lines = [f"White: {spoken(game.get('white', '?'))}. Black: {spoken(game.get('black', '?'))}.",
             f"Moves played so far: {len(moves)} half-moves."]
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

    def focus(self, game_id: str | None) -> None:
        with self._lock:
            self._focus = game_id or None

    def clips(self, game_id: str, after_seq: int = 0) -> list[dict]:
        with self._lock:
            return [dict(c) for c in self._clips.get(game_id, []) if c["seq"] > after_seq]

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
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        games = state.get("games") or {}
        with self._lock:
            game_id = self._focus if self._focus in games else None
        if game_id is None:
            live = [g for g in games.values() if g.get("status") == "live"]
            game_id = live[0]["id"] if live else None
        if game_id is None:
            return None
        game = games[game_id]
        plies = len(game.get("moves") or [])
        done = self._done_ply.get(game_id, 0)
        finished = game.get("status") != "live" and game.get("result", "*") != "*"
        if plies <= done and not (finished and done >= 0 and not self._said_result(game_id)):
            return None
        annotations = self._annotations(state).get(game_id, {})
        context = build_context(game, annotations, done + 1)
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
            self.spent_usd += cost
            self._busy_until = time.time() + max(MIN_GAP_SECONDS, seconds + 1.0)
            self._save_ledger()
        self.log(f"commentary {game_id} ply {plies}: {text} (${cost:.4f}, total ${self.spent_usd:.4f})")
        return clip

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
        body = {"model": TTS_MODEL, "input": f"{TTS_STYLE}: {text}", "voice": TTS_VOICE, "response_format": "pcm"}
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
