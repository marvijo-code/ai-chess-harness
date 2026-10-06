#!/usr/bin/env python3
"""Record one live round of the LLM chess tournament viewer on a private Xvfb display (runs on the VPS).

The viewer is reached through a reverse SSH tunnel (laptop 8770 -> VPS --port). Headful Chrome fills the
display in kiosk mode, the Commentary chip is unmuted when the round starts, Move sound stays on, and an
init script logs every commentary clip and every move click (page epoch ms) into events.jsonl.
ffmpeg x11grab records the display losslessly (H.264 CRF 0, yuv444p) into raw.mkv; its x11grab input
start time (wall-clock epoch) is saved in meta.json so events map to video time.

Usage:
  rec.py test   <run_dir> [--port 18770] [--display 97]
  rec.py record <run_dir> --round N [--slug ID] [--port 18770] [--display 97] [--max-min 75] [--wait-change]
  rec.py series <run_dir> --from-round N [--slug ID] [--port 18770] [--display 97] [--wait-change] [--max-min 360]

Series mode: one Xvfb + one Chrome page for the rest of the tournament, one lossless take per round
(raw-r<R>.mkv); at each round boundary the next ffmpeg starts before the previous one stops (no gap) and
rounds.json + R<key>_REC_DONE record each closed take. Boundaries: see timeline.RoundBoundary.

Stop rule: every game in the round's pairings has a result (not live, not "*") AND the round status is
"finished" (knockout rounds can grow an Armageddon decider; it is re-read every poll), then a tail that
lasts until the page has played the host's round recap and closed the results card (at least 15 s, at most
120 s), so every round ends on its recap.
Hard cap --max-min minutes of recording.
"""
import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))
import timeline as tl  # noqa: E402

try:
    from playwright.sync_api import sync_playwright
except ImportError:          # unit tests import this module without Playwright
    sync_playwright = None

W, H = 1920, 1080

INIT = r"""
(() => {
  const ep = () => performance.timeOrigin + performance.now();
  const send = (o) => { try { window.aclLog(JSON.stringify(Object.assign({ t: ep() }, o))); } catch (e) {} };
  const origPlay = HTMLMediaElement.prototype.play;
  HTMLMediaElement.prototype.play = function () {
    const el = this;
    const src = el.src || el.currentSrc;
    send({ kind: "clip_play", src });
    if (!el.__acl) {
      el.__acl = 1;
      el.addEventListener("playing", () => send({ kind: "clip_playing", src: el.src, ct: el.currentTime }));
      el.addEventListener("pause", () => send({ kind: "clip_pause", src: el.src, ct: el.currentTime }));
      el.addEventListener("ended", () => send({ kind: "clip_ended", src: el.src, ct: el.currentTime, dur: el.duration }));
      el.addEventListener("error", () => send({ kind: "clip_error", src: el.src, code: el.error && el.error.code }));
    }
    const p = origPlay.apply(el, arguments);
    if (p && p.then) p.then(() => send({ kind: "clip_play_ok", src }), (e) => send({ kind: "clip_play_err", src, err: String(e && e.name) }));
    return p;
  };
  const origStart = AudioBufferSourceNode.prototype.start;
  AudioBufferSourceNode.prototype.start = function (when) {
    let delay = 0;
    try { delay = Math.max(0, (when || 0) - this.context.currentTime); } catch (e) {}
    send({ kind: "click", delay, state: this.context && this.context.state });
    return origStart.apply(this, arguments);
  };
  // Director cues of the viewer (board tours = time-lapse spans, round cards, champion overlay).
  window.addEventListener("acl-director", (e) => send(Object.assign({ kind: "director" }, (e && e.detail) || {})));
})();
"""


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


class Events:
    def __init__(self, path, on_director=None):
        self.f = open(path, "a", buffering=1)
        self.lock = threading.Lock()
        self.on_director = on_director

    def write(self, obj):
        with self.lock:
            self.f.write(json.dumps(obj) + "\n")

    def from_page(self, s):
        try:
            obj = json.loads(s)
        except Exception:
            obj = {"raw": s}
        obj["py"] = time.time()
        self.write(obj)
        if self.on_director and obj.get("kind") == "director":
            self.on_director(obj)


