"""The hosted marvijo.com/ai-chess page (tools/aichess_export_page.py) and the local "Published" chip."""

import json
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import aichess_export_page as exporter  # noqa: E402
import llm_tournament_viewer as viewer  # noqa: E402

FLAGS = '<script>window.AICHESS_API_BASE="/api/aichess"; window.AICHESS_HOSTED=true;</script>'


class ExporterTest(unittest.TestCase):
    def setUp(self):
        self.page = exporter.build_page()

    def test_flags_are_set_before_the_main_script(self):
        self.assertEqual(self.page.count(FLAGS), 1)
        self.assertLess(self.page.index(FLAGS), self.page.index("const API_BASE = window.AICHESS_API_BASE"))

    def test_head_has_title_description_og_and_favicon(self):
        self.assertIn("<title>AI Chess Tournament - marvijo.com</title>", self.page)
        self.assertNotIn("<title>AI Chess Swiss</title>", self.page)
        self.assertIn('<meta name="description" content="', self.page)
        self.assertIn('<meta property="og:title" content="AI Chess Tournament - marvijo.com">', self.page)
        self.assertIn('<meta property="og:description" content="', self.page)
        self.assertIn('<link rel="icon" href="/favicon.svg" type="image/svg+xml">', self.page)
        self.assertEqual(len(re.findall(r'property="og:', self.page)), 2, "title + description only")

    def test_every_request_uses_api_base(self):
        # No fetch or audio src may hit /api/... directly: on marvijo.com that is the site's own API root.
        for pattern in (r"""fetch\(\s*[`"']/api""", r"""\.src\s*=\s*[`"']/api""", r"""new Audio\(\s*[`"']/api"""):
            self.assertEqual(re.findall(pattern, self.page), [], pattern)
        for path in ("tournament", "commentary?after=", "commentary/audio/", "thinking?", "analyze?",
                     "commentary/focus", "commentary/tour"):
            self.assertIn("${API_BASE}/api/" + path, self.page, path)

    def test_hosted_mode_never_posts_and_never_tours(self):
        for line in self.page.splitlines():
            if 'method: "POST"' in line:
                self.assertIn("if (!HOSTED)", line, line)
        self.assertIn("if (HOSTED || STREAM) { tourPending = false; return; }", self.page)   # the live stream never tours either
        self.assertIn("setInterval(poll, HOSTED ? 2000 : 1000);", self.page)
        self.assertIn("setInterval(pollCommentary, HOSTED ? 1500 : 1000);", self.page)
        self.assertIn("since=${encodeURIComponent(data.updated_epoch_ms)}", self.page)

    def test_banner_texts(self):
        for needle in ("Live from the tournament", "Offline: showing the last update from", "Final result",
                       "age < 90", "function stateAgeS()"):
            self.assertIn(needle, self.page, needle)

    def test_commentary_is_on_by_default_and_asks_for_one_click(self):
        self.assertIn('let commentaryOn = HOSTED ? store("swissCommentary") !== "off"', self.page)
        self.assertIn("let needGesture = HOSTED && !(navigator.userActivation", self.page)
        self.assertIn("Commentary is on - click anywhere to hear it", self.page)

    def test_engine_analysis_also_covers_replays_and_finished_boards(self):
        # the pusher adds data.eval_track; evalFor must read it before it gives up on /api/analyze
        self.assertIn("function trackEval(game, ply)", self.page)
        self.assertLess(self.page.index("const tracked = trackEval(game, ply);"), self.page.index("if (analyzeOff) return undefined;"))
        self.assertIn('trackMate ? "" : Math.abs(a.mate)', self.page)

    def test_live_eval_holds_the_last_number_while_the_new_position_has_none(self):
        # after every move the engine has no result for the new position for a moment: the bar must stay
        # where it was, not jump to the middle (owner 2026-10-11)
        self.assertIn("const liveEvalHold = {};", self.page)
        self.assertIn("if (cur) { liveEvalHold[game.id] = cur; return cur; }", self.page)
        self.assertIn('{ ...held, best: "", pv: "", depth: 0, hold: true }', self.page)
        self.assertIn("for (const k of Object.keys(liveEvalHold)) delete liveEvalHold[k];", self.page)

    def test_phone_header_is_one_row_of_chips_and_one_row_of_links(self):
        phone = self.page[self.page.index("@media (max-width: 700px) {\n  header { gap: 6px;"):]
        phone = phone[:phone.index("</style>")]
        for needle in (".chips, .hdr-links { flex: none;", "flex-wrap: nowrap;", "overflow-x: auto;",
                       ".ticker { flex-wrap: nowrap;", ".wide { display: none; } .narrow { display: inline; }"):
            self.assertIn(needle, phone, needle)
        self.assertIn('<span><span class="wide">Watch the </span>livestream</span>', self.page)
        self.assertIn('<span class="narrow">Tap to hear the commentary</span>', self.page)

    def test_output_is_deterministic_and_has_no_long_dashes(self):
        self.assertEqual(self.page, exporter.build_page())
        for bad in (chr(0x2014), chr(0x2013)):   # em and en dash
            self.assertNotIn(bad, self.page)

    def test_cli_writes_the_same_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "ai-chess" / "index.html"
            subprocess.run([sys.executable, str(ROOT / "tools" / "aichess_export_page.py"), "--out", str(out)],
                           check=True, capture_output=True)
            self.assertEqual(out.read_bytes(), self.page.encode("utf-8"))

    def test_a_changed_page_shape_fails_loudly(self):
        with self.assertRaises(ValueError):
            exporter.build_page(page="<html><title>Other</title><script>x</script></html>")


