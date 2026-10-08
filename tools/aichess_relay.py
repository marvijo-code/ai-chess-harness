#!/usr/bin/env python3
"""Public read-only mirror of the local AI-chess tournament viewer (runs on the VPS).

The laptop pusher (tools/aichess_push.py) posts the viewer's live data to the INGEST
listener (127.0.0.1 only, token required). The PUBLIC listener serves the same GET API
shapes as tools/llm_tournament_viewer.py, so a hosted page (marvijo.com/ai-chess, through
its server-side proxy) can show the tournament live.

  PUBLIC  (0.0.0.0:8780, GET/HEAD only)
    /api/tournament[?since=<updated_epoch_ms>]   latest state (gzip when accepted)
    /api/commentary?after=N[&game=ID]           commentary clip metadata
    /api/commentary/audio/clip-N.wav            clip audio (immutable)
    /api/thinking?game=&ply=&since=             thinking tail of one move
    /api/analyze                                {"enabled": false}
    /api/viewer-version                         {"version": ...}
    /healthz                                    relay health numbers
  INGEST  (127.0.0.1:8781, POST, header X-Ingest-Token)
    /ingest/state  /ingest/clip-audio/clip-N.wav  /ingest/clips  /ingest/thinking  /ingest/version
    GET /healthz (same numbers, so the pusher can ask through its ssh tunnel)

Python standard library only. All time math uses this machine's clock: the laptop clock
is never compared with it.
"""

from __future__ import annotations

import argparse
import gzip
import hmac
import json
import os
import re
import shutil
import struct
import sys
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

CLIP_NAME = re.compile(r"clip-\d{1,9}\.wav")
SAFE_ID = re.compile(r"[A-Za-z0-9-]{1,80}")
MAX_BODY = 12 * 1024 * 1024          # any ingest body
MAX_WAV = 4 * 1024 * 1024            # one clip
MAX_STATE_JSON = 64 * 1024 * 1024    # decompressed state (gzip bomb guard)
CLIPS_DISK_CAP = 400 * 1024 * 1024   # oldest WAVs are pruned beyond this
MAX_CLIP_META = 5000                 # clip metadata entries kept
CLIP_IDENTITY = ("game", "ply", "text", "audio")   # a resend of a held seq matches on these
THINK_TAIL_MAX = 64 * 1024           # bytes kept per game/ply
THINK_PLIES_PER_GAME = 40            # newest plies kept per game
THINK_TOTAL_CAP = 100 * 1024 * 1024  # all thinking tails together
THINK_CHUNK = 60_000                 # same read limit as the local viewer
THINK_MAX_PLY = 1000
VOLATILE = ("state_age_s", "server_now_ms", "hosted", "relay_received_epoch_ms")
LOG_ROTATE_BYTES = 20 * 1024 * 1024


# ---------------------------------------------------------------- helpers

