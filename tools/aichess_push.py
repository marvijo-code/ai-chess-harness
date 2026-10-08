#!/usr/bin/env python3
"""Push the local AI-chess tournament viewer's live data to the public relay.

Runs on the laptop next to tools/llm_tournament_viewer.py. Every ~1 s it reads the local
viewer and posts what changed to the relay's ingest listener (tools/aichess_relay.py),
reached through an ssh forward tunnel (run-aichess-push.ps1 starts both):

  * /api/tournament  when it changed (state_age_s and server_now_ms ignored), gzipped,
                     at most once every 1.5 s;
  * commentary clips audio first, then metadata. On a (re)start only the newest 20
                     clips are sent, never the whole archive. The clip cursor follows the
                     relay's /healthz (read on start and every 30 s), never the status file,
                     and drops when the local commentary renumbered from 1;
  * eval_track       added to that state: the viewer's Stockfish score and best move for every
                     position of every game, so the public page shows the engine on replays and
                     finished boards too (read from <live-dir>/<id>-annotations.json);
  * thinking tails   of every live board's current ply when the size changed, at most
                     once every 2 s per board;
  * viewer version   when it changed.

It never crashes and never blocks the viewer: both ends may be down, it retries with
backoff. Every 2 s it writes <live-dir>/<tournament id>-publish-status.json for the
local viewer's "published" chip. Python standard library only. The token is read from a
file and never printed.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

VOLATILE = ("state_age_s", "server_now_ms")
STATE_MIN_GAP = 1.5
THINK_MIN_GAP = 2.0
CLIP_CATCHUP_LIMIT = 20
CLIPS_PER_CYCLE = 4
STATUS_EVERY = 2.0
VERSION_EVERY = 60.0
AUDIO_MISSING_TRIES = 3
HEALTH_EVERY = 30.0          # re-read the relay's last_clip_seq (it may have been reset)
LOCAL_PROBE_EVERY = 15.0     # when idle, check the local commentary did not renumber from 1

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# ---------------------------------------------------------------- pure logic

def state_fingerprint(state: dict) -> str:
    """Hash of the state without the fields that change on every read."""
    stable = {k: v for k, v in state.items() if k not in VOLATILE}
    return hashlib.sha256(json.dumps(stable, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def select_new_clips(clips: list[dict], last_seq: int | None, limit: int = CLIP_CATCHUP_LIMIT) -> list[dict]:
    """Clips to push, oldest first: newer than last_seq, never more than the newest `limit`.

    last_seq None (first start, nothing known) or a long gap both send only the newest `limit`.
    """
    floor = -1 if last_seq is None else last_seq
    fresh = sorted((c for c in clips if isinstance(c.get("seq"), int) and c["seq"] > floor), key=lambda c: c["seq"])
    return fresh[-limit:] if limit else fresh


def live_boards(state: dict) -> dict[str, int]:
    """game id -> the ply being thought about now (plies played + 1) for every live game."""
    out = {}
    for gid, game in (state.get("games") or {}).items():
        if isinstance(game, dict) and game.get("status") == "live":
            try:
                out[gid] = int(game.get("plies") or len(game.get("moves") or [])) + 1
            except (TypeError, ValueError):
                continue
    return out


class EvalTrack:
    """Stockfish scores per ply for every game, from the viewer's annotations sidecar.

    track[game] = {"depth", "cp": [White-side score after i plies], "best": [best move in SAN]}.
    The sidecar scores positions from the side to move; this turns them into White's side. Scores
    that are not known yet stay null and are filled in later. python-chess is optional: without it
    (or without the sidecar) the state is pushed as it is.
    """

    def __init__(self):
        self.games: dict[str, dict] = {}
        self.sidecar_mtime: float | None = None
        self.positions: dict[str, dict] = {}
        self.depth = 0
        self.tid: str | None = None

    def _load(self, path: Path) -> None:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return
        if mtime == self.sidecar_mtime:
            return
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return                       # half-written: keep what we have
        self.positions = saved.get("positions") or {}
        self.depth = int(saved.get("depth") or 0)
        self.sidecar_mtime = mtime

    def build(self, state: dict, sidecar: Path) -> dict[str, dict]:
        try:
            import chess
        except ImportError:
            return {}
        if state.get("id") != self.tid:
            self.tid, self.games, self.sidecar_mtime, self.positions = state.get("id"), {}, None, {}
        self._load(sidecar)
        if not self.positions:
            return {}
        out: dict[str, dict] = {}
        for gid, game in (state.get("games") or {}).items():
            ucis = [m.get("uci") for m in (game.get("moves") or []) if isinstance(m, dict) and m.get("uci")]
            have = self.games.get(gid)
            if have is None or have["ucis"] != ucis[: len(have["ucis"])]:
                board = chess.Board()
                have = {"ucis": [], "fens": [board.fen()], "turns": [True], "board": board, "cp": [None], "best": [""]}
                self.games[gid] = have
            for uci in ucis[len(have["ucis"]):]:
                try:
                    have["board"].push_uci(uci)
                except ValueError:
                    break
                have["ucis"].append(uci)
                have["fens"].append(have["board"].fen())
                have["turns"].append(have["board"].turn == chess.WHITE)
                have["cp"].append(None)
                have["best"].append("")
            for i, fen in enumerate(have["fens"]):
                if have["cp"][i] is not None:
                    continue
                rec = self.positions.get(fen)
                if not rec or rec.get("cp") is None:
                    continue
                cp = int(rec["cp"])
                have["cp"][i] = cp if have["turns"][i] else -cp
                if rec.get("best"):
                    try:
                        have["best"][i] = chess.Board(fen).san(chess.Move.from_uci(rec["best"]))
                    except ValueError:
                        pass
            out[gid] = {"depth": self.depth, "cp": list(have["cp"]), "best": list(have["best"])}
        return out


class Backoff:
    """Skip an endpoint after failures: 1, 2, 4 ... max_delay seconds; reset on success."""

    def __init__(self, clock=time.monotonic, first: float = 1.0, max_delay: float = 30.0):
        self.clock = clock
        self.first = first
        self.max_delay = max_delay
        self.delay = 0.0
        self.until = 0.0
        self.failures = 0

    def ready(self) -> bool:
        return self.clock() >= self.until

    def fail(self) -> None:
        self.failures += 1
        self.delay = self.first if self.delay == 0 else min(self.max_delay, self.delay * 2)
        self.until = self.clock() + self.delay

    def ok(self) -> None:
        self.failures = 0
        self.delay = 0.0
        self.until = 0.0


# ---------------------------------------------------------------- HTTP

class Http:
    """Tiny urllib wrapper so tests can swap it for a fake."""

    def get(self, url: str, timeout: float = 10) -> tuple[int, bytes]:
        req = urllib.request.Request(url, headers={"Accept-Encoding": "identity"})
        try:
            with _OPENER.open(req, timeout=timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read() if exc.fp else b""

    def post(self, url: str, body: bytes, headers: dict, timeout: float = 30) -> tuple[int, bytes]:
        req = urllib.request.Request(url, data=body, method="POST", headers=headers)
        try:
            with _OPENER.open(req, timeout=timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read() if exc.fp else b""


class EndpointDown(Exception):
    pass


# ---------------------------------------------------------------- pusher

class Pusher:
    def __init__(self, local: str, relay: str, token: str, live_dir: Path, http: Http | None = None,
                 clock=time.time, mono=time.monotonic, log=print):
        self.local = local.rstrip("/")
        self.relay = relay.rstrip("/")
        self.token = token
        self.live_dir = Path(live_dir)
        self.http = http or Http()
        self.clock = clock
        self.mono = mono
        self.log = log
        self.local_backoff = Backoff(mono)
        self.relay_backoff = Backoff(mono)
        self.local_up = False
        self.relay_up = False
        self.tid: str | None = None
        self.state: dict | None = None
        self._raw_state = b""
        self._cycle_err = False
        self.pushed_fp: str | None = None
        self.eval_track = EvalTrack()
        self.last_state_push_mono = -1e9
        self.last_state_push_epoch_ms: int | None = None
        self.state_bytes_gz = 0
        self.last_seq: int | None = None
        self.seq_known = False
        self.health_at = -1e9
        self.local_probe_at = -1e9
        self.local_newest: int | None = None    # newest local clip seq seen (None: not known yet)
        self.clips_pushed = 0
        self.audio_misses: dict[int, int] = {}
        self.think_sizes: dict[tuple[str, int], int] = {}
        self.think_at: dict[str, float] = {}
        self.board_ply: dict[str, int] = {}
        self.version = None
        self.version_at = -1e9
        self.status_at = -1e9
        self.error = ""
        self.state_pushes = 0
        self.summary_at = mono()

    # ----- endpoints

    def _local_get(self, path: str, timeout: float = 10) -> bytes:
        try:
            code, body = self.http.get(self.local + path, timeout)
        except (OSError, ValueError) as exc:
            self._local_failed(f"local viewer unreachable: {exc.__class__.__name__}")
            raise EndpointDown() from exc
        if code == 404:
            return b""
        if code != 200:
            self._local_failed(f"local viewer HTTP {code} on {path.split('?')[0]}")
            raise EndpointDown()
        self.local_up = True
        self.local_backoff.ok()
        return body

    def _local_failed(self, message: str) -> None:
        self.local_up = False
        self.local_backoff.fail()
        self._set_error(message)

    def _relay_post(self, path: str, body: bytes, kind: str = "application/json", extra: dict | None = None) -> dict:
        headers = {"Content-Type": kind, "X-Ingest-Token": self.token}
        headers.update(extra or {})
        try:
            code, resp = self.http.post(self.relay + path, body, headers)
        except (OSError, ValueError) as exc:
            self._relay_failed(f"relay unreachable: {exc.__class__.__name__}")
            raise EndpointDown() from exc
        if code != 200:
            detail = resp[:200].decode("utf-8", errors="replace")
            if code >= 500 or code in (403, 404):
                self._relay_failed(f"relay HTTP {code} on {path}: {detail}")
                raise EndpointDown()
            self._set_error(f"relay rejected {path}: HTTP {code} {detail}")   # bad payload: do not stall the loop
            self.log(self.error)
            return {"rejected": True}
        self.relay_up = True
        self.relay_backoff.ok()
        try:
            return json.loads(resp or b"{}")
        except ValueError:
            return {}

    def _relay_failed(self, message: str) -> None:
        self.relay_up = False
        self.relay_backoff.fail()
        self._set_error(message)

    def _set_error(self, message: str) -> None:
        self.error = message
        self._cycle_err = True

    def _relay_health(self) -> dict:
        try:
            code, body = self.http.get(self.relay + "/healthz", 10)
        except (OSError, ValueError) as exc:
            self._relay_failed(f"relay unreachable: {exc.__class__.__name__}")
            raise EndpointDown() from exc
        if code != 200:
            self._relay_failed(f"relay healthz HTTP {code}")
            raise EndpointDown()
        self.relay_up = True
        self.relay_backoff.ok()
        return json.loads(body)

    # ----- one cycle

    def cycle(self) -> None:
        self._cycle_err = False
        if self.local_backoff.ready():
            try:
                self._read_state()
            except EndpointDown:
                pass
        if self.state is not None and self.relay_backoff.ready():
            try:
                self._push_state()
                if self.local_up:
                    self._push_clips()
                    self._push_thinking()
                    self._push_version()
            except EndpointDown:
                pass
        if self.local_up and self.relay_up and not self._cycle_err:
            self.error = ""
        self._write_status()
        self._summary()

    def _read_state(self) -> None:
        body = self._local_get("/api/tournament")
        if not body:
            self._set_error("local viewer has no tournament state yet")
            return
        state = json.loads(body)
        tid = state.get("id")
        if not tid:
            return
        if tid != self.tid:            # new tournament: start its bookkeeping fresh
            if self.tid is not None:
                self.log(f"tournament changed {self.tid} -> {tid}")
                self.pushed_fp = None
                self.think_sizes.clear()
                self.board_ply.clear()
            self.tid = tid
            self._load_status_seq()
        try:
            track = self.eval_track.build(state, self.live_dir / f"{tid}-annotations.json")
        except Exception as exc:           # scores are a bonus: never stop publishing the state
            self.log(f"eval track skipped: {exc.__class__.__name__}")
            track = {}
        if track:
            state["eval_track"] = track
            body = json.dumps(state, separators=(",", ":")).encode("utf-8")
        self.state = state
        self._raw_state = body

    def _push_state(self) -> None:
        fp = state_fingerprint(self.state)
        if fp == self.pushed_fp or self.mono() - self.last_state_push_mono < STATE_MIN_GAP:
            return
        packed = gzip.compress(self._raw_state, 6)
        result = self._relay_post("/ingest/state", packed, "application/json", {"Content-Encoding": "gzip"})
        self.last_state_push_mono = self.mono()
        if result.get("rejected"):
            return
        self.pushed_fp = fp
        self.state_bytes_gz = len(packed)
        self.last_state_push_epoch_ms = int(self.clock() * 1000)
        self.state_pushes += 1

    def _sync_cursor_with_relay(self) -> None:
        """Set the clip cursor from what the relay holds. The relay is the truth, the status file is not.

        Resending is safe (the relay ingest is idempotent by tournament id + seq), skipping is not:
        a relay whose data dir was archived reports 0 while the status file still says 509.
        """
        health = self._relay_health()
        self.health_at = self.mono()
        self.seq_known = True
        try:
            relay_last = int(health.get("last_clip_seq") or 0)
        except (TypeError, ValueError):
            relay_last = 0
        other = (health.get("tournament_id") not in (None, self.tid)
                 or health.get("clips_tournament_id") not in (None, self.tid))
        cursor = None if other or relay_last <= 0 else relay_last   # None: newest 20 only
        renumbered = cursor is not None and self.local_newest is not None and cursor > self.local_newest
        if renumbered:                        # relay holds the old numbering: keep our own cursor
            mine = self.last_seq
            cursor = mine if mine is not None and mine <= self.local_newest else None
        if cursor != self.last_seq:
            reason = ("relay holds another tournament" if other else
                      f"relay has seq {relay_last}, local newest {self.local_newest}" if renumbered else
                      f"relay has seq {relay_last}")
            self.log(f"clips: cursor {self.last_seq} -> {cursor} ({reason})")
            self.last_seq = cursor

    def _push_clips(self) -> None:
        if not self.seq_known or self.mono() - self.health_at >= HEALTH_EVERY:
            self._sync_cursor_with_relay()
        after = self.last_seq if self.last_seq is not None else 0
        raw = self._local_get(f"/api/commentary?after={after}")
        fetched = self.mono()
        data = json.loads(raw) if raw else {}
        if not data.get("enabled"):
            return
        clips = data.get("clips") or []
        seen = max((c["seq"] for c in clips if isinstance(c.get("seq"), int)), default=None)
        if seen is not None:
            self.local_newest = max(seen, self.local_newest or 0)
        todo = select_new_clips(clips, self.last_seq)
        if not todo and self.last_seq is not None and self.mono() - self.local_probe_at >= LOCAL_PROBE_EVERY:
            # Nothing above the cursor. A fresh viewer process numbers its clips from 1 again, so
            # check the local newest seq: below the cursor means everything new would be skipped.
            self.local_probe_at = self.mono()
            every = json.loads(self._local_get("/api/commentary?after=0") or b"{}").get("clips") or []
            newest = max((c["seq"] for c in every if isinstance(c.get("seq"), int)), default=0)
            self.local_newest = newest
            if newest < self.last_seq:
                self.log(f"clips: local newest seq {newest} is below cursor {self.last_seq}: resending newest")
                self.last_seq = None
                todo = select_new_clips(every, None)
                fetched = self.mono()
        if todo and self.last_seq is not None and todo[0]["seq"] > self.last_seq + 1:
            self.log(f"clips: catching up from seq {todo[0]['seq']} (skipped {todo[0]['seq'] - self.last_seq - 1} older)")
        for clip in todo[:CLIPS_PER_CYCLE]:
            audio = clip.get("audio")
            if audio:
                wav = self._local_get(f"/api/commentary/audio/{quote(audio)}", 20)
                if wav:
                    self._relay_post(f"/ingest/clip-audio/{quote(audio)}", wav, "audio/wav")
                else:
                    misses = self.audio_misses.get(clip["seq"], 0) + 1
                    self.audio_misses[clip["seq"]] = misses
                    if misses < AUDIO_MISSING_TRIES:
                        return              # try again next cycle, keep the order
                    self.log(f"clip {clip['seq']}: audio {audio} missing locally, pushing text only")
            if isinstance(clip.get("age_s"), (int, float)):   # age at push: add the time spent uploading
                clip = dict(clip, age_s=round(clip["age_s"] + (self.mono() - fetched), 1))
            clip = dict(clip, tournament_id=self.tid)    # the relay keys clips by tournament id + seq
            result = self._relay_post("/ingest/clips", json.dumps([clip]).encode("utf-8"))
            self.last_seq = clip["seq"]
            self.audio_misses.pop(clip["seq"], None)
            if not result.get("rejected"):
                self.clips_pushed += 1
                self.log(f"clip {clip['seq']} pushed ({clip.get('game')}, {len(wav) if audio and wav else 0} B audio)")

    def _push_thinking(self) -> None:
        boards = live_boards(self.state)
        now = self.mono()
        for gid, ply in boards.items():
            if now - self.think_at.get(gid, -1e9) < THINK_MIN_GAP:
                continue
            self.think_at[gid] = now
            previous = self.board_ply.get(gid)
            plies = [ply]
            if previous is not None and previous != ply and 1 <= previous:
                plies.insert(0, previous)   # the move just ended: send its final text once
            self.board_ply[gid] = ply
            for p in plies:
                raw = self._local_get(f"/api/thinking?game={quote(gid)}&ply={p}&since=0", 10)
                if not raw:
                    continue
                info = json.loads(raw)
                if not info.get("exists"):
                    continue
                key = (gid, p)
                if self.think_sizes.get(key) == info.get("size"):
                    continue
                payload = {"game": gid, "ply": p, "size": info.get("size"), "text": info.get("text") or "",
                           "from": info.get("from") or 0}
                result = self._relay_post("/ingest/thinking", json.dumps(payload).encode("utf-8"))
                if not result.get("rejected"):
                    self.think_sizes[key] = info.get("size")
        for gid in list(self.board_ply):
            if gid not in boards:
                self.board_ply.pop(gid, None)
        if len(self.think_sizes) > 2000:
            for key in list(self.think_sizes)[:-500]:
                del self.think_sizes[key]

    def _push_version(self) -> None:
        if self.mono() - self.version_at < VERSION_EVERY:
            return
        self.version_at = self.mono()
        raw = self._local_get("/api/viewer-version")
        version = (json.loads(raw) if raw else {}).get("version")
        if version is not None and version != self.version:
            result = self._relay_post("/ingest/version", json.dumps({"version": version}).encode())
            if not result.get("rejected"):
                self.version = version

    # ----- status file

    def status_path(self) -> Path | None:
        return self.live_dir / f"{self.tid}-publish-status.json" if self.tid else None

    def status(self) -> dict:
        fresh = self.last_state_push_epoch_ms is not None and self.clock() * 1000 - self.last_state_push_epoch_ms < 600_000
        return {"ok": bool(self.local_up and self.relay_up and fresh and not self.error),
                "relay_up": self.relay_up, "local_up": self.local_up,
                "last_state_push_epoch_ms": self.last_state_push_epoch_ms,
                "last_clip_seq": self.last_seq, "clips_pushed": self.clips_pushed,
                "state_bytes_gz": self.state_bytes_gz, "tournament_id": self.tid,
                "updated_epoch_ms": int(self.clock() * 1000), "error": self.error}

    def _load_status_seq(self) -> None:
        self.seq_known = False
        self.last_seq = None
        self.local_newest = None
        self.local_probe_at = -1e9
        path = self.status_path()
        try:
            seq = json.loads(path.read_text("utf-8")).get("last_clip_seq")
            if isinstance(seq, int):
                self.last_seq = seq
        except (OSError, ValueError, AttributeError):
            pass

    def _write_status(self, force: bool = False) -> None:
        path = self.status_path()
        if path is None or (not force and self.mono() - self.status_at < STATUS_EVERY):
            return
        self.status_at = self.mono()
        data = json.dumps(self.status(), indent=1).encode("utf-8")
        tmp = path.with_name(path.name + ".tmp")
        for _ in range(3):
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_bytes(data)
                os.replace(tmp, path)      # the viewer may hold it open for a moment on Windows
                return
            except OSError:
                time.sleep(0.05)

    def _summary(self) -> None:
        if self.mono() - self.summary_at < 60:
            return
        self.summary_at = self.mono()
        self.log(f"status: local_up={self.local_up} relay_up={self.relay_up} state_pushes={self.state_pushes} "
                 f"last_clip_seq={self.last_seq} clips_pushed={self.clips_pushed} state_gz={self.state_bytes_gz}"
                 + (f" error={self.error}" if self.error else ""))
        self.state_pushes = 0

    def run(self, interval: float = 1.0) -> None:
        self.log(f"pusher: local {self.local} -> relay {self.relay}, status in {self.live_dir}")
        while True:
            started = self.mono()
            try:
                self.cycle()
            except Exception as exc:   # never crash: log and try again
                self.error = f"{exc.__class__.__name__}: {exc}"[:300]
                self.log(f"cycle error: {self.error}")
            time.sleep(max(0.2, interval - (self.mono() - started)))


def log_line(message: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}", flush=True)


def read_token(path: Path) -> str:
    token = path.read_text("utf-8").strip()
    if len(token) < 16:
        raise SystemExit(f"token file {path} is empty or too short")
    return token


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--local", default="http://127.0.0.1:8770", help="local tournament viewer")
    parser.add_argument("--relay", default="http://127.0.0.1:18781", help="relay ingest URL (ssh tunnel)")
    parser.add_argument("--token-file", type=Path, default=Path.home() / ".aichess-ingest-token")
    parser.add_argument("--live-dir", type=Path, required=True, help="out/live of the checkout that runs the tournament")
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args(argv)
    pusher = Pusher(args.local, args.relay, read_token(args.token_file), args.live_dir, log=log_line)
    try:
        pusher.run(args.interval)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