class LocalPageTest(unittest.TestCase):
    def test_local_page_is_not_hosted_by_default(self):
        page = viewer.PAGE
        self.assertNotIn("window.AICHESS_HOSTED=true", page)
        self.assertNotIn("window.AICHESS_API_BASE=", page)
        self.assertIn('const API_BASE = window.AICHESS_API_BASE || "";', page)
        self.assertIn("const HOSTED = !!window.AICHESS_HOSTED;", page)
        self.assertIn("<title>AI Chess Swiss</title>", page)
        # the banner exists only when hosted; the publish chip only when not hosted
        self.assertIn("if (HOSTED) {\n  const banner", page)
        self.assertIn("const p = !HOSTED && data ? data.publish : null;", page)


class PhoneNameWrapTest(unittest.TestCase):
    """On a phone a player's name breaks between words only (owner 2026-10-11: "DeepSee / k V4.1 Flash")."""

    def setUp(self):
        self.page = viewer.PAGE
        self.css = self.page[self.page.index("<style>"):self.page.index("</style>")]
        marker = "/* ---- phones: a player's name breaks between words only"
        self.assertEqual(self.css.count(marker), 1)
        self.phone = self.css[self.css.index(marker):]

    def block(self, width):
        start = self.phone.index(f"@media (max-width: {width}px) {{")
        return self.phone[start:self.phone.index("\n}", start)]

    def test_the_phone_rules_are_last_and_only_for_phones(self):
        # nothing after them can win over them, and no rule in them is outside a phone media query
        self.assertEqual(re.findall(r"@media \(max-width: (\d+)px\)", self.phone), ["700", "520"])
        outside = re.sub(r"@media \(max-width: \d+px\) \{.*?\n\}", "", self.phone, flags=re.S)
        self.assertEqual(re.sub(r"/\*.*?\*/", "", outside, flags=re.S).strip(), "")
        self.assertNotIn("body.stream", self.phone)

    def test_names_break_between_words_only(self):
        phone = self.block(700)
        names = ".pbar .name, .rc-m .pl, .rc-r .n, .bk-name, .pair .w, .pair .b, .lb-n"
        self.assertIn(names + " { overflow-wrap: break-word; word-break: normal; }", phone)
        self.assertNotIn("anywhere", self.phone)
        # every name column keeps the room of its longest word
        self.assertIn(".pbar .name { flex: 1 1 0; min-width: min-content; }", phone)
        for row in (".rc-m", ".rc-r", ".bk-p", ".pair", ".lb-r"):
            self.assertRegex(phone, re.escape(row) + r" \{ grid-template-columns: [^}]*minmax\(min-content, 1fr\)")
        self.assertIn(".rc-r { grid-template-columns: 20px minmax(min-content, 1fr) 58px 52px;", self.block(520))
        self.assertIn(".pair { grid-template-columns: minmax(min-content, 1fr) 46px minmax(min-content, 1fr); }", self.block(520))

    def test_the_status_line_sits_under_the_name_and_nothing_overlaps(self):
        phone = self.block(700)
        self.assertIn(".pbar { flex-wrap: wrap;", phone)
        self.assertIn(".pbar .think { order: 1; flex: 0 0 100%; min-width: 0;", phone)
        # a live game keeps the line (the board does not jump on every move), a finished game drops it
        self.assertIn(".pbar.live .think { min-height: 15px; }", phone)
        self.assertIn(".pbar:not(.live) .think:empty { display: none; }", phone)
        self.assertIn('el.classList.toggle("live", live);', self.page)
        # the board label of a round card gets its own line, the bracket's semifinals stack
        self.assertIn(".rc-m .bd { grid-column: 1 / -1; white-space: nowrap; }", phone)
        self.assertIn(".bk-semis { grid-template-columns: minmax(0, 1fr); }", self.block(520))
        # the standings table scrolls inside its card instead of widening the page
        self.assertIn("#standings { overflow-x: auto; }", phone)

    def test_a_word_with_a_hyphen_stays_whole_on_phones_only(self):
        self.assertIn(".nb { white-space: nowrap; }", self.block(700))
        self.assertEqual(self.css.count(".nb {"), 1)
        self.assertIn('function nbw(text) { return esc(text).replace(/[^\\s-]*-\\S*/g, m => `<span class="nb">${m}</span>`); }', self.page)
        self.assertIn('<span class="name" title="${esc(route(name))}">${nbw(name)}${ladderTag(name, game)}</span>', self.page)
        self.assertIn("</span>${nbw(text.slice(2))}`;", self.page)

    def test_wide_screens_keep_their_rules(self):
        wide = self.css[:self.css.index("/* ---- phones: a player's name breaks between words only")]
        self.assertIn(".pbar { display: flex; align-items: center; gap: 10px;", wide)
        self.assertIn(".pbar .name { font-weight: 600; flex: 1; min-width: 0; white-space: normal; overflow-wrap: anywhere; line-height: 1.2; }", wide)
        self.assertIn(".pbar .think { font-size: 12px; color: var(--accent); min-width: 88px; text-align: right;", wide)
        self.assertIn(".rc-m { display: grid; grid-template-columns: minmax(76px, 6.4em) minmax(0, 1fr) auto minmax(0, 1fr);", wide)

    def test_the_hosted_page_carries_the_same_rules(self):
        self.assertIn(self.phone, exporter.build_page())


