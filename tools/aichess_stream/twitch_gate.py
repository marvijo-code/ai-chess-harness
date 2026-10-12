#!/usr/bin/env python3
"""Twitch gate: decides WHEN the AI-chess stream is sent to Twitch (runs on the VPS as ai-chess-twitch-gate).

Why: Twitch counts "average viewers" over every minute a channel is live. A stream that is live 24 hours
a day needs 72 viewer-hours a day for an average of 3; a 2 hour show needs 6. So Twitch gets a daily show
and YouTube stays live all day.

How: stream.sh sends its second output (tee muxer) to this gate on a local RTMP address instead of to
Twitch. The gate runs one small ffmpeg that copies the stream on (no encode):
  show is on   -> to Twitch (key from the Twitch key file)
  show is off  -> to nowhere (-f null), so the stream's Twitch leg stays connected and quiet
At a change the gate stops that ffmpeg and starts the other one; the stream's Twitch leg reconnects by
itself in about 10 s. The main YouTube leg is never touched.

Schedule file (default ~/.config/ai-chess/twitch-show.env, read again whenever it changes):
  TWITCH_SHOW_MODE=daily            always | daily | off   (no file = always, the old behaviour)
  TWITCH_SHOW_START=19:00           local start time, 24 hour clock
  TWITCH_SHOW_MINUTES=120           length of the show
  TWITCH_SHOW_TZ=Africa/Johannesburg

Commands:
  twitch_gate.py run                                  the service loop
  twitch_gate.py set --mode daily --start 19:00 --minutes 120 [--tz Africa/Johannesburg]
  twitch_gate.py set --mode always | --mode off
  twitch_gate.py status                               one line + the status file

Environment: AICHESS_TWITCH_GATE (listen address, rtmp://127.0.0.1:19350/live/aichess),
AICHESS_TWITCH_KEY_FILE (~/.config/ai-chess/twitch-stream.env), AICHESS_TWITCH_SHOW_FILE,
RUNTIME_DIRECTORY (status.json goes there). The key is never logged: ffmpeg output passes a filter.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from zoneinfo import ZoneInfo

DEFAULT_GATE = "rtmp://127.0.0.1:19350/live/aichess"
DEFAULT_TWITCH_URL = "rtmp://ingest.global-contribute.live-video.net/app"
MODES = ("always", "daily", "off")
UTC = dt.timezone.utc
DAY = dt.timedelta(days=1)


@dataclass(frozen=True)
class Show:
    mode: str = "always"
    start_minute: int = 19 * 60      # minutes after local midnight
    minutes: int = 120
    tz: str = "Africa/Johannesburg"

    @property
    def start_text(self) -> str:
        return "%02d:%02d" % divmod(self.start_minute, 60)

    def describe(self) -> str:
        if self.mode != "daily":
            return {"always": "always on (24 hours a day)", "off": "off (Twitch gets nothing)"}[self.mode]
        return "daily %s for %d min (%s)" % (self.start_text, self.minutes, self.tz)


def parse_clock(text: str) -> int:
    parts = text.strip().split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        raise ValueError("start time must be HH:MM, got %r" % text)
    h, m = int(parts[0]), int(parts[1])
    if h > 23 or m > 59:
        raise ValueError("start time must be HH:MM, got %r" % text)
    return h * 60 + m


def parse_config(text: str) -> Show:
    """KEY=VALUE lines -> Show. Unknown keys and # comments are ignored; a bad value raises ValueError."""
    vals = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        vals[k.strip()] = v.strip().strip('"').strip("'")
    base = Show()
    mode = vals.get("TWITCH_SHOW_MODE", base.mode).lower()
    if mode not in MODES:
        raise ValueError("TWITCH_SHOW_MODE must be one of %s, got %r" % (", ".join(MODES), mode))
    start = parse_clock(vals["TWITCH_SHOW_START"]) if "TWITCH_SHOW_START" in vals else base.start_minute
    raw = vals.get("TWITCH_SHOW_MINUTES", str(base.minutes))
    if not raw.isdigit() or not 1 <= int(raw) <= 1440:
        raise ValueError("TWITCH_SHOW_MINUTES must be 1 to 1440, got %r" % raw)
    tz = vals.get("TWITCH_SHOW_TZ", base.tz)
    try:
        ZoneInfo(tz)
    except Exception:
        raise ValueError("TWITCH_SHOW_TZ is not a known time zone: %r" % tz) from None
    return Show(mode, start, int(raw), tz)


