"""Stream auto-focus in a real headless Chromium at 1920x1080 (the YouTube stream's screen).

Two live games: the camera rotates between them inside the dwell limits. One live game: it stays on that
board. No game live: all boards. A new tournament behind the --follow pointer: no page reload. The focused
layout fits the screen with nothing cut off. Short limits (?dwell=6&minDwell=2) keep the test quick.
"""
import glob
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import llm_tournament_viewer as viewer  # noqa: E402

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None

DWELL_MS, MIN_DWELL_MS, LINGER_MS = 6000, 2000, 8000
PLAYERS = ["Claude Opus 5.5", "GPT-6.1 Sol", "DeepSeek V4.1 Flash", "Stockfish 19"]
START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
AFTER_E4_E5_FEN = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq e6 0 2"


def chromium_candidates() -> list[str]:
    env = os.environ.get("AICHESS_E2E_CHROMIUM")
    roots = [Path.home() / "AppData/Local/ms-playwright", Path.home() / ".cache/ms-playwright"]
    found: list[str] = [env] if env else []
    for root in roots:
        for pattern in ("chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell*", "chromium-*/chrome-*/chrome", "chromium-*/chrome-*/chrome.exe"):
            hits = [h for h in glob.glob(str(root / pattern)) if os.path.isfile(h)]
            found += sorted(hits, key=lambda h: int("".join(c for c in Path(h).parts[-3].split("-")[-1] if c.isdigit()) or 0), reverse=True)
    return found


def launch(p):
    try:
        return p.chromium.launch()
    except Exception as first:  # the pinned browser build is missing: use any installed Playwright Chromium
        for exe in chromium_candidates():
            try:
                return p.chromium.launch(executable_path=exe)
            except Exception:
                continue
        raise unittest.SkipTest(f"no Chromium for Playwright: {str(first)[:200]}")


def game(gid: str, board: int, white: str, black: str, status: str) -> dict:
    now = int(time.time() * 1000)
    moves = [
        {"ply": 1, "side": "white", "san": "e4", "uci": "e2e4", "elapsed_ms": 4000, "tries": 1, "comment": "Centre first."},
        {"ply": 2, "side": "black", "san": "e5", "uci": "e7e5", "elapsed_ms": 5000, "tries": 1, "comment": "Mirror the centre."},
    ]
    g = {"id": gid, "round": 1, "board": board, "white": white, "black": black, "status": status, "moves": moves,
         "fen": AFTER_E4_E5_FEN, "start": START_FEN, "clocks": {"white": 880000, "black": 870000},
         "result": "1-0" if status == "finished" else "*", "termination": "White won on time" if status == "finished" else ""}
    if status == "live":
        g["thinking"] = {"side": "white", "since_epoch_ms": now - 60000}
    return g


def state(tid: str, title: str, statuses: dict[str, str]) -> dict:
    pairs = {"r1b1": (PLAYERS[0], PLAYERS[1], 1), "r1b2": (PLAYERS[2], PLAYERS[3], 2)}
    games = {gid: game(gid, pairs[gid][2], pairs[gid][0], pairs[gid][1], st) for gid, st in statuses.items()}
    standings = [{"rank": i + 1, "name": n, "points": 0, "elo": 1500, "elo_delta": 0, "wins": 0, "draws": 0, "losses": 0,
                  "forfeits": 0, "flags": 0, "invalid_attempts": 0, "played": 0} for i, n in enumerate(PLAYERS)]
    return {
        "id": tid, "title": title, "current_round": 1,
        "config": {"rounds": 3, "timeControlMs": 900000, "incrementMs": 10000, "maxAttempts": 3, "startElo": 1500, "eloK": 32},
        "players": [{"name": n, "route": "subscription"} for n in PLAYERS],
        "standings": standings,
        "rounds": [{"round": 1, "status": "live", "pairings": [
            {"board": b, "white": w, "black": bl, "game_id": gid} for gid, (w, bl, b) in pairs.items()]}],
        "games": games,
        "latest_notes": {n: {"note": f"{n}: keep the centre, watch the clock, trade when ahead.", "game": "r1b1", "ply": 2} for n in PLAYERS},
        "cache_stats": {n: {"hit_rate": 0.9} for n in PLAYERS},
    }


GEOMETRY_JS = """() => {
  const vw = innerWidth, vh = innerHeight, bad = [];
  const want = ['header', '.boards > .card.focused', '.card.focused .game-head', '.card.focused .pbar', '.card.focused .board-wrap',
    '.card.focused .comment', '.card.focused .lstrip', '#standingsCard', '#standings tr:last-child', '#agentsCard', '#agents .ag:last-child'];
  for (const q of want) {
    const els = document.querySelectorAll(q);
    if (!els.length) { bad.push(q + ': missing'); continue; }
    for (const el of els) {
      if (el.offsetParent === null && getComputedStyle(el).display === 'none') continue;
      const r = el.getBoundingClientRect();
      if (!r.width || !r.height) bad.push(q + ': not shown');
      else if (r.bottom > vh + 0.5 || r.right > vw + 0.5 || r.left < -0.5 || r.top < -0.5) bad.push(`${q}: ${r.left.toFixed(0)},${r.top.toFixed(0)},${r.right.toFixed(0)},${r.bottom.toFixed(0)}`);
    }
  }
  return { bad, sw: document.documentElement.scrollWidth, sh: document.documentElement.scrollHeight, vw, vh };
}"""
FOCUSED_JS = "() => { const c = document.querySelector('.boards > .card.focused'); return c ? c.dataset.game : null; }"


