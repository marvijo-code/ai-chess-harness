"""Earlier tournaments, maker logos, the livestream link and the "What each AI has learnt" panel (viewer side)."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import aichess_export_page as exporter  # noqa: E402
import aichess_logos as logos  # noqa: E402
import llm_tournament_viewer as viewer  # noqa: E402

NODE = shutil.which("node")
PLAYERS = [
    {"name": "Sonnet 5.5", "memoryFolder": "claude", "provider": "claude", "model": "claude-sonnet-5-5"},
    {"name": "GPT-6.1 Sol", "memoryFolder": "gpt", "provider": "codex", "model": "gpt-6.1-sol"},
    {"name": "Stockfish 19", "provider": "uci", "model": "stockfish-19"},
]


def make_state(tid: str, number: int, finished: bool, updated_ms: int, champion: str = "") -> dict:
    moves = [{"uci": "e2e4"}, {"uci": "e7e5"}, {"uci": "g1f3"}]
    return {
        "id": tid, "number": number, "title": f"Test Cup #{number}", "finished": finished, "winner": champion or None,
        "knockout": {"champion": champion} if champion else None,
        "updated_epoch_ms": updated_ms, "created_at": "2026-10-08T10:00:00+00:00", "updated_at": "2026-10-08T12:00:00+00:00",
        "players": PLAYERS, "rounds": [{"round": 1, "pairings": [{"game_id": "r1b1", "white": "Sonnet 5.5", "black": "GPT-6.1 Sol"}]}],
        "config": {"forever": {"memoryRepo": "/nowhere", "memoryRemote": "git@github-aichess:someone/agent-memory.git"}},
        "games": {"r1b1": {"id": "r1b1", "status": "finished" if finished else "live", "result": "1-0" if finished else "*",
                           "white": "Sonnet 5.5", "black": "GPT-6.1 Sol", "moves": moves}},
    }


class LiveDir(unittest.TestCase):
    """A live dir with one finished tournament (#1, with a marks sidecar) and the live one (#2)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.live = Path(tmp.name) / "live"
        self.live.mkdir()
        now_ms = int(time.time() * 1000)
        self.old = self.live / "cup-0001-tournament.json"
        self.old.write_text(json.dumps(make_state("cup-0001", 1, True, now_ms - 7_200_000, "Sonnet 5.5")), encoding="utf-8")
        os.utime(self.old, (time.time() - 7200, time.time() - 7200))
        self.cur = self.live / "cup-0002-tournament.json"
        self.cur.write_text(json.dumps(make_state("cup-0002", 2, False, now_ms)), encoding="utf-8")
        import chess
        board = chess.Board()
        positions = {board.fen(): {"cp": 20, "second": 15, "best": "e2e4"}}
        for uci, cp in (("e2e4", -20), ("e7e5", 25), ("g1f3", -25)):
            board.push_uci(uci)
            positions[board.fen()] = {"cp": cp, "second": cp - 5, "best": "g8f6"}
        (self.live / "cup-0001-annotations.json").write_text(
            json.dumps({"engine": "Stockfish", "depth": 99, "positions": positions}), encoding="utf-8")
        self.repo = Path(tmp.name) / "memory"
        for folder, text in (("claude", "# Chess memory\n\n## Rules\n- Guard the queen.\n"), ("gpt", "# Chess memory\n- Castle early.\n"),
                             ("glm", "# Chess memory\n- Old lesson.\n")):
            (self.repo / "agents" / folder / "notes").mkdir(parents=True)
            (self.repo / "agents" / folder / "MEMORY.md").write_text(text, encoding="utf-8")
            (self.repo / "agents" / folder / "notes" / "openings.md").write_text("x", encoding="utf-8")
        viewer._list_cache.update(at=0.0, dir=None, rows=[])
        viewer._memory_cache.update(at=0.0, key=None, payload=None)

    def serve(self):
        class H(viewer.Handler):
            pass

        H.state_path, H.follow, H.live_dir = None, None, self.live
        H.analyzer = H.annotator = H.commentator = None
        H.memory_repo, H.memory_url, H.live_url = self.repo, "", "https://example.test/live"
        server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.base = f"http://127.0.0.1:{server.server_address[1]}"

    def get(self, path: str):
        with urllib.request.urlopen(self.base + path, timeout=5) as res:
            return json.loads(res.read())


class TournamentListTest(LiveDir):
    def test_summary_fields(self):
        row = viewer.tournament_summary(json.loads(self.old.read_text(encoding="utf-8")))
        self.assertEqual((row["id"], row["number"], row["finished"], row["champion"], row["games"], row["rounds"]),
                         ("cup-0001", 1, True, "Sonnet 5.5", 1, 1))
        self.assertEqual(row["players"], ["Sonnet 5.5", "GPT-6.1 Sol", "Stockfish 19"])

    def test_a_live_game_is_not_counted_as_played(self):
        self.assertEqual(viewer.tournament_summary(json.loads(self.cur.read_text(encoding="utf-8")))["games"], 0)

    def test_list_is_newest_first_and_skips_broken_files(self):
        (self.live / "broken-tournament.json").write_text("{not json", encoding="utf-8")
        (self.live / "cup-0001-tournament.json.bak-before-fix").write_text("{}", encoding="utf-8")
        rows = viewer.list_tournaments(self.live, now=1.0)
        self.assertEqual([r["id"] for r in rows], ["cup-0002", "cup-0001"])

    def test_list_is_cached_then_refreshed(self):
        viewer.list_tournaments(self.live, now=100.0)
        self.old.unlink()
        self.assertEqual(len(viewer.list_tournaments(self.live, now=100.0 + viewer.TOURNAMENT_LIST_TTL_S - 1)), 2)
        self.assertEqual(len(viewer.list_tournaments(self.live, now=100.0 + viewer.TOURNAMENT_LIST_TTL_S + 1)), 1)

    def test_api_lists_all_and_names_the_live_one(self):
        self.serve()
        data = self.get("/api/tournaments")
        self.assertEqual(data["current"], "cup-0002")
        self.assertEqual([t["id"] for t in data["tournaments"]], ["cup-0002", "cup-0001"])

    def test_an_earlier_tournament_is_served_as_an_archive_with_its_saved_marks(self):
        self.serve()
        old = self.get("/api/tournament?id=cup-0001")
        self.assertTrue(old["archived"])
        self.assertFalse(old["commentary"])
        self.assertEqual(old["title"], "Test Cup #1 - By Marvijo")
        self.assertEqual(old["live_url"], "https://example.test/live")
        self.assertIn("r1b1", old["annotations"])
        self.assertNotIn("publish", old)

    def test_the_live_tournament_is_never_an_archive(self):
        self.serve()
        self.assertNotIn("archived", self.get("/api/tournament"))
        self.assertNotIn("archived", self.get("/api/tournament?id=cup-0002"))

    def test_a_bad_or_unknown_id_is_refused(self):
        self.serve()
        for path, code in (("/api/tournament?id=..%2F..%2Fsecret", 400), ("/api/tournament?id=nope", 404)):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.get(path)
            self.assertEqual(caught.exception.code, code, path)

    def test_archived_marks_need_no_engine_and_survive_a_missing_sidecar(self):
        state = json.loads(self.old.read_text(encoding="utf-8"))
        marks = viewer.archived_annotations(self.old, state)
        self.assertEqual(set(marks), {"r1b1"})
        self.assertEqual(viewer.archived_annotations(self.cur, json.loads(self.cur.read_text(encoding="utf-8"))), {})


class MemoryPanelTest(LiveDir):
    def test_remote_to_public_address(self):
        self.assertEqual(viewer.memory_repo_url("git@github-aichess:someone/agent-memory.git"), "https://github.com/someone/agent-memory")
        self.assertEqual(viewer.memory_repo_url("https://github.com/someone/agent-memory"), "https://github.com/someone/agent-memory")
        self.assertEqual(viewer.memory_repo_url("git@example.org:someone/repo.git"), "")
        self.assertEqual(viewer.memory_repo_url(None), "")

    def test_payload_names_players_by_family_folder(self):
        state = json.loads(self.cur.read_text(encoding="utf-8"))
        data = viewer.memory_payload(self.repo, state, now=1.0)
        self.assertEqual(data["repo_url"], "https://github.com/someone/agent-memory")
        rows = {p["folder"]: p for p in data["players"]}
        self.assertEqual(set(rows), {"claude", "gpt", "glm"})          # the engine has no memory
        self.assertEqual((rows["claude"]["name"], rows["claude"]["playing"], rows["claude"]["notes"]), ("Sonnet 5.5", True, 1))
        self.assertEqual((rows["glm"]["name"], rows["glm"]["playing"]), ("glm", False))
        self.assertIn("Guard the queen.", rows["claude"]["text"])
        self.assertEqual(rows["gpt"]["url"], "https://github.com/someone/agent-memory/tree/main/agents/gpt")
        self.assertEqual([p["playing"] for p in data["players"]], [True, True, False])   # players first

    def test_no_repository_gives_an_empty_list(self):
        self.assertEqual(viewer.memory_payload(None, {"config": {}}, now=2.0)["players"], [])
        self.assertEqual(viewer.memory_payload(Path(self.live) / "missing", {}, now=3.0)["players"], [])

    def test_api_memory(self):
        self.serve()
        data = self.get("/api/memory")
        self.assertEqual(len(data["players"]), 3)


class PageTest(unittest.TestCase):
    def setUp(self):
        self.page = viewer.PAGE
        self.hosted = exporter.build_page()

    def test_sprite_is_in_the_page_once_with_every_logo(self):
        self.assertNotIn("<!--LOGO-SPRITE-->", self.page)
        self.assertNotIn("__MAKER_LABELS__", self.page)
        self.assertEqual(self.page.count('id="logoSprite"'), 1)
        for key in logos.LABELS:
            self.assertEqual(self.page.count(f'<symbol id="lg-{key}"'), 1, key)
        for key in ("anthropic", "openai", "deepseek", "google", "zai", "xai", "stockfish"):
            self.assertIn(key, logos.LABELS)

    def test_sprite_holds_shapes_only(self):
        sprite = logos.sprite().lower()
        for bad in ("<script", "onload=", "onclick=", "javascript:", "<foreignobject", "http://", "https://"):
            self.assertNotIn(bad, sprite.replace('xmlns="http://www.w3.org/2000/svg"', ""), bad)

    def test_new_requests_use_api_base_and_never_post(self):
        for path in ("tournaments", "memory"):
            self.assertIn("${API_BASE}/api/" + path + "`", self.hosted, path)
        self.assertIn("if (HOSTED && isArchive()) return;", self.hosted)   # no thinking text of the wrong tournament

    def test_page_has_the_link_the_picker_and_the_panel_and_the_stream_hides_them(self):
        for needle in ('id="liveLink"', "Watch the livestream", 'id="tourSelect"', 'id="learnt"', "What each AI has learnt",
                       "Back to the live tournament",
                       "body.stream .hdr-links, body.stream .extras, body.stream .archive-bar { display: none !important; }"):
            self.assertIn(needle, self.page, needle)
        self.assertNotIn("location.reload", self.page)

    def test_an_archive_is_loaded_once_and_stays_quiet(self):
        for needle in ("if (want && isArchive()) return;", "if (isArchive()) return;\n  if (!autoFocus",
                       "if (!data || !data.commentary || !commentaryOn || isArchive()) return;"):
            self.assertIn(needle, self.page, needle)

    def test_the_stream_never_starts_pinned_to_a_restored_focus_address(self):
        page = self.page
        drop = 'if (STREAM && focusId) { focusId = null;'
        self.assertIn(drop, page)
        self.assertLess(page.index(drop), page.index("if (focusId) { focusPinned = true; autoFocus = false; }"))
        self.assertIn("if (autoFocus && !STREAM) { autoFocus = false;", page)

    def run_js(self, source: str, body: str):
        out = subprocess.run([NODE, "-e", source + "\n" + body], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    @unittest.skipUnless(NODE, "node is not installed")
    def test_maker_rules(self):
        page = self.page
        source = page[page.index("// MAKER-OF-START"):page.index("// MAKER-OF-END")]
        names = {
            "Sonnet 5.5 claude-sonnet-5-5": "anthropic", "GPT-6.1 Sol gpt-6.1-sol": "openai", "DeepSeek V4.1 Flash deepseek-v4.1-flash": "deepseek",
            "GLM 5.3 Flash glm-5.3-flash": "zai", "Gemini 3.8 Flash gemini-3.8-flash": "google", "Grok 4.7 grok-4.7": "xai",
            "Stockfish 19 stockfish-19": "stockfish", "Qwen 4 Max ": "qwen", "Kimi K3 ": "moonshot", "MiMo V2.6 Pro ": "xiaomi",
            "claude ": "anthropic", "gpt ": "openai", "glm ": "zai", "gemini ": "google", "deepseek ": "deepseek",
            "Mystery Model X ": "",
        }
        got = self.run_js(source, f"console.log(JSON.stringify({json.dumps(list(names))}.map(makerKey)));")
        self.assertEqual(dict(zip(names, got)), names)
        self.assertTrue(set(got) - {""} <= set(logos.LABELS))

    @unittest.skipUnless(NODE, "node is not installed")
    def test_memory_markdown_is_escaped(self):
        page = self.page
        source = page[page.index("// MEMORY-MD-START"):page.index("// MEMORY-MD-END")]
        text = "# Chess memory\n\n## Rules <b>\n- Never play `Qxb6??` <img src=x onerror=alert(1)>\n- **Scan** checks\n\nPlain <script>x</script> line\n"
        html = self.run_js(source, f"console.log(JSON.stringify(memoryHtml({json.dumps(text)})));")
        self.assertEqual(html, "<h3>Rules &lt;b&gt;</h3><ul><li>Never play <code>Qxb6??</code> &lt;img src=x onerror=alert(1)&gt;</li>"
                               "<li><b>Scan</b> checks</li></ul><p>Plain &lt;script&gt;x&lt;/script&gt; line</p>")


if __name__ == "__main__":
    unittest.main()