def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def start_xvfb(run, disp_no):
    lock = Path(f"/tmp/.X{disp_no}-lock")
    if lock.exists():
        try:
            other = int(lock.read_text().strip())
        except Exception:
            other = 0
        if other and pid_alive(other):
            raise SystemExit(f"display :{disp_no} is in use by pid {other}")
        lock.unlink(missing_ok=True)                       # stale lock of a dead server
        Path(f"/tmp/.X11-unix/X{disp_no}").unlink(missing_ok=True)
    p = subprocess.Popen(["Xvfb", f":{disp_no}", "-screen", "0", f"{W}x{H}x24", "-nolisten", "tcp"],
                         stdout=open(run / "xvfb.log", "w"), stderr=subprocess.STDOUT)
    (run / "xvfb.pid").write_text(str(p.pid))
    for _ in range(50):
        if Path(f"/tmp/.X11-unix/X{disp_no}").exists():
            break
        time.sleep(0.1)
    time.sleep(0.5)
    return p


def open_page(pw, run, ev, url, disp):
    ctx = pw.chromium.launch_persistent_context(
        str(run / "chrome-profile"),
        executable_path="/usr/bin/google-chrome",
        headless=False,
        no_viewport=True,
        env={**os.environ, "DISPLAY": disp},
        ignore_default_args=["--enable-automation"],
        args=[
            "--kiosk", "--start-fullscreen", f"--window-size={W},{H}", "--window-position=0,0",
            "--autoplay-policy=no-user-gesture-required", "--no-first-run", "--no-default-browser-check",
            "--disable-infobars", "--disable-session-crashed-bubble", "--disable-features=Translate",
            "--password-store=basic", "--hide-scrollbars",
        ],
    )
    ctx.expose_function("aclLog", ev.from_page)
    ctx.add_init_script(INIT)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_selector("[data-toggle-sound]", timeout=60000)
    page.wait_for_selector("[data-toggle-commentary]", timeout=60000)
    return ctx, page


def chip_text(page, sel):
    try:
        return page.locator(sel).first.inner_text(timeout=5000)
    except Exception:
        return ""


def ensure_sound_on(page):
    if "off" in chip_text(page, "[data-toggle-sound]"):
        page.locator("[data-toggle-sound]").first.click()
        time.sleep(0.3)
    return chip_text(page, "[data-toggle-sound]")


def ensure_autofocus_on(page):
    """Keep the Auto-focus chip ON (if this viewer has it) so the take follows the commentated board."""
    if page.locator("[data-toggle-autofocus]").count() == 0:
        return "no auto-focus chip"
    t = chip_text(page, "[data-toggle-autofocus]")
    if t.split(":", 1)[-1].strip().startswith("off"):
        page.locator("[data-toggle-autofocus]").first.click()
        time.sleep(0.3)
        t = chip_text(page, "[data-toggle-autofocus]")
    return t


def unmute(page):
    for _ in range(4):
        t = chip_text(page, "[data-toggle-commentary]")
        if t.split(":")[-1].strip() == "on":
            return t
        page.locator("[data-toggle-commentary]").first.click()
        time.sleep(0.6)
    return chip_text(page, "[data-toggle-commentary]")


def grab_png(path, disp):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "x11grab", "-draw_mouse", "0",
                    "-video_size", f"{W}x{H}", "-i", f"{disp}.0+0,0", "-frames:v", "1", str(path)], check=False)


def round_state(page, rnd):
    return page.evaluate("""(rnd) => {
      if (typeof data === 'undefined' || !data || !data.rounds) return null;
      const r = data.rounds.find(x => x.round === rnd);
      const games = r ? r.pairings.map(p => { const g = (data.games || {})[p.game_id] || {};
        return { id: p.game_id, status: g.status, result: g.result, plies: (g.moves || []).length }; }) : [];
      return { id: data.id, current: data.current_round, finished: !!data.finished, paused: data.paused || null,
               exists: !!r, status: r ? r.status : null, games };
    }""", rnd)