def render_config(show: Show) -> str:
    return ("# Twitch show schedule, read by ai-chess-twitch-gate (tools/aichess_stream/twitch_gate.py).\n"
            "# always = 24 hours a day, daily = only the window below, off = never.\n"
            "TWITCH_SHOW_MODE=%s\nTWITCH_SHOW_START=%s\nTWITCH_SHOW_MINUTES=%d\nTWITCH_SHOW_TZ=%s\n"
            % (show.mode, show.start_text, show.minutes, show.tz))


def window(show: Show, now: dt.datetime) -> tuple[bool, dt.datetime | None]:
    """(is the show on at `now`, the next moment that answer changes or None). `now` must carry a zone."""
    if show.mode == "always" or (show.mode == "daily" and show.minutes >= 1440):
        return True, None
    if show.mode == "off":
        return False, None
    zone = ZoneInfo(show.tz)
    local = now.astimezone(zone)
    length = dt.timedelta(minutes=show.minutes)
    h, m = divmod(show.start_minute, 60)
    starts = []
    for delta in (-1, 0, 1):       # yesterday's show can still run past midnight
        day = (local + delta * DAY).date()
        starts.append(dt.datetime(day.year, day.month, day.day, h, m, tzinfo=zone).astimezone(UTC))
    now = now.astimezone(UTC)
    for s in starts:
        if s <= now < s + length:
            return True, s + length
    return False, min(s for s in starts if s > now)


def relay_cmd(listen_url: str, target: str | None, ffmpeg: str = "ffmpeg") -> list[str]:
    """The copy-only ffmpeg: listen for the stream's Twitch leg, pass it on to `target` (None = nowhere)."""
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-nostats", "-loglevel", "warning",
           "-f", "flv", "-listen", "1", "-i", listen_url, "-c", "copy"]
    if target:
        return cmd + ["-f", "flv", "-flvflags", "no_duration_filesize", target]
    return cmd + ["-f", "null", "-"]


def read_twitch_target(path: str) -> tuple[str | None, str | None]:
    """(full RTMP target, key) from the Twitch key file, or (None, None) when there is no key."""
    url = key = ""
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("TWITCH_RTMP_URL="):
                    url = line.partition("=")[2].strip()
                elif line.startswith("TWITCH_STREAM_KEY="):
                    key = line.partition("=")[2].strip()
    except OSError:
        return None, None
    if not key:
        return None, None
    return (url or DEFAULT_TWITCH_URL).rstrip("/") + "/" + key, key


def mask(line: str, secrets) -> str:
    for s in secrets:
        if s:
            line = line.replace(s, "<key>")
    return line


def log(msg: str) -> None:
    print("%s twitch-gate: %s" % (time.strftime("%F %T"), msg), flush=True)


def default_paths() -> dict:
    home = os.path.expanduser("~")
    key_file = os.environ.get("AICHESS_TWITCH_KEY_FILE")
    return {
        "gate": os.environ.get("AICHESS_TWITCH_GATE") or DEFAULT_GATE,
        "key_file": key_file if key_file is not None else os.path.join(home, ".config/ai-chess/twitch-stream.env"),
        "show_file": os.environ.get("AICHESS_TWITCH_SHOW_FILE") or os.path.join(home, ".config/ai-chess/twitch-show.env"),
        "run_dir": os.environ.get("RUNTIME_DIRECTORY") or "/run/ai-chess-twitch-gate",
    }