class PublishStatusTest(unittest.TestCase):
    def serve(self, publish=None, raw=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        live = Path(tmp.name)
        state = live / "t1-tournament.json"
        state.write_text(json.dumps({"id": "t1", "games": {}, "updated_epoch_ms": int(time.time() * 1000)}), encoding="utf-8")
        status = live / "t1-publish-status.json"
        if raw is not None:
            status.write_text(raw, encoding="utf-8")
        elif publish is not None:
            status.write_text(json.dumps(publish), encoding="utf-8")

        class H(viewer.Handler):
            pass

        H.state_path, H.live_dir = state, live
        H.analyzer = H.annotator = H.commentator = None
        server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}/api/tournament", timeout=5) as res:
            return json.loads(res.read())

    def test_no_status_file_no_publish_field(self):
        self.assertNotIn("publish", self.serve())

    def test_status_file_adds_publish(self):
        pushed = int(time.time() * 1000) - 5000
        state = self.serve({"ok": True, "relay_up": True, "last_state_push_epoch_ms": pushed, "clips_pushed": 12,
                            "error": None, "relay_url": "http://10.0.0.1:8780", "token": "x"})
        pub = state["publish"]
        self.assertTrue(pub["ok"])
        self.assertTrue(pub["relay_up"])
        self.assertEqual(pub["clips_pushed"], 12)
        self.assertEqual(pub["last_state_push_epoch_ms"], pushed)
        self.assertGreaterEqual(pub["push_age_s"], 4)
        self.assertLess(pub["push_age_s"], 60)
        self.assertEqual(pub["url"], "https://marvijo.com/ai-chess")
        # unknown fields (a relay address, a token) never reach the page
        self.assertNotIn("relay_url", pub)
        self.assertNotIn("token", pub)

    def test_unreadable_status_file_reports_offline(self):
        pub = self.serve(raw="{not json")["publish"]
        self.assertFalse(pub["ok"])
        self.assertIn("unreadable", pub["error"])


if __name__ == "__main__":
    unittest.main()