def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with open(tmp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def strip_key(obj, key: str):
    """Remove `key` from every dict inside obj (in place)."""
    if isinstance(obj, dict):
        obj.pop(key, None)
        for value in obj.values():
            strip_key(value, key)
    elif isinstance(obj, list):
        for value in obj:
            strip_key(value, key)
    return obj


def bounded_gunzip(data: bytes, limit: int) -> bytes:
    inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = inflater.decompress(data, limit + 1)
    if len(out) > limit or inflater.unconsumed_tail:
        raise ValueError("decompressed body too large")
    return out


def is_wav(data: bytes) -> bool:
    return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE"


def clip_seq(name: str) -> int:
    return int(name[5:-4])


def gzip_member(deflate: bytes, crc: int, size: int) -> bytes:
    header = b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\xff"
    return header + deflate + struct.pack("<II", crc & 0xFFFFFFFF, size & 0xFFFFFFFF)


def read_tail(base: int, data: bytes, since: int, limit: int = THINK_CHUNK) -> dict:
    """read_thinking() of the local viewer, answered from a stored tail.

    The tail holds the file bytes [base, base + len(data)). A reader asking for bytes
    before `base` gets what is kept, flagged truncated.
    """
    size = base + len(data)
    start = max(0, min(since, size))
    if since > size:                    # the file was replaced: start over
        start = 0
    truncated = size - start > limit
    if truncated:
        start = size - limit
    if start < base:
        start, truncated = base, True
    raw = data[start - base:]
    if truncated:                       # do not start inside a multi-byte character
        skip = 0
        while skip < min(3, len(raw)) and 0x80 <= raw[skip] <= 0xBF:
            skip += 1
        raw, start = raw[skip:], start + skip
    end = len(raw)
    for back in range(1, min(4, len(raw)) + 1):   # drop an unfinished trailing character
        byte = raw[-back]
        if byte < 0x80:
            break
        if byte >= 0xC0:
            need = 2 if byte < 0xE0 else 3 if byte < 0xF0 else 4
            if back < need:
                end = len(raw) - back
            break
    raw = raw[:end]
    return {"exists": True, "size": start + len(raw), "text": raw.decode("utf-8", errors="replace"),
            "from": start, "truncated": truncated}


class Log:
    def __init__(self, path: Path | None = None):
        self.path = path
        self.lock = threading.Lock()

    def __call__(self, message: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
        with self.lock:
            if self.path is None:
                print(line, flush=True)
                return
            try:
                if self.path.exists() and self.path.stat().st_size > LOG_ROTATE_BYTES:
                    os.replace(self.path, self.path.with_name(self.path.name + ".1"))
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError:
                print(line, flush=True)


# ---------------------------------------------------------------- store

class Store:
    """Everything the relay keeps, in memory and in data_dir. Thread-safe."""

    def __init__(self, data_dir: Path, clock=time.time, log=print,
                 clips_cap: int = CLIPS_DISK_CAP, think_cap: int = THINK_TOTAL_CAP):
        self.dir = Path(data_dir)
        self.clock = clock
        self.log = log
        self.clips_cap = clips_cap
        self.think_cap = think_cap
        self.lock = threading.RLock()
        self.state: dict | None = None      # {tail, deflate, crc, length, updated, received, age, id, gz_len}
        self.clips: dict[int, dict] = {}    # seq -> clip (+ _received, _age)
        self.wavs: dict[str, int] = {}      # name -> bytes
        self.thinking: dict[str, dict[int, dict]] = {}   # game -> ply -> {base, data, received}
        self.version = "relay"
        (self.dir / "clips").mkdir(parents=True, exist_ok=True)
        (self.dir / "thinking").mkdir(parents=True, exist_ok=True)
        self._load()

    # ----- loading

    def _load(self) -> None:
        state_file = self.dir / "state.json.gz"
        meta_file = self.dir / "state.meta.json"
        if state_file.exists() and meta_file.exists():
            try:
                body = gzip.decompress(state_file.read_bytes())
                meta = json.loads(meta_file.read_text("utf-8"))
                self._set_state(body, float(meta["received"]), float(meta["age"]), persist=False)
            except (OSError, ValueError, KeyError) as exc:
                self.log(f"state reload failed: {exc}")
        try:
            for clip in json.loads((self.dir / "clips.json").read_text("utf-8")):
                if isinstance(clip, dict) and isinstance(clip.get("seq"), int):
                    self.clips[clip["seq"]] = clip
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            self.log(f"clips reload failed: {exc}")
        for wav in (self.dir / "clips").iterdir():
            if CLIP_NAME.fullmatch(wav.name):
                self.wavs[wav.name] = wav.stat().st_size
        for game_dir in (self.dir / "thinking").iterdir():
            if not (game_dir.is_dir() and SAFE_ID.fullmatch(game_dir.name)):
                continue
            for item in game_dir.iterdir():
                if not re.fullmatch(r"\d{1,4}\.json", item.name):
                    continue
                try:
                    rec = json.loads(item.read_text("utf-8"))
                    self.thinking.setdefault(game_dir.name, {})[int(item.stem)] = {
                        "base": int(rec["from"]), "data": rec["text"].encode("utf-8"),
                        "received": float(rec["received"])}
                except (OSError, ValueError, KeyError):
                    continue
        try:
            self.version = json.loads((self.dir / "version.json").read_text("utf-8"))["version"]
        except (OSError, ValueError, KeyError):
            pass

    # ----- state

    def _set_state(self, body: bytes, received: float, age: float, persist: bool = True) -> None:
        obj = json.loads(body)
        tail = body[1:]
        compressor = zlib.compressobj(6, zlib.DEFLATED, -15)
        deflate = compressor.compress(tail) + compressor.flush(zlib.Z_FINISH)
        entry = {"tail": tail, "deflate": deflate, "length": len(body), "updated": obj.get("updated_epoch_ms"),
                 "received": received, "age": age, "id": obj.get("id"), "gz_len": len(deflate) + 18}
        with self.lock:
            self.state = entry              # serve from memory even when the disk write fails
        if persist:
            try:
                atomic_write(self.dir / "state.json.gz", gzip.compress(body, 1))
                atomic_write(self.dir / "state.meta.json", json.dumps({"received": received, "age": age}).encode())
            except OSError as exc:
                self.log(f"state persist failed (kept in memory): {exc}")

    def ingest_state(self, raw: bytes) -> dict:
        if raw[:2] == b"\x1f\x8b":
            raw = bounded_gunzip(raw, MAX_STATE_JSON)
        obj = json.loads(raw)
        if not isinstance(obj, dict) or not obj.get("id"):
            raise ValueError("state payload has no id")
        age = obj.get("state_age_s")
        age = float(age) if isinstance(age, (int, float)) and age >= 0 else 0.0
        for key in VOLATILE:
            obj.pop(key, None)
        strip_key(obj, "pgn_path")
        body = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        self._set_state(body, self.clock(), age)
        return {"ok": True, "id": obj.get("id"), "updated_epoch_ms": obj.get("updated_epoch_ms"), "bytes": len(body)}

    def state_age(self) -> float | None:
        st = self.state
        if st is None:
            return None
        return round(st["age"] + max(0.0, self.clock() - st["received"]), 1)

    def state_response(self, since: str | None, want_gzip: bool) -> tuple[int, bytes, bool]:
        """(status, body, gzipped)."""
        st = self.state
        now = self.clock()
        if st is None:
            return 404, b'{"error":"no tournament state yet"}', False
        age = round(st["age"] + max(0.0, now - st["received"]), 1)
        if since is not None and since == str(st["updated"]):
            return 200, json.dumps({"unchanged": True, "state_age_s": age, "server_now_ms": int(now * 1000),
                                    "hosted": True}).encode(), False
        head = {"state_age_s": age, "server_now_ms": int(now * 1000), "hosted": True,
                "relay_received_epoch_ms": int(st["received"] * 1000)}
        prefix = json.dumps(head, separators=(",", ":")).encode()[:-1]
        if st["tail"] != b"}":
            prefix += b","
        if not want_gzip:
            return 200, prefix + st["tail"], False
        # Splice: the prefix ends on a byte boundary (sync flush), the cached tail stream follows.
        compressor = zlib.compressobj(6, zlib.DEFLATED, -15)
        lead = compressor.compress(prefix) + compressor.flush(zlib.Z_SYNC_FLUSH)
        crc = zlib.crc32(st["tail"], zlib.crc32(prefix))
        return 200, gzip_member(lead + st["deflate"], crc, len(prefix) + len(st["tail"])), True

    # ----- clips

    def ingest_clip_audio(self, name: str, data: bytes) -> dict:
        if not CLIP_NAME.fullmatch(name):
            raise ValueError("bad clip name")
        if len(data) > MAX_WAV:
            raise ValueError("clip too large")
        if not is_wav(data):
            raise ValueError("not a WAV file")
        atomic_write(self.dir / "clips" / name, data)
        with self.lock:
            self.wavs[name] = len(data)
            pruned = self._prune_wavs()
        return {"ok": True, "name": name, "bytes": len(data), "pruned": pruned}

    def _prune_wavs(self) -> int:
        pruned = 0
        total = sum(self.wavs.values())
        for name in sorted(self.wavs, key=clip_seq):
            if total <= self.clips_cap:
                break
            try:
                (self.dir / "clips" / name).unlink()
            except FileNotFoundError:
                pass
            total -= self.wavs.pop(name)
            pruned += 1
        return pruned

    def ingest_clips(self, clips) -> dict:
        if not isinstance(clips, list):
            raise ValueError("clips must be a JSON list")
        now = self.clock()
        added = 0
        with self.lock:
            for clip in clips:
                if not isinstance(clip, dict) or not isinstance(clip.get("seq"), int) or isinstance(clip.get("seq"), bool):
                    raise ValueError("each clip needs an integer seq")
                audio = clip.get("audio")
                if audio and not (isinstance(audio, str) and CLIP_NAME.fullmatch(audio)):
                    raise ValueError("bad clip audio name")
                tid = clip.get("tournament_id")
                if tid is not None and not (isinstance(tid, str) and 0 < len(tid) <= 200):
                    raise ValueError("bad clip tournament_id")
                held_tid = self._clips_tid()
                if tid and held_tid and tid != held_tid:
                    self.clips.clear()      # clips of a new tournament: the old ones are stale
                have = self.clips.get(clip["seq"])
                if have is not None:
                    if all(have.get(k) == clip.get(k) for k in CLIP_IDENTITY):
                        continue            # idempotent by tournament id + seq
                    # Same seq, other clip: the source renumbered (a fresh viewer starts at 1 again),
                    # so everything held from this seq up belongs to the old numbering.
                    for seq in [s for s in self.clips if s >= clip["seq"]]:
                        del self.clips[seq]
                age = clip.get("age_s")
                entry = {k: v for k, v in clip.items() if not k.startswith("_")}
                entry["_received"] = now
                entry["_age"] = float(age) if isinstance(age, (int, float)) else 0.0
                self.clips[clip["seq"]] = entry
                added += 1
            for seq in sorted(self.clips)[:-MAX_CLIP_META]:
                del self.clips[seq]
            if added:
                data = json.dumps([self.clips[s] for s in sorted(self.clips)], ensure_ascii=False).encode("utf-8")
                try:
                    atomic_write(self.dir / "clips.json", data)
                except OSError as exc:
                    self.log(f"clips persist failed (kept in memory): {exc}")
            last = max(self.clips) if self.clips else 0
        return {"ok": True, "added": added, "last_clip_seq": last}

    def _clips_tid(self) -> str | None:
        """Tournament of the clips held (the newest clip's tag; None for untagged or no clips)."""
        return self.clips[max(self.clips)].get("tournament_id") if self.clips else None

    def clips_after(self, after: int, game: str = "") -> list[dict]:
        now = self.clock()
        with self.lock:
            chosen = [self.clips[s] for s in sorted(self.clips) if s > after]
        out = []
        for clip in chosen:
            if game and clip.get("game") != game:
                continue
            item = {k: v for k, v in clip.items() if not k.startswith("_")}
            item["age_s"] = round(clip["_age"] + max(0.0, now - clip["_received"]), 1)
            out.append(item)
        return out

    def wav_path(self, name: str) -> Path | None:
        if not CLIP_NAME.fullmatch(name):
            return None
        with self.lock:
            known = name in self.wavs
        path = self.dir / "clips" / name
        return path if known and path.is_file() else None

    # ----- thinking

    def ingest_thinking(self, rec) -> dict:
        if not isinstance(rec, dict):
            raise ValueError("thinking must be a JSON object")
        game, ply, text, base = rec.get("game"), rec.get("ply"), rec.get("text"), rec.get("from", 0)
        if not (isinstance(game, str) and SAFE_ID.fullmatch(game)):
            raise ValueError("bad game id")
        if not (isinstance(ply, int) and 1 <= ply <= THINK_MAX_PLY):
            raise ValueError("bad ply")
        if not isinstance(text, str) or not isinstance(base, int) or base < 0:
            raise ValueError("bad text or from")
        data = text.encode("utf-8")
        if len(data) > THINK_TAIL_MAX:      # keep the newest 64 KB, never mid-character
            cut = len(data) - THINK_TAIL_MAX
            while cut < len(data) and 0x80 <= data[cut] <= 0xBF:
                cut += 1
            data, base = data[cut:], base + cut
        now = self.clock()
        stored = {"base": base, "data": data, "received": now}
        try:
            atomic_write(self.dir / "thinking" / game / f"{ply}.json",
                         json.dumps({"from": base, "text": data.decode("utf-8", errors="replace"), "received": now},
                                    ensure_ascii=False).encode("utf-8"))
        except OSError as exc:
            self.log(f"thinking persist failed (kept in memory): {exc}")
        with self.lock:
            plies = self.thinking.setdefault(game, {})
            plies[ply] = stored
            for old in sorted(plies)[:-THINK_PLIES_PER_GAME]:
                self._drop_thinking(game, old)
            self._prune_thinking()
        return {"ok": True, "game": game, "ply": ply, "size": base + len(data)}

    def _drop_thinking(self, game: str, ply: int) -> None:
        self.thinking.get(game, {}).pop(ply, None)
        try:
            (self.dir / "thinking" / game / f"{ply}.json").unlink()
        except FileNotFoundError:
            pass
        if game in self.thinking and not self.thinking[game]:
            del self.thinking[game]
            try:
                (self.dir / "thinking" / game).rmdir()
            except OSError:
                pass

    def thinking_bytes(self) -> int:
        with self.lock:
            return sum(len(r["data"]) for plies in self.thinking.values() for r in plies.values())

    def _prune_thinking(self) -> None:
        total = self.thinking_bytes()
        if total <= self.think_cap:
            return
        order = sorted(((r["received"], g, p) for g, plies in self.thinking.items() for p, r in plies.items()))
        for _, game, ply in order:
            if total <= self.think_cap:
                break
            total -= len(self.thinking[game][ply]["data"])
            self._drop_thinking(game, ply)

    def read_thinking(self, game: str, ply: int, since: int) -> dict:
        with self.lock:
            rec = self.thinking.get(game, {}).get(ply)
        if rec is None:
            return {"exists": False, "size": 0, "text": "", "from": 0, "truncated": False}
        return read_tail(rec["base"], rec["data"], since)

    # ----- version and health

    def set_version(self, version) -> dict:
        if not isinstance(version, (str, int, float)) or len(str(version)) > 100:
            raise ValueError("bad version")
        self.version = version
        atomic_write(self.dir / "version.json", json.dumps({"version": version}).encode())
        return {"ok": True, "version": version}

    def health(self) -> dict:
        st = self.state
        with self.lock:
            last = max(self.clips) if self.clips else 0
            n = len(self.clips)
            clips_tid = self._clips_tid()
            wav_bytes = sum(self.wavs.values())
        used = wav_bytes + self.thinking_bytes() + (st["gz_len"] if st else 0)
        try:
            free = shutil.disk_usage(self.dir).free // (1024 * 1024)
        except OSError:
            free = None
        return {"ok": True, "state_age_s": self.state_age(), "state_updated_epoch_ms": st["updated"] if st else None,
                "tournament_id": st["id"] if st else None, "last_clip_seq": last, "clips": n,
                "clips_tournament_id": clips_tid, "clip_wavs": len(self.wavs), "disk_mb": round(used / (1024 * 1024), 1), "disk_free_mb": free}


# ---------------------------------------------------------------- HTTP

class BaseHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 30
    store: Store
    log = staticmethod(print)

    def log_message(self, fmt, *args) -> None:  # quiet console
        pass

    def _send(self, code: int, body: bytes, kind: str, headers: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        if not headers or "Cache-Control" not in headers:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload, code: int = 200) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json")

    def _not_found(self) -> None:
        self._send(404, b"not found", "text/plain")


class PublicHandler(BaseHandler):
    def do_GET(self) -> None:  # noqa: N802
        try:
            self._route()
        except (BrokenPipeError, ConnectionResetError):
            pass

    do_HEAD = do_GET

    def do_POST(self) -> None:  # noqa: N802
        self.close_connection = True
        self._not_found()

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_POST

    def _route(self) -> None:
        url = urlparse(self.path)
        query = parse_qs(url.query)
        path = url.path
        store = self.store
        if path == "/api/tournament":
            since = (query.get("since") or [None])[0]
            gz_ok = "gzip" in (self.headers.get("Accept-Encoding") or "").lower()
            code, body, gz = store.state_response(since, gz_ok)
            headers = {"Vary": "Accept-Encoding"}
            if gz:
                headers["Content-Encoding"] = "gzip"
            self._send(code, body, "application/json", headers)
        elif path == "/api/commentary":
            try:
                after = int((query.get("after") or ["0"])[0] or 0)
            except ValueError:
                after = 0
            game = (query.get("game") or [""])[0]
            if game and not SAFE_ID.fullmatch(game):
                self._json({"enabled": True, "clips": [], "error": "bad game id"}, 400)
                return
            self._json({"enabled": True, "clips": store.clips_after(after, game), "quiet_s": 0, "held": False})
        elif path.startswith("/api/commentary/audio/"):
            name = unquote(path[len("/api/commentary/audio/"):])
            wav = store.wav_path(name)
            if wav is None:
                self._not_found()
                return
            try:
                data = wav.read_bytes()
            except OSError:
                self._not_found()
                return
            self._send(200, data, "audio/wav", {"Cache-Control": "public, max-age=86400, immutable"})
        elif path == "/api/thinking":
            game = (query.get("game") or [""])[0]
            try:
                ply = int((query.get("ply") or [""])[0])
                since = int((query.get("since") or ["0"])[0] or 0)
            except ValueError:
                self._json({"error": "ply and since must be whole numbers"}, 400)
                return
            wanted = (query.get("id") or [""])[0]
            if not SAFE_ID.fullmatch(game) or (wanted and not SAFE_ID.fullmatch(wanted)) or not 1 <= ply <= THINK_MAX_PLY or since < 0:
                self._json({"error": "bad game, id, ply or since"}, 400)
                return
            self._json(store.read_thinking(game, ply, since))
        elif path == "/api/analyze":
            self._json({"enabled": False})
        elif path == "/api/viewer-version":
            self._json({"version": store.version})
        elif path == "/healthz":
            self._json(store.health())
        else:
            self._not_found()


class IngestHandler(BaseHandler):
    token: bytes = b""

    def do_GET(self) -> None:  # noqa: N802
        if urlparse(self.path).path == "/healthz":
            self._json(self.store.health())
        else:
            self._not_found()

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        given = (self.headers.get("X-Ingest-Token") or "").encode("utf-8")
        if not self.token or not hmac.compare_digest(given, self.token):
            self.close_connection = True
            self._json({"error": "bad token"}, 403)
            self.log(f"ingest {path} refused: bad token")
            return
        try:
            length = int(self.headers.get("Content-Length") or -1)
        except ValueError:
            length = -1
        if length < 0:
            self.close_connection = True
            self._json({"error": "Content-Length required"}, 411)
            return
        if length > MAX_BODY:
            self.close_connection = True
            self._json({"error": "body too large"}, 413)
            self.log(f"ingest {path} refused: {length} bytes")
            return
        body = self.rfile.read(length)
        if (self.headers.get("Content-Encoding") or "").lower() == "gzip" and body[:2] != b"\x1f\x8b":
            self._json({"error": "body is not gzip"}, 400)
            return
        store = self.store
        try:
            if path == "/ingest/state":
                result = store.ingest_state(body)
            elif path.startswith("/ingest/clip-audio/"):
                result = store.ingest_clip_audio(unquote(path[len("/ingest/clip-audio/"):]), body)
            elif path == "/ingest/clips":
                result = store.ingest_clips(json.loads(self._plain(body)))
            elif path == "/ingest/thinking":
                result = store.ingest_thinking(json.loads(self._plain(body)))
            elif path == "/ingest/version":
                result = store.set_version((json.loads(self._plain(body)) or {}).get("version"))
            else:
                self._not_found()
                return
        except (ValueError, TypeError, AttributeError) as exc:
            self._json({"error": str(exc)}, 400)
            self.log(f"ingest {path} {length}B rejected: {exc}")
            return
        except OSError as exc:
            self._json({"error": "storage error"}, 500)
            self.log(f"ingest {path} {length}B storage error: {exc}")
            return
        self._json(result)
        brief = {k: v for k, v in result.items() if k in ("id", "updated_epoch_ms", "name", "added", "last_clip_seq", "game", "ply", "size", "pruned", "version")}
        self.log(f"ingest {path} {length}B ok {json.dumps(brief)}")

    @staticmethod
    def _plain(body: bytes) -> bytes:
        return bounded_gunzip(body, MAX_BODY) if body[:2] == b"\x1f\x8b" else body


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64


def make_servers(store: Store, token: bytes, public_host: str, public_port: int, ingest_port: int, log=print):
    public = type("Public", (PublicHandler,), {"store": store, "log": staticmethod(log)})
    ingest = type("Ingest", (IngestHandler,), {"store": store, "token": token, "log": staticmethod(log)})
    pub = Server((public_host, public_port), public)
    ing = Server(("127.0.0.1", ingest_port), ingest)   # never exposed
    return pub, ing


def read_token(path: Path) -> bytes:
    try:
        token = path.read_text("utf-8").strip()
    except OSError as exc:
        raise SystemExit(f"token file unreadable: {path} ({exc.__class__.__name__})")
    if len(token) < 16:
        raise SystemExit(f"token file {path} is empty or too short")
    return token.encode("utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--public-host", default="0.0.0.0")
    parser.add_argument("--public-port", type=int, default=8780)
    parser.add_argument("--ingest-port", type=int, default=8781)
    parser.add_argument("--data-dir", type=Path, default=Path("~/acl-chess-relay/data"))
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--log-file", type=Path, help="append log lines here (rotated at 20 MB); default stdout")
    args = parser.parse_args(argv)
    token = read_token(args.token_file.expanduser())
    log = Log(args.log_file.expanduser() if args.log_file else None)
    store = Store(args.data_dir.expanduser(), log=log)
    pub, ing = make_servers(store, token, args.public_host, args.public_port, args.ingest_port, log)
    threading.Thread(target=ing.serve_forever, daemon=True).start()
    health = store.health()
    log(f"relay up: public {args.public_host}:{args.public_port}, ingest 127.0.0.1:{args.ingest_port}, "
        f"data {store.dir}, clips {health['clips']}, state {health['tournament_id']}")
    try:
        pub.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
