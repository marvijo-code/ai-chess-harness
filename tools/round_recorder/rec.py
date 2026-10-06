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
import signal
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import sync_playwright

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
})();
"""


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


class Events:
    def __init__(self, path):
        self.f = open(path, "a", buffering=1)
        self.lock = threading.Lock()

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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["test", "record"])
    ap.add_argument("run")
    ap.add_argument("--round", type=int, default=2)
    ap.add_argument("--slug", default="")
    ap.add_argument("--port", type=int, default=18770)
    ap.add_argument("--display", type=int, default=97)
    ap.add_argument("--max-min", type=float, default=75)
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
    else:
        do_record(run, url, a.round, a.max_min, a.wait_hours, a.display, a.allow_late, a.wait_change)


if __name__ == "__main__":
    main()