def show_over(page, rnd):
    """The page finished the round's closing show: recap line heard (round robin rounds), no clip playing, no card."""
    try:
        return bool(page.evaluate(
            "n => { if (typeof heardEvents === 'undefined') return true;"
            " const rr = rrRounds().some(r => r.round === n);"
            " return (!rr || heardEvents.has('recap-' + n)) && !clipPlaying && !rcardEl && !commentaryQueue.length; }", rnd))
    except Exception:
        return True


def round_done(st):
    if not st or not st["exists"] or not st["games"]:
        return False
    games_done = all(g["status"] != "live" and g["result"] not in (None, "", "*") for g in st["games"])
    return games_done and st["status"] == "finished"


def do_test(run, url, disp_no):
    disp = f":{disp_no}"
    ev = Events(run / "test-events.jsonl")
    xv = start_xvfb(run, disp_no)
    try:
        with sync_playwright() as pw:
            ctx, page = open_page(pw, run, ev, url, disp)
            log("page open; sound chip: " + ensure_sound_on(page))
            page.locator("#title").click()  # a plain user click: unlocks the AudioContext
            time.sleep(0.5)
            log("audioCtx: " + str(page.evaluate("audioCtx && audioCtx.state")))
            page.evaluate("playClick(0)")
            log("viewport: " + str(page.evaluate("[innerWidth, innerHeight, screen.width, screen.height]")))
            grab_png(run / "test-frame.png", disp)
            ctx.close()
    finally:
        xv.terminate()
        xv.wait(10)
    log("test done")