@unittest.skipIf(sync_playwright is None, "playwright is not installed")
class StreamAutoFocusE2E(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.pointer = self.dir / "current.json"

        class H(viewer.Handler):
            pass

        H.state_path, H.follow, H.live_dir, H._follow_cache = None, self.pointer, self.dir, {}
        H.analyzer = H.annotator = H.commentator = None
        self.publish("t1", "Autofocus test #1", {"r1b1": "live", "r1b2": "live"})
        server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.base = f"http://127.0.0.1:{server.server_address[1]}"

    def publish(self, tid: str, title: str, statuses: dict[str, str]) -> None:
        path = self.dir / f"{tid}-tournament.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state(tid, title, statuses)), encoding="utf-8")
        os.replace(tmp, path)
        ptr = self.pointer.with_suffix(".tmp")
        ptr.write_text(json.dumps({"state_path": str(path), "id": tid}), encoding="utf-8")
        os.replace(ptr, self.pointer)

    def sample(self, page, seconds: float) -> list:
        out, end = [], time.time() + seconds
        while time.time() < end:
            out.append(page.evaluate(FOCUSED_JS))
            page.wait_for_timeout(250)
        return out

    def assert_fits(self, page, name: str):
        shots = os.environ.get("AICHESS_E2E_SHOTS")   # optional: keep a 1920x1080 picture of each checked layout
        if shots:
            Path(shots).mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(Path(shots) / f"stream-autofocus-{name}.png"))
        geo = page.evaluate(GEOMETRY_JS)
        self.assertEqual(geo["bad"], [], geo)
        self.assertLessEqual(geo["sw"], geo["vw"], geo)
        self.assertLessEqual(geo["sh"], geo["vh"], geo)

    def test_stream_auto_focus(self):
        with sync_playwright() as p:
            browser = launch(p)
            try:
                self.run_stream(browser)
            finally:
                browser.close()

    def run_stream(self, browser):
        page = browser.new_page(viewport={"width": 1920, "height": 1080})
        # an old "off" in the profile must not matter in stream mode
        page.add_init_script("try { localStorage.setItem('swissAutoFocus', 'off'); } catch (e) {}")
        page.goto(f"{self.base}/?stream=1&dwell={DWELL_MS // 1000}&minDwell={MIN_DWELL_MS // 1000}", wait_until="domcontentloaded")
        page.wait_for_function("document.querySelector('.boards > .card.focused') !== null", timeout=15000)
        self.assertIn("Auto-focus: on", page.inner_text("[data-toggle-autofocus]"))
        page.evaluate("window.__sameDocument = 1")

        # 1) two live games: rotation, every completed dwell inside [min, max] (1 s poll granularity)
        seen = self.sample(page, 20)
        self.assertEqual(set(seen), {"r1b1", "r1b2"}, seen)
        log = page.evaluate("window.AICHESS_FOCUS_LOG")
        switches = [e["t"] for e in log if e["id"]]
        dwells = [b - a for a, b in zip(switches, switches[1:])]
        self.assertGreaterEqual(len(dwells), 2, log)
        self.assertLessEqual(max(dwells), DWELL_MS + 1200, dwells)
        self.assertGreaterEqual(min(dwells), MIN_DWELL_MS, dwells)
        # the strip shows the other live board and the countdown
        self.assertIn("Also live", page.text_content(".card.focused .lstrip"))
        self.assertEqual(page.locator(".card.focused .ls-g").count(), 1)
        self.assert_fits(page, "two-live")

        # 2) one live game: the camera goes there (after the finished board's result) and stays
        self.publish("t1", "Autofocus test #1", {"r1b1": "live", "r1b2": "finished"})
        page.wait_for_function("document.querySelector('.boards > .card.focused')?.dataset.game === 'r1b1'", timeout=LINGER_MS + 4000)
        stay = self.sample(page, 3 * DWELL_MS / 1000)
        self.assertEqual(set(stay), {"r1b1"}, stay)
        self.assertIn("the only game still playing", page.text_content(".card.focused .lstrip"))
        self.assert_fits(page, "one-live")

        # 3) no game live: all boards, no stale focus
        self.publish("t1", "Autofocus test #1", {"r1b1": "finished", "r1b2": "finished"})
        page.wait_for_function("document.querySelector('.boards > .card.focused') === null", timeout=LINGER_MS + 4000)
        self.assertFalse(page.evaluate("document.body.classList.contains('focus-mode')"))
        self.assertEqual(page.locator(".boards > .card[data-game]").count(), 2)

        # 4) the pointer moves to a new tournament (same game ids): followed without a reload
        self.publish("t2", "Autofocus test #2", {"r1b1": "live", "r1b2": "pending"})
        page.wait_for_function("document.title.includes('Autofocus test #2') && document.querySelector('.boards > .card.focused')?.dataset.game === 'r1b1'", timeout=10000)
        self.assertEqual(page.evaluate("window.__sameDocument"), 1)
        self.assert_fits(page, "new-tournament")


if __name__ == "__main__":
    unittest.main()