def spawn_relay(cmd: list[str], secrets) -> subprocess.Popen:
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            text=True, errors="replace")

    def pump():
        for line in proc.stderr:
            print("ffmpeg: " + mask(line.rstrip("\n"), secrets), flush=True)
    threading.Thread(target=pump, daemon=True).start()
    return proc


class Gate:
    """One step = look at the schedule and the clock, make the running relay match. Kept free of sleeps
    and real processes (spawn is passed in) so the tests can drive it."""

    def __init__(self, paths: dict, spawn=spawn_relay, clock=time.time):
        self.paths, self.spawn, self.clock = paths, spawn, clock
        self.show: Show | None = None
        self.show_stamp = "unread"
        self.proc = None
        self.sending = None            # "twitch" | "nowhere" | None (no relay)
        self.started = 0.0
        self.restarts = 0
        self.next_try = 0.0
        self.backoff = 2.0
        self.note = ""

    def say(self, msg: str) -> None:
        if msg != self.note:
            self.note = msg
            log(msg)

    def load_show(self) -> None:
        path = self.paths["show_file"]
        try:
            st = os.stat(path)
            stamp = "%s:%s" % (st.st_mtime_ns, st.st_size)
        except OSError:
            stamp = "missing"
        if stamp == self.show_stamp:
            return
        self.show_stamp = stamp
        if stamp == "missing":
            self.show = Show()
            log("no schedule file %s: %s" % (path, self.show.describe()))
            return
        try:
            with open(path, encoding="utf-8") as fh:
                self.show = parse_config(fh.read())
            log("schedule: " + self.show.describe())
        except (OSError, ValueError) as exc:
            if self.show is None:
                self.show = Show(mode="off")
                log("schedule file is not valid (%s); Twitch stays OFF until it is fixed" % exc)
            else:
                log("schedule file is not valid (%s); keeping: %s" % (exc, self.show.describe()))

    def stop_relay(self) -> None:
        proc, self.proc, self.sending = self.proc, None, None
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)

    def step(self, now: dt.datetime | None = None) -> dict:
        now = now or dt.datetime.now(UTC)
        self.load_show()
        on, change = window(self.show, now)
        target, key = read_twitch_target(self.paths["key_file"]) if on else (None, None)
        if on and not target:
            self.say("the show is on but there is no Twitch key in %s; sending nowhere" % self.paths["key_file"])
        want = "twitch" if target else "nowhere"

        if self.proc is not None and self.proc.poll() is not None:
            ran = self.clock() - self.started
            # a short life means the port is taken or Twitch refused: wait longer before the next try
            self.backoff = 2.0 if ran > 60 else min(self.backoff * 2, 30.0)
            self.next_try = self.clock() + (0 if ran > 60 else self.backoff)
            log("relay to %s ended (code %s after %ds)" % (self.sending, self.proc.returncode, ran))
            self.proc, self.sending = None, None
            self.restarts += 1
        if self.proc is not None and self.sending != want:
            log("show %s: switching the relay from %s to %s" % ("starts" if on else "ends", self.sending, want))
            self.stop_relay()
            self.next_try = 0.0
        if self.proc is None and self.clock() >= self.next_try:
            self.proc = self.spawn(relay_cmd(self.paths["gate"], target), [key])
            self.sending, self.started = want, self.clock()
            log("relay pid %s: %s -> %s" % (getattr(self.proc, "pid", "?"), self.paths["gate"], want))

        return {
            "updated": now.astimezone(UTC).isoformat(timespec="seconds"),
            "mode": self.show.mode, "start": self.show.start_text, "minutes": self.show.minutes, "tz": self.show.tz,
            "schedule": self.show.describe(),
            "on": on, "sending": self.sending, "has_key": bool(target) if on else None,
            "next_change": change.isoformat(timespec="seconds") if change else None,
            "relay_pid": getattr(self.proc, "pid", None), "relay_restarts": self.restarts,
        }