def do_record(run, url, rnd, max_min, wait_hours, disp_no, allow_late, wait_change=False):
    disp = f":{disp_no}"
    ev = Events(run / "events.jsonl")
    meta = {"round": rnd, "url": url, "display": disp}
    stop = {"flag": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))
    xv = start_xvfb(run, disp_no)
    ff = None
    try:
        with sync_playwright() as pw:
            ctx, page = open_page(pw, run, ev, url, disp)
            log("page open; sound chip: " + ensure_sound_on(page) + "; commentary chip: " + chip_text(page, "[data-toggle-commentary]")
                + "; auto-focus chip: " + ensure_autofocus_on(page))
            deadline = time.time() + wait_hours * 3600
            last = None
            first = True
            frozen = None
            while True:
                st = round_state(page, rnd)
                cur = st and st["current"]
                if cur != last:
                    log(f"tournament={st and st['id']} current_round={cur} paused={st and st['paused']}")
                    last = cur
                if st and st["exists"] and wait_change:
                    # The runner is stopped with this round frozen: start only when its games change
                    # (a resumed runner restarts them from move 1, so the signature changes at once).
                    sig = [(g["id"], g["status"], g["plies"]) for g in st["games"]]
                    if frozen is None:
                        frozen = sig
                        log(f"round {rnd} frozen at {sig}; waiting for the runner to resume")
                    if sig == frozen:
                        if time.time() > deadline:
                            raise SystemExit("round never resumed")
                        if stop["flag"]:
                            raise SystemExit("stopped while waiting")
                        time.sleep(1)
                        continue
                    log(f"round {rnd} changed: {sig}")
                    break
                if st and st["exists"]:
                    if first and not allow_late and any((g["plies"] or 0) > 2 for g in st["games"]):
                        raise SystemExit(f"round {rnd} is already in progress; arm before it starts or pass --allow-late")
                    if round_done(st):
                        raise SystemExit(f"round {rnd} is already finished")
                    break
                first = False
                if time.time() > deadline:
                    raise SystemExit("round never started")
                if stop["flag"]:
                    raise SystemExit("stopped while waiting")
                time.sleep(2)
            meta["round_seen_epoch"] = time.time()
            ev.write({"kind": "round_seen", "py": time.time(), "state": st})
            raw = run / "raw.mkv"
            cmd = ["ffmpeg", "-hide_banner", "-y", "-f", "x11grab", "-draw_mouse", "0", "-framerate", "30",
                   "-video_size", f"{W}x{H}", "-thread_queue_size", "1024", "-i", f"{disp}.0+0,0",
                   "-c:v", "libx264", "-preset", "ultrafast", "-crf", "0", "-pix_fmt", "yuv444p",
                   "-fps_mode", "cfr", "-r", "30", str(raw)]
            meta["ffmpeg_popen_epoch"] = time.time()
            ff = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                  stderr=open(run / "ffmpeg-rec.log", "w"))
            (run / "ffmpeg.pid").write_text(str(ff.pid))
            log(f"recording round {rnd}; ffmpeg pid={ff.pid}")
            time.sleep(1.0)
            meta["sound_chip"] = ensure_sound_on(page)
            meta["unmute_click_epoch"] = time.time()
            ev.write({"kind": "unmute_click", "py": time.time()})
            page.locator("#title").click()      # a plain click first: unlocks the AudioContext for move clicks
            meta["commentary_chip"] = unmute(page)
            meta["autofocus_chip"] = ensure_autofocus_on(page)
            log("sound: " + meta["sound_chip"] + " | commentary: " + meta["commentary_chip"])
            rec_start = time.time()
            done_at = None
            nxt = 0
            while True:
                if ff.poll() is not None:
                    meta["stop_reason"] = "ffmpeg_exited"
                    log("ffmpeg exited early")
                    break
                if stop["flag"]:
                    meta["stop_reason"] = "sigterm"
                    break
                st = round_state(page, rnd)
                if time.time() > nxt:
                    ev.write({"kind": "state", "py": time.time(), "state": st})
                    nxt = time.time() + 60
                if done_at is None and round_done(st):
                    done_at = time.time()
                    meta["round_done_epoch"] = done_at
                    ev.write({"kind": "round_done", "py": done_at, "state": st})
                    log("round done; tail until the recap has played (15 to 120 s)")
                elif done_at is not None and not round_done(st):
                    done_at = None                  # an Armageddon decider was added: keep recording
                    meta.pop("round_done_epoch", None)
                    ev.write({"kind": "round_reopened", "py": time.time(), "state": st})
                    log("round reopened (new game in pairings)")
                if done_at and time.time() - done_at >= 15 and (time.time() - done_at >= 120 or show_over(page, rnd)):
                    time.sleep(3)                   # let the last word ring out
                    meta["stop_reason"] = "round_done"
                    meta["tail_seconds"] = round(time.time() - done_at, 1)
                    break
                if time.time() - rec_start > max_min * 60:
                    meta["stop_reason"] = "max_minutes"
                    log("max minutes reached")
                    break
                t = chip_text(page, "[data-toggle-commentary]")
                if "muted" in t or "click" in t:
                    ev.write({"kind": "re_unmute", "py": time.time(), "chip": t})
                    page.locator("#title").click()
                    unmute(page)
                time.sleep(2)
            meta["stop_epoch"] = time.time()
            if ff.poll() is None:
                ff.stdin.write(b"q")
                ff.stdin.flush()
                try:
                    ff.wait(120)
                except subprocess.TimeoutExpired:
                    ff.terminate()
                    ff.wait(30)
            meta["ffmpeg_rc"] = ff.returncode
            ctx.close()
    finally:
        if ff and ff.poll() is None:
            ff.terminate()
        xv.terminate()
        try:
            xv.wait(10)
        except Exception:
            xv.kill()
        logtxt = (run / "ffmpeg-rec.log").read_text(errors="ignore") if (run / "ffmpeg-rec.log").exists() else ""
        m = re.search(r"Input #0, x11grab.*?start: ([0-9.]+)", logtxt, re.S)
        if m:
            meta["ffmpeg_input_start"] = float(m.group(1))
        (run / "meta.json").write_text(json.dumps(meta, indent=2))
        log("meta: " + json.dumps(meta))


# ---- series mode --------------------------------------------------------------------------------

POLL_JS = """(rnd) => {
  if (typeof data === 'undefined' || !data || !data.rounds) return null;
  const r = data.rounds.find(x => x.round === rnd);
  const games = r ? (r.pairings || []).map(p => { const g = (data.games || {})[p.game_id] || {};
    return { id: p.game_id, status: g.status, result: g.result, plies: (g.moves || []).length }; }) : [];
  const k = data.knockout || null;
  const last = data.rounds.reduce((m, x) => Math.max(m, x.round || 0), 0);
  let heard = false, clip = false, card = false, q = 0;
  try { heard = heardEvents.has('champion'); } catch (e) {}
  try { clip = !!clipPlaying; } catch (e) {}
  try { card = !!rcardEl; } catch (e) {}
  try { q = commentaryQueue.length; } catch (e) {}
  return { id: data.id, current: data.current_round, finished: !!data.finished, paused: data.paused || null,
           tstage: data.stage || null, exists: !!r, status: r ? r.status : null, stage: r ? (r.stage || null) : null,
           games, champion: (k && k.champion) || null, last_round: last, heard_champion: heard,
           clip_playing: clip, rcard: card, queue: q, page_now: performance.timeOrigin + performance.now() };
}"""


def rec_cmd(disp, raw):
    return ["ffmpeg", "-hide_banner", "-y", "-f", "x11grab", "-draw_mouse", "0", "-framerate", "30",
            "-video_size", f"{W}x{H}", "-thread_queue_size", "1024", "-i", f"{disp}.0+0,0",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "0", "-pix_fmt", "yuv444p",
            "-fps_mode", "cfr", "-r", "30", str(raw)]


def parse_input_start(logp):
    try:
        txt = Path(logp).read_text(errors="ignore")
    except OSError:
        return None
    m = re.search(r"Input #0, x11grab.*?start: ([0-9.]+)", txt, re.S)
    return float(m.group(1)) if m else None


class Take:
    """One lossless x11grab ffmpeg process writing raw-r<key>.mkv."""

    def __init__(self, run, key):
        self.run, self.key = run, str(key)
        self.raw = run / f"raw-r{self.key}.mkv"
        self.logp = run / f"ffmpeg-rec-r{self.key}.log"
        self.proc = None
        self.popen_epoch = None
        self.input_start = None
        self.rc = None

    def start(self, disp):
        if self.raw.exists():                  # never overwrite a take
            self.raw.rename(self.raw.with_name(f"{self.raw.stem}.prev-{int(time.time())}.mkv"))
        self.popen_epoch = time.time()
        self.proc = subprocess.Popen(rec_cmd(disp, self.raw), stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                     stderr=open(self.logp, "w"))
        (self.run / f"ffmpeg-r{self.key}.pid").write_text(str(self.proc.pid))
        end = time.time() + 6
        while time.time() < end and self.input_start is None and self.proc.poll() is None:
            time.sleep(0.1)
            self.input_start = parse_input_start(self.logp)
        log(f"take {self.key}: ffmpeg pid={self.proc.pid} input_start={self.input_start}")
        return self

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.write(b"q")
                self.proc.stdin.flush()
            except Exception:
                pass
            try:
                self.proc.wait(120)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    self.proc.wait(30)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(10)
        self.rc = self.proc.returncode if self.proc else None
        if self.input_start is None:
            self.input_start = parse_input_start(self.logp)