def write_status(run_dir: str, status: dict) -> None:
    try:
        os.makedirs(run_dir, exist_ok=True)
        tmp = os.path.join(run_dir, "status.json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(status, fh)
        os.replace(tmp, os.path.join(run_dir, "status.json"))
    except OSError:
        pass


def status_line(status: dict, now: dt.datetime | None = None) -> str:
    """One plain line for status.sh, with the next change in the schedule's own time zone."""
    now = now or dt.datetime.now(UTC)
    line = "Twitch show: %s - now %s" % (status.get("schedule"), "ON AIR" if status.get("sending") == "twitch"
                                         else "off air")
    if status.get("on") and status.get("sending") != "twitch":
        line += " (the show is on but nothing is sent: see the gate log)"
    nxt = status.get("next_change")
    if nxt:
        when = dt.datetime.fromisoformat(nxt).astimezone(ZoneInfo(status.get("tz") or "UTC"))
        mins = max(0, int((when - now).total_seconds() // 60))
        line += ", %s %s (in %dh%02dm)" % ("ends" if status.get("on") else "starts", when.strftime("%a %H:%M"), mins // 60, mins % 60)
    try:
        age = (now - dt.datetime.fromisoformat(status["updated"])).total_seconds()
        if age > 30:
            line += "  <- the gate has not reported for %d s" % age
    except (KeyError, ValueError):
        pass
    return line


def cmd_run(paths: dict) -> int:
    gate = Gate(paths)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    log("listening on %s" % paths["gate"])
    while not stop.is_set():
        write_status(paths["run_dir"], gate.step())
        stop.wait(2)
    log("stopping")
    gate.stop_relay()
    return 0


def cmd_set(paths: dict, args) -> int:
    try:
        with open(paths["show_file"], encoding="utf-8") as fh:
            old = parse_config(fh.read())
    except (OSError, ValueError):
        old = Show()
    lines = ["TWITCH_SHOW_MODE=%s" % args.mode,
             "TWITCH_SHOW_START=%s" % (args.start or old.start_text),
             "TWITCH_SHOW_MINUTES=%s" % (args.minutes if args.minutes is not None else old.minutes),
             "TWITCH_SHOW_TZ=%s" % (args.tz or old.tz)]
    try:
        show = parse_config("\n".join(lines))
    except ValueError as exc:
        print("not saved: %s" % exc, file=sys.stderr)
        return 2
    os.makedirs(os.path.dirname(paths["show_file"]), exist_ok=True)
    tmp = paths["show_file"] + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(render_config(show))
    os.replace(tmp, paths["show_file"])
    on, change = window(show, dt.datetime.now(UTC))
    print("saved %s: %s" % (paths["show_file"], show.describe()))
    print("now %s%s" % ("ON" if on else "off", "" if change is None else ", next change %s"
                        % change.astimezone(ZoneInfo(show.tz)).strftime("%a %Y-%m-%d %H:%M %Z")))
    return 0


def cmd_status(paths: dict) -> int:
    path = os.path.join(paths["run_dir"], "status.json")
    try:
        with open(path, encoding="utf-8") as fh:
            status = json.load(fh)
    except (OSError, ValueError):
        print("Twitch show: the gate is not running (no %s)" % path)
        return 1
    print(status_line(status))
    print(json.dumps(status))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Twitch gate: sends the AI-chess stream to Twitch only while the show is on.")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("run")
    sp = sub.add_parser("set")
    sp.add_argument("--mode", required=True, choices=MODES)
    sp.add_argument("--start", help="local start time HH:MM")
    sp.add_argument("--minutes", type=int, help="length of the show in minutes")
    sp.add_argument("--tz", help="time zone name, for example Africa/Johannesburg")
    sub.add_parser("status")
    args = ap.parse_args(argv)
    paths = default_paths()
    if args.cmd == "set":
        return cmd_set(paths, args)
    if args.cmd == "status":
        return cmd_status(paths)
    return cmd_run(paths)


if __name__ == "__main__":
    sys.exit(main())