class SeriesBook:
    """rounds.json (a list of per-round takes) plus the R<key>_REC_DONE markers."""

    def __init__(self, run, ev):
        self.run, self.ev = run, ev
        self.lock = threading.RLock()
        self.entries = []
        self.threads = []

    def save(self):
        with self.lock:
            tmp = self.run / "rounds.json.tmp"
            tmp.write_text(json.dumps(self.entries, indent=2))
            tmp.replace(self.run / "rounds.json")

    def open_entry(self, rnd, take, start_epoch, part=1):
        e = {"round": rnd, "key": take.key, "part": part, "raw": take.raw.name,
             "ffmpeg_popen_epoch": take.popen_epoch, "ffmpeg_input_start": take.input_start,
             "start_epoch": start_epoch, "end_epoch": None, "stop_reason": None, "last": False}
        with self.lock:
            self.entries.append(e)
            self.save()
        return e

    def close(self, take, entry, reason, end_epoch, sync, **extra):
        with self.lock:
            entry.update(end_epoch=end_epoch, stop_reason=reason, **extra)
            self.save()

        def fin():
            take.stop()
            raw_bytes = take.raw.stat().st_size if take.raw.exists() else 0
            with self.lock:
                entry.update(ffmpeg_rc=take.rc, ffmpeg_input_start=take.input_start, stop_epoch=time.time(),
                             raw_bytes=raw_bytes)
                self.save()
            (self.run / f"R{take.key}_REC_DONE").write_text(str(take.rc))
            self.ev.write({"kind": "take_closed", "py": time.time(), "key": take.key, "rc": take.rc,
                           "reason": reason, "raw_bytes": raw_bytes})
            log(f"take {take.key} closed rc={take.rc} reason={reason} bytes={raw_bytes}")

        if sync:
            fin()
        else:
            t = threading.Thread(target=fin, name=f"close-{take.key}")
            t.start()
            self.threads.append(t)

    def join(self):
        for t in self.threads:
            t.join()


class PageCtl:
    """The kiosk page with self-healing: a failed evaluate reloads the page (or relaunches Chrome)."""

    def __init__(self, pw, run, ev, url, disp):
        self.pw, self.run, self.ev, self.url, self.disp = pw, run, ev, url, disp
        self.ctx = self.page = None
        self.armed = False          # true once the first round started: re-assert audio after a reload
        self.last_recover = 0.0
        self.reloads = 0
        self.ctx, self.page = open_page(pw, run, ev, url, disp)

    def evaluate(self, js, arg=None):
        try:
            return self.page.evaluate(js, arg)
        except Exception as e:
            log(f"page.evaluate failed: {str(e)[:200]}")
            self.recover()
            return None

    def recover(self):
        if time.time() - self.last_recover < 10:
            return
        self.last_recover = time.time()
        self.reloads += 1
        self.ev.write({"kind": "page_reload", "py": time.time(), "n": self.reloads})
        try:
            self.page.goto(self.url, wait_until="domcontentloaded", timeout=60000)
            self.page.wait_for_selector("[data-toggle-commentary]", timeout=60000)
            log(f"page reloaded (#{self.reloads})")
        except Exception as e:
            log(f"reload failed ({str(e)[:160]}); relaunching Chrome")
            try:
                self.ctx.close()
            except Exception:
                pass
            try:
                self.ctx, self.page = open_page(self.pw, self.run, self.ev, self.url, self.disp)
                log("Chrome relaunched")
            except Exception as e2:
                log(f"relaunch failed: {str(e2)[:200]}")
                return
        if self.armed:
            self.assert_audio()

    def assert_audio(self):
        """Move sound on, a plain click (unlocks the AudioContext), Commentary on, Auto-focus on."""
        out = {}
        try:
            out["sound_chip"] = ensure_sound_on(self.page)
            self.page.locator("#title").click(timeout=5000)
            out["commentary_chip"] = unmute(self.page)
            out["autofocus_chip"] = ensure_autofocus_on(self.page)
        except Exception as e:
            log(f"assert_audio failed: {str(e)[:200]}")
        return out


def compact_state(st):
    if not st:
        return st
    return {k: st.get(k) for k in ("current", "finished", "paused", "tstage", "exists", "status", "stage",
                                   "champion", "last_round", "heard_champion", "clip_playing", "rcard", "queue")} | {
        "games": [(g["id"], g["status"], g["result"], g["plies"]) for g in st.get("games") or []]}


def wait_series_start(ctl, rnd, wait_change, allow_late, deadline, stop):
    """Same start logic as record mode: the round exists (and, with --wait-change, its games changed)."""
    last, first, frozen = None, True, None
    while True:
        st = ctl.evaluate(POLL_JS, rnd)
        cur = st and st["current"]
        if cur != last:
            log(f"tournament={st and st['id']} current_round={cur} paused={st and st['paused']}")
            last = cur
        if st and st["exists"] and wait_change:
            sig = [(g["id"], g["status"], g["plies"]) for g in st["games"]]
            if frozen is None:
                frozen = sig
                log(f"round {rnd} frozen at {sig}; waiting for the runner to resume")
            if sig != frozen:
                log(f"round {rnd} changed: {sig}")
                return st
        elif st and st["exists"]:
            if first and not allow_late and any((g["plies"] or 0) > 2 for g in st["games"]):
                raise SystemExit(f"round {rnd} is already in progress; arm before it starts or pass --allow-late")
            if tl.round_done(st):
                raise SystemExit(f"round {rnd} is already finished")
            return st
        first = False
        if time.time() > deadline:
            raise SystemExit("round never started")
        if stop["flag"]:
            raise SystemExit("stopped while waiting")
        time.sleep(1)


def do_series(run, url, from_round, max_min, wait_hours, disp_no, allow_late, wait_change):
    disp = f":{disp_no}"
    director_q = deque()
    ev = Events(run / "events.jsonl", on_director=director_q.append)
    stop = {"flag": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))
    if (run / "rounds.json").exists():
        raise SystemExit(f"{run} already holds a series (rounds.json); use a new run dir")
    xv = start_xvfb(run, disp_no)
    book = SeriesBook(run, ev)
    take, entry = None, None
    try:
        with sync_playwright() as pw:
            ctl = PageCtl(pw, run, ev, url, disp)
            log("page open; sound chip: " + ensure_sound_on(ctl.page) + "; commentary chip: "
                + chip_text(ctl.page, "[data-toggle-commentary]") + "; auto-focus chip: " + ensure_autofocus_on(ctl.page))
            st = wait_series_start(ctl, from_round, wait_change, allow_late, time.time() + wait_hours * 3600, stop)
            ev.write({"kind": "round_seen", "py": time.time(), "round": from_round, "state": compact_state(st)})
            rnd, part = from_round, 1
            take = Take(run, str(rnd)).start(disp)
            entry = book.open_entry(rnd, take, take.input_start or take.popen_epoch)
            time.sleep(1.0)
            ctl.armed = True
            chips = ctl.assert_audio()          # unmute only now: no speech budget spent while waiting
            ev.write({"kind": "unmute_click", "py": time.time(), **chips})
            log("armed audio: " + json.dumps(chips))
            series_start = time.time()
            bd = tl.RoundBoundary(rnd)
            next_state_log = 0.0
            while True:
                try:
                    now = time.time()
                    if stop["flag"]:
                        book.close(take, entry, "sigterm", time.time(), sync=True)
                        take = None
                        break
                    if now - series_start > max_min * 60:
                        log("max minutes reached")
                        book.close(take, entry, "max_minutes", time.time(), sync=True)
                        take = None
                        break
                    if not take.alive():
                        # The recorder died mid round: close this part and start a new part of the same round.
                        log(f"ffmpeg of take {take.key} exited (rc={take.proc.returncode}); starting a new part")
                        book.close(take, entry, "ffmpeg_exited", time.time(), sync=True)
                        part += 1
                        take = Take(run, f"{rnd}p{part}").start(disp)
                        entry = book.open_entry(rnd, take, take.input_start or take.popen_epoch, part=part)
                        continue
                    st = ctl.evaluate(POLL_JS, rnd)
                    while director_q:
                        d = director_q.popleft()
                        bd.on_director(d, tl.ev_epoch(d) or time.time())
                    tr = bd.on_poll(st, now)
                    if tr:
                        ev.write({"kind": tr, "round": rnd, "py": now, "state": compact_state(st)})
                        log(f"round {rnd}: {tr}")
                    if now >= next_state_log:
                        free = shutil.disk_usage(run).free
                        skew = round(st["page_now"] / 1000.0 - time.time(), 3) if st and st.get("page_now") else None
                        ev.write({"kind": "state", "py": now, "round": rnd, "state": compact_state(st),
                                  "disk_free_gb": round(free / 1e9, 2), "page_clock_skew_s": skew})
                        if free < 1.5e9:
                            log(f"WARNING: only {free / 1e9:.2f} GB free on the VPS disk")
                        next_state_log = now + 60
                    cut = bd.cut_at(st, now)
                    if cut:
                        cut_epoch, reason = cut
                        while time.time() < cut_epoch:
                            time.sleep(0.05)
                        info = {"stage": bd.stage, "boundary_events": bd.events}
                        if bd.series_over_after(st):
                            log(f"round {rnd} over ({reason}); the tournament is over")
                            book.close(take, entry, reason, time.time(), sync=True, last=True, **info)
                            take = None
                            break
                        nrnd = rnd + 1
                        ntake = Take(run, str(nrnd)).start(disp)       # next take first: no gap
                        boundary = ntake.input_start or ntake.popen_epoch
                        book.close(take, entry, reason, boundary, sync=False, **info)
                        entry = book.open_entry(nrnd, ntake, boundary)
                        ev.write({"kind": "round_switch", "py": time.time(), "from": rnd, "to": nrnd,
                                  "boundary": boundary, "reason": reason})
                        log(f"round {rnd} -> {nrnd} at {boundary:.3f} ({reason})")
                        take, rnd, part = ntake, nrnd, 1
                        bd = tl.RoundBoundary(rnd)
                        continue
                    t = chip_text(ctl.page, "[data-toggle-commentary]")
                    if "muted" in t or "click" in t:
                        ev.write({"kind": "re_unmute", "py": time.time(), "chip": t})
                        ctl.assert_audio()
                except Exception as e:                  # never abort the series because of one failed poll
                    log(f"poll error: {str(e)[:300]}")
                    ev.write({"kind": "poll_error", "py": time.time(), "err": str(e)[:300]})
                time.sleep(1)
            book.join()
            try:
                ctl.ctx.close()
            except Exception:
                pass
    finally:
        if take is not None and entry is not None:
            try:
                book.close(take, entry, "error", time.time(), sync=True)
            except Exception as e:
                log(f"closing take after an error failed: {e}")
        book.join()
        xv.terminate()
        try:
            xv.wait(10)
        except Exception:
            xv.kill()
        log("series done: " + json.dumps([(e["key"], e["stop_reason"]) for e in book.entries]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["test", "record", "series"])
    ap.add_argument("run")
    ap.add_argument("--round", type=int, default=2)
    ap.add_argument("--from-round", type=int, default=None, help="series mode: the first round to record")
    ap.add_argument("--slug", default="")
    ap.add_argument("--port", type=int, default=18770)
    ap.add_argument("--display", type=int, default=97)
    ap.add_argument("--max-min", type=float, default=None, help="cap in minutes (record 75, series 360)")
    ap.add_argument("--wait-hours", type=float, default=12)
    ap.add_argument("--allow-late", action="store_true")
    ap.add_argument("--wait-change", action="store_true",
                    help="the round exists but its runner is stopped: start when its games change (resume)")
    a = ap.parse_args()
    run = Path(a.run).expanduser()
    run.mkdir(parents=True, exist_ok=True)
    url = f"http://127.0.0.1:{a.port}/" + (f"?id={quote(a.slug)}" if a.slug else "")
    if a.mode == "test":
        do_test(run, url, a.display)
    elif a.mode == "series":
        if a.from_round is None:
            raise SystemExit("series mode needs --from-round N")
        do_series(run, url, a.from_round, a.max_min or 360, a.wait_hours, a.display, a.allow_late, a.wait_change)
    else:
        do_record(run, url, a.round, a.max_min or 75, a.wait_hours, a.display, a.allow_late, a.wait_change)


if __name__ == "__main__":
    main()
