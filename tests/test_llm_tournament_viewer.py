import json
import math
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import llm_tournament_viewer as viewer  # noqa: E402


class WinPercentTest(unittest.TestCase):
    def test_even_position_is_fifty(self):
        self.assertAlmostEqual(viewer.win_percent(0), 50.0)

    def test_matches_lichess_formula(self):
        for cp in (-700, -250, -40, 35, 120, 480):
            expected = 50 + 50 * (2 / (1 + math.exp(-0.00368208 * cp)) - 1)
            self.assertAlmostEqual(viewer.win_percent(cp), expected)

    def test_symmetric(self):
        self.assertAlmostEqual(viewer.win_percent(300) + viewer.win_percent(-300), 100.0)

    def test_clamped_at_1000(self):
        self.assertAlmostEqual(viewer.win_percent(5000), viewer.win_percent(1000))
        self.assertAlmostEqual(viewer.win_percent(-viewer.MATE_CP), viewer.win_percent(-1000))
        self.assertLess(viewer.win_percent(1000), 100.0)

    def test_mate_scores(self):
        self.assertEqual(viewer.score_cp(None, 3), viewer.MATE_CP)
        self.assertEqual(viewer.score_cp(None, -2), -viewer.MATE_CP)
        self.assertEqual(viewer.score_cp(None, 0), -viewer.MATE_CP)   # side to move is mated
        self.assertEqual(viewer.score_cp(57, None), 57)


def cp_for_win(target: float) -> float:
    """Inverse of win_percent, to build exact drops in the tests."""
    x = (target - 50) / 50
    return -math.log(2 / (x + 1) - 1) / 0.00368208


class ClassifyMoveTest(unittest.TestCase):
    def drop(self, before_win: float, drop: float, played="e2e4", best="d2d4") -> str:
        before = cp_for_win(before_win)
        after = cp_for_win(before_win - drop)
        return viewer.classify_move(before, None, best, played, after)

    def test_thresholds(self):
        self.assertEqual(self.drop(60, 35), "??")
        self.assertEqual(self.drop(60, 30.01), "??")
        self.assertEqual(self.drop(60, 29.9), "?")
        self.assertEqual(self.drop(60, 20.01), "?")
        self.assertEqual(self.drop(60, 19.9), "?!")
        self.assertEqual(self.drop(60, 10.01), "?!")
        self.assertEqual(self.drop(60, 9.9), "")
        self.assertEqual(self.drop(60, -5), "")   # improving on the engine's line is not an error

    def test_engine_best_move_is_never_an_error(self):
        self.assertEqual(self.drop(60, 35, played="d2d4", best="d2d4"), "")

    def test_good_move_needs_best_move_and_a_gap(self):
        before = cp_for_win(55)
        bad_second = cp_for_win(44)   # 11 win% worse
        close_second = cp_for_win(50)  # 5 win% worse
        self.assertEqual(viewer.classify_move(before, bad_second, "g1f3", "g1f3", before), "!")
        self.assertEqual(viewer.classify_move(before, close_second, "g1f3", "g1f3", before), "")
        self.assertEqual(viewer.classify_move(before, bad_second, "g1f3", "b1c3", before), "")   # not the best move
        self.assertEqual(viewer.classify_move(before, None, "g1f3", "g1f3", before), "")          # only one legal move

    def test_good_move_not_in_decided_positions(self):
        winning = cp_for_win(95)
        losing = cp_for_win(5)
        self.assertEqual(viewer.classify_move(winning, cp_for_win(60), "a1a8", "a1a8", winning), "")
        self.assertEqual(viewer.classify_move(losing, -viewer.MATE_CP, "a1a8", "a1a8", losing), "")
        edge = cp_for_win(89.5)
        self.assertEqual(viewer.classify_move(edge, cp_for_win(70), "a1a8", "a1a8", edge), "!")

    def test_mate_in_after_position(self):
        # Mover had +2.00 and walked into a forced mate: a blunder.
        self.assertEqual(viewer.classify_move(200, 150, "d2d4", "e2e4", -viewer.MATE_CP), "??")

    def test_only_standard_symbols(self):
        seen = set()
        for before in range(-1200, 1201, 150):
            for after in range(-1200, 1201, 150):
                seen.add(viewer.classify_move(before, before - 300, "a", "b", after))
                seen.add(viewer.classify_move(before, before - 300, "a", "a", after))
        self.assertTrue(seen <= {"", "??", "?", "?!", "!"})


class AnnotatePliesTest(unittest.TestCase):
    def test_marks_by_ply_from_side_to_move_scores(self):
        # Position i is after i plies; cp is from the side to move's point of view.
        records = [
            {"cp": 20, "second": 10, "best": "e2e4", "over": False},     # white to move
            {"cp": -30, "second": -40, "best": "e7e5", "over": False},   # black to move (white +0.30)
            {"cp": 600, "second": 580, "best": "d2d4", "over": False},   # white to move: black blundered
            None,                                                        # not analysed yet
        ]
        marks = viewer.annotate_plies(["e2e4", "g8h6", "d2d4"], records)
        self.assertNotIn("1", marks)
        self.assertEqual(marks.get("2"), "??")
        self.assertNotIn("3", marks)

    def test_checkmate_is_not_a_blunder(self):
        records = [
            {"cp": viewer.MATE_CP, "second": 0, "best": "d8h4", "over": False},
            {"cp": -viewer.MATE_CP, "second": None, "best": None, "over": True},
        ]
        self.assertEqual(viewer.annotate_plies(["d8h4"], records), {})


class SidecarTest(unittest.TestCase):
    def test_names(self):
        path = Path("out/live/llm-swiss-1-tournament.json")
        self.assertEqual(viewer.state_slug(path), "llm-swiss-1")
        self.assertEqual(viewer.annotations_path(path).name, "llm-swiss-1-annotations.json")

    def test_save_and_reload_skips_known_positions(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "t-tournament.json"
            state = {"games": {"r1b1": {"status": "finished", "moves": [{"uci": "e2e4"}, {"uci": "e7e5"}]}}}
            first = viewer.Annotator(Path("missing-engine.exe"))
            first.watch(state_path, state)
            fens = first.games[state_path]["r1b1"]["fens"]
            self.assertEqual(len(fens), 3)
            for fen in fens:
                first.positions[state_path][fen] = {"cp": 20, "second": 0, "best": "x", "over": False}
            first.save(state_path)
            self.assertTrue((Path(tmp) / "t-annotations.json").exists())
            second = viewer.Annotator(Path("missing-engine.exe"))
            second.watch(state_path, state)
            self.assertIsNone(second._next())      # everything came from the sidecar
            self.assertEqual(second.progress(state_path)["done"], 3)
            self.assertIn("r1b1", second.annotations(state_path))

    def test_new_moves_extend_positions(self):
        annotator = viewer.Annotator(Path("missing-engine.exe"))
        path = Path("x-tournament.json")
        annotator.watch(path, {"games": {"g": {"status": "live", "moves": [{"uci": "e2e4"}]}}})
        annotator.watch(path, {"games": {"g": {"status": "live", "moves": [{"uci": "e2e4"}, {"uci": "c7c5"}]}}})
        self.assertEqual(annotator.games[path]["g"]["uci"], ["e2e4", "c7c5"])
        self.assertEqual(len(annotator.games[path]["g"]["fens"]), 3)
        self.assertTrue(annotator.games[path]["g"]["live"])


class CommentaryOffTest(unittest.TestCase):
    """The commentary endpoints with --commentary off (or the module missing)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        state = Path(cls.tmp.name) / "t-tournament.json"
        state.write_text(json.dumps({"id": "t", "games": {"r1b1": {"status": "live", "moves": []}}}), encoding="utf-8")

        class H(viewer.Handler):
            pass

        H.state_path = state
        H.live_dir = Path(cls.tmp.name)
        H.analyzer = None
        H.annotator = None
        H.commentator = None
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def get(self, path, method="GET"):
        req = urllib.request.Request(self.base + path, method=method, data=b"" if method == "POST" else None)
        with urllib.request.urlopen(req, timeout=5) as res:
            return res.status, json.loads(res.read())

    def test_clips_disabled(self):
        self.assertEqual(self.get("/api/commentary?game=r1b1&after=0"), (200, {"enabled": False, "clips": []}))

    def test_focus_disabled(self):
        self.assertEqual(self.get("/api/commentary/focus?game=r1b1")[1], {"enabled": False})
        self.assertEqual(self.get("/api/commentary/focus?game=r1b1", method="POST")[1], {"enabled": False})

    def test_audio_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(self.base + "/api/commentary/audio/clip-1.mp3", timeout=5)
        self.assertEqual(ctx.exception.code, 404)

    def test_tournament_reports_commentary_off(self):
        status, state = self.get("/api/tournament")
        self.assertEqual(status, 200)
        self.assertFalse(state["commentary"])
        self.assertEqual(state["annotations"], {})

    def test_start_commentator_without_module(self):
        saved = sys.modules.get("llm_commentary")
        sys.modules["llm_commentary"] = None   # makes the import fail
        try:
            logs = []
            self.assertIsNone(viewer.start_commentator(Path(self.tmp.name) / "t-tournament.json", log=logs.append))
            self.assertTrue(any("commentary off" in line for line in logs))
            self.assertIsNone(viewer.start_commentator(None, log=logs.append))
        finally:
            if saved is None:
                sys.modules.pop("llm_commentary", None)
            else:
                sys.modules["llm_commentary"] = saved

    def test_audio_names(self):
        self.assertTrue(viewer.safe_audio_name("r1b1-0003.mp3"))
        for bad in ("../x.mp3", "a/b.mp3", "a\\b.mp3", "", "..", ".hidden.mp3", "C:x.mp3"):
            self.assertFalse(viewer.safe_audio_name(bad), bad)


class CommentaryOnTest(unittest.TestCase):
    """The endpoints talk to the Commentator contract (clips, audio_path, focus)."""

    def test_clips_audio_and_focus(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "c1.mp3"
            audio.write_bytes(b"ID3fake")

            class Fake:
                focused = None
                calls = []

                def clips(self, game, after):
                    return [{"seq": 1, "ply": 4, "text": "hi", "audio": "c1.mp3"}] if after < 1 else []

                def clips_all(self, after):
                    every = [{"seq": 1, "game": "r1b1", "ply": 4, "text": "a", "audio": "c1.mp3", "seconds": 3.0, "final": False},
                             {"seq": 2, "game": "r1b2", "ply": 7, "text": "b", "audio": None, "seconds": 0, "final": True}]
                    return [c for c in every if c["seq"] > after]

                def audio_path(self, name):
                    return audio if name == "c1.mp3" else None

                def focus(self, game_id, pinned=False):
                    Fake.focused = game_id
                    Fake.calls.append((game_id, pinned))

            class H(viewer.Handler):
                pass

            H.state_path, H.analyzer, H.annotator, H.commentator = None, None, None, Fake()
            server = ThreadingHTTPServer(("127.0.0.1", 0), H)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                with urllib.request.urlopen(base + "/api/commentary?game=r1b1&after=0", timeout=5) as res:
                    self.assertEqual(json.loads(res.read())["clips"][0]["seq"], 1)
                with urllib.request.urlopen(base + "/api/commentary?game=r1b1&after=1", timeout=5) as res:
                    self.assertEqual(json.loads(res.read()), {"enabled": True, "clips": []})
                with urllib.request.urlopen(base + "/api/commentary/audio/c1.mp3", timeout=5) as res:
                    self.assertEqual(res.headers["Content-Type"], "audio/mpeg")
                    self.assertEqual(res.read(), b"ID3fake")
                with self.assertRaises(urllib.error.HTTPError):
                    urllib.request.urlopen(base + "/api/commentary/audio/..%2Fc1.mp3", timeout=5)
                # No game: every board's clips in seq order (auto mode).
                with urllib.request.urlopen(base + "/api/commentary?after=0", timeout=5) as res:
                    clips = json.loads(res.read())["clips"]
                self.assertEqual([(c["seq"], c["game"]) for c in clips], [(1, "r1b1"), (2, "r1b2")])
                with urllib.request.urlopen(base + "/api/commentary?after=1", timeout=5) as res:
                    self.assertEqual([c["seq"] for c in json.loads(res.read())["clips"]], [2])
                # Focus mode pins the board; leaving it (no game) hands the choice back.
                req = urllib.request.Request(base + "/api/commentary/focus?game=r2b1", method="POST", data=b"")
                with urllib.request.urlopen(req, timeout=5) as res:
                    self.assertTrue(json.loads(res.read())["enabled"])
                self.assertEqual(Fake.focused, "r2b1")
                req = urllib.request.Request(base + "/api/commentary/focus", method="POST", data=b"")
                with urllib.request.urlopen(req, timeout=5) as res:
                    self.assertTrue(json.loads(res.read())["enabled"])
                self.assertEqual(Fake.calls, [("r2b1", True), (None, False)])
                req = urllib.request.Request(base + "/api/commentary/focus?game=..%2Fx", method="POST", data=b"")
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(req, timeout=5)
                self.assertEqual(ctx.exception.code, 400)
                self.assertEqual(len(Fake.calls), 2)
            finally:
                server.shutdown()
                server.server_close()


class ThinkingEndpointTest(unittest.TestCase):
    """GET /api/thinking reads <slug>-<game>-ply<N>.thinking.txt next to the state file."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        # The slug is the state's "id", not the file name.
        state = cls.dir / "named-differently-tournament.json"
        state.write_text(json.dumps({"id": "swiss-1", "games": {"r1b1": {"status": "live", "moves": []}}}), encoding="utf-8")
        (cls.dir / "other-tournament.json").write_text(json.dumps({"id": "other", "games": {}}), encoding="utf-8")
        (cls.dir / "other-r1b1-ply1.thinking.txt").write_text("from the other tournament", encoding="utf-8")
        (cls.dir / "swiss-1-r1b1-ply3.thinking.txt").write_text("I consider e4.\n[thinking stopped at the move cap - answering from its own thoughts]\n", encoding="utf-8")
        (cls.dir / "swiss-1-r1b1-ply4.thinking.txt").write_bytes(("x" * 70_000 + "END").encode("utf-8"))
        (cls.dir / "swiss-1-r1b1-ply5.thinking.txt").write_bytes("abé€".encode("utf-8")[:-1])   # live writer mid-character
        (cls.dir / "secret.txt").write_text("nope", encoding="utf-8")

        class H(viewer.Handler):
            pass

        H.state_path = state
        H.live_dir = cls.dir
        H.analyzer = H.annotator = H.commentator = None
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def get(self, query):
        with urllib.request.urlopen(self.base + "/api/thinking?" + query, timeout=5) as res:
            return json.loads(res.read())

    def status(self, query):
        try:
            with urllib.request.urlopen(self.base + "/api/thinking?" + query, timeout=5) as res:
                return res.status
        except urllib.error.HTTPError as exc:
            return exc.code

    def test_reads_whole_small_file(self):
        j = self.get("game=r1b1&ply=3")
        self.assertTrue(j["exists"])
        self.assertFalse(j["truncated"])
        self.assertEqual(j["from"], 0)
        self.assertIn("[thinking stopped at the move cap", j["text"])
        self.assertEqual(j["size"], (self.dir / "swiss-1-r1b1-ply3.thinking.txt").stat().st_size)

    def test_since_returns_only_new_text(self):
        full = self.get("game=r1b1&ply=3")
        j = self.get(f"game=r1b1&ply=3&since={len('I consider ')}")
        self.assertEqual(j["from"], len("I consider "))
        self.assertTrue(j["text"].startswith("e4."))
        self.assertEqual(j["size"], full["size"])
        self.assertEqual(self.get(f"game=r1b1&ply=3&since={full['size']}")["text"], "")

    def test_since_past_the_end_starts_over(self):
        j = self.get("game=r1b1&ply=3&since=999999")
        self.assertEqual(j["from"], 0)
        self.assertTrue(j["text"].startswith("I consider"))

    def test_large_file_returns_the_tail(self):
        j = self.get("game=r1b1&ply=4")
        self.assertTrue(j["truncated"])
        self.assertEqual(j["size"], 70_003)
        self.assertEqual(j["from"], 70_003 - viewer.THINK_CHUNK)
        self.assertEqual(len(j["text"]), viewer.THINK_CHUNK)
        self.assertTrue(j["text"].endswith("END"))
        # Close behind: everything new, not truncated.
        k = self.get("game=r1b1&ply=4&since=69000")
        self.assertFalse(k["truncated"])
        self.assertEqual((k["from"], k["text"]), (69000, "x" * 1000 + "END"))
        # Far behind: only the last 60,000 bytes.
        self.assertTrue(self.get("game=r1b1&ply=4&since=5")["truncated"])

    def test_unfinished_character_waits_for_the_next_read(self):
        j = self.get("game=r1b1&ply=5")
        self.assertEqual(j["text"], "abé")
        self.assertEqual(j["size"], 4)   # the cut euro sign is read next time from byte 4
        self.assertNotIn("�", j["text"])

    def test_missing_file(self):
        self.assertEqual(self.get("game=r1b1&ply=1"), {"exists": False, "size": 0, "text": "", "from": 0, "truncated": False})
        self.assertFalse(self.get("game=r9b9&ply=7")["exists"])

    def test_other_tournament_by_id(self):
        self.assertEqual(self.get("game=r1b1&ply=1&id=other")["text"], "from the other tournament")

    def test_rejects_bad_input(self):
        for query in (
            "game=..%2Fsecret&ply=1", "game=r1b1%2F..&ply=1", "game=r1b1.txt&ply=1", "game=&ply=1", "ply=1",
            "game=r1b1&ply=0", "game=r1b1&ply=1001", "game=r1b1&ply=-1", "game=r1b1&ply=abc", "game=r1b1",
            "game=r1b1&ply=1&since=-5", "game=r1b1&ply=1&since=x", "game=r1b1&ply=1&id=..%2F..%2Fx",
            "game=r1b1%00&ply=1", "game=r%C3%A9&ply=1", "game=" + "a" * 81 + "&ply=1",
        ):
            self.assertEqual(self.status(query), 400, query)

    def test_unknown_tournament_id(self):
        self.assertEqual(self.status("game=r1b1&ply=1&id=nope"), 404)

    def test_read_thinking_helper_never_starts_mid_character(self):
        path = self.dir / "multi.txt"
        path.write_bytes(("€" * 30).encode("utf-8"))   # 90 bytes, 3 per character
        j = viewer.read_thinking(path, 0, limit=10)
        self.assertTrue(j["truncated"])
        self.assertEqual(j["text"], "€" * 3)
        self.assertEqual(j["from"], 81)
        self.assertEqual(j["size"], 90)


class PageTest(unittest.TestCase):
    def test_commentary_follows_all_boards(self):
        self.assertIn("/api/commentary?after=", viewer.PAGE)
        self.assertNotIn("/api/commentary?game=", viewer.PAGE)
        self.assertIn("On commentary", viewer.PAGE)

    def test_thinking_panel_present(self):
        for needle in ("/api/thinking?", "data-think-toggle", 'class="think-body"', "swissThinking.", "No visible thinking for this move.",
                       "Thinking shows what each model chose to reveal while deciding: raw reasoning for most API models, summaries for GPT and Claude, search lines for Stockfish."):
            self.assertIn(needle, viewer.PAGE, needle)

    def test_no_long_dashes(self):
        text = Path(viewer.__file__).read_text(encoding="utf-8")
        self.assertNotIn("—", text)
        self.assertNotIn("–", text)

    def test_rules_note_present(self):
        self.assertIn("Move marks (?? ? ?! !) come from Stockfish 19 for viewers only.", viewer.PAGE)

    def test_round_robin_and_knockout_pieces_present(self):
        for needle in (
            # header stage chip and rules
            "Round robin - round <b>", "Semifinals", "Champion: <b>", "data-show-champion",
            "Round robin, everyone plays everyone once; top ${koSize} to the knockouts.",
            "Knockout draw: an Armageddon decider with colours swapped; White ${clock(cfg.armageddonWhiteMs || 600000)}, Black ${clock(cfg.armageddonBlackMs || 450000)}, a draw counts as a Black win.",
            # board card label and Armageddon tag
            "pairingOf(game.id)", "<b>ARMAGEDDON</b>", "draw = Black wins",
            # bracket (and its round-robin preview)
            'id="bracketCard"', "Road to the final", "Knockout bracket", "Champion: to be crowned", "data-bk-game", "Won in Armageddon",
            "Winner of Semifinal 1", "Third place",
            # champion moment
            'id="champOverlay"', 'id="confetti"', "is the champion", "Runner-up", "Back to the boards", "data-close-champion", "#champion",
            # opening hook
            "1 crown.", "Click or press Esc to skip", "intro", "Draw? Armageddon decides",
        ):
            self.assertIn(needle, viewer.PAGE, needle)

    def test_auto_focus_present(self):
        for needle in ("data-toggle-autofocus", "Auto-focus: <b>", 'store("swissAutoFocus"', "following the commentary",
                       "AUTO_GAP_MS = 20000", "history.replaceState", "focusPinned"):
            self.assertIn(needle, viewer.PAGE, needle)
        # auto switches never pin the commentator: only a focus the viewer chose is sent as a pin
        self.assertIn("const want = focusId && focusPinned && data.games && data.games[focusId] ? focusId : null;", viewer.PAGE)
        self.assertIn("if (focusId && focusPinned && clip.game !== focusId) return false;", viewer.PAGE)

    def test_compact_bracket_beside_focused_board(self):
        # one bracket model drives the side panel and the compact focus-mode bracket
        for needle in ("function bracketSlots()", "function miniBracketHtml(curId)", 'data-part="minibk"',
                       "setHTML(p.minibk, focused ? miniBracketHtml(game.id) : \"\");", "Armageddon live", "To be crowned"):
            self.assertIn(needle, viewer.PAGE, needle)
        # wide screens only, so phones and narrow windows keep the bracket below the boards
        self.assertIn(".mini-bk { display: none; }", viewer.PAGE)
        self.assertIn("@media (min-width: 1280px) { .card.focused .mini-bk:not(:empty) { display: block;", viewer.PAGE)
        # a one-row header leaves the focused board as large as before; a wrapped header shrinks it to fit
        self.assertIn("calc(100vh - 290px - var(--hdr-extra, 0px))", viewer.PAGE)
        # compact matches reuse the bracket click (a manual pick: pins the commentator, Auto-focus off)
        mini = viewer.PAGE[viewer.PAGE.index("function miniMatch("):viewer.PAGE.index("function miniBracketHtml(")]
        self.assertIn("data-bk-game", mini)

    def test_focus_mode_leaderboard_always_visible(self):
        page = viewer.PAGE
        for needle in ("function leaderboardHtml(game)", 'data-part="lbside"', 'data-part="lbunder"',
                       "knockout zone", "Round robin table (final)", 'class="sd"', "lb-r ${side ? \"me\" : \"\"}",
                       'const lb = focused ? leaderboardHtml(game) : "";'):
            self.assertIn(needle, page, needle)
        # wide screens: a third column beside the board, same height as the info column
        self.assertIn('grid-template-areas: "head head head" "boardcol infocol lbcol"', page)
        self.assertIn(".card.focused .lb-side:not(:empty) { display: flex; flex-direction: column; grid-area: lbcol; height: calc(var(--fboard) + 64px); }", page)
        # the third column comes out of the width, never the height: a 1080p board keeps its size
        self.assertIn("calc(100vh - 290px - var(--hdr-extra, 0px)), calc(100vw - 822px)", page)
        # narrow screens: under the board (inside the board column, so above the move list)
        board_col = page[page.index('<div class="boardcol">'):page.index('<div class="infocol">')]
        self.assertIn('data-part="lbunder"', board_col)
        # the grid view keeps the full standings panel
        self.assertIn('id="standingsCard"', page)

    def test_no_external_assets(self):
        self.assertNotIn("http://", viewer.PAGE.replace("http://www.w3.org", ""))
        self.assertNotIn("https://", viewer.PAGE)


KNOCKOUT_STATE = {
    "id": "ko-1",
    "title": "AI Chess Championship",
    "config": {"rounds": 9, "timeControlMs": 600000, "incrementMs": 0, "maxAttempts": 3, "startElo": 1500, "eloK": 32},
    "format": {"type": "round-robin+knockout", "rr_rounds": 9, "ko_size": 4},
    "stage": "semifinals",
    "current_round": 10,
    "players": [{"name": n} for n in ("A", "B", "C", "D")],
    "standings": [{"name": n, "rank": i + 1, "points": 9 - i} for i, n in enumerate("ABCD")],
    "rounds": [
        {"round": 9, "pairings": [{"board": 1, "white": "A", "black": "B", "game_id": "r9b1"}], "status": "finished"},
        {"round": 10, "stage": "semifinals", "label": "Semifinals", "status": "live", "pairings": [
            {"board": 1, "white": "A", "black": "D", "game_id": "r10b1", "match": "sf1", "label": "Semifinal 1 - Game 1"},
            {"board": 2, "white": "B", "black": "C", "game_id": "r10b2", "match": "sf2", "label": "Semifinal 2 - Game 1"},
            {"board": 3, "white": "D", "black": "A", "game_id": "r10b3", "match": "sf1", "label": "Semifinal 1 - Armageddon decider", "armageddon": True},
        ]},
    ],
    "games": {
        "r9b1": {"id": "r9b1", "status": "finished", "result": "1-0", "white": "A", "black": "B", "moves": []},
        "r10b1": {"id": "r10b1", "status": "finished", "result": "1/2-1/2", "white": "A", "black": "D", "moves": [], "match": "sf1"},
        "r10b2": {"id": "r10b2", "status": "live", "result": "*", "white": "B", "black": "C", "moves": [], "match": "sf2"},
        "r10b3": {"id": "r10b3", "status": "live", "result": "*", "white": "D", "black": "A", "moves": [], "match": "sf1",
                  "armageddon": True, "draw_odds": "black", "clocks": {"white": 600000, "black": 450000, "running": "white"}},
    },
    "knockout": {
        "seeds": [{"seed": i + 1, "name": n, "points": 9 - i} for i, n in enumerate("ABCD")],
        "matches": [
            {"id": "sf1", "stage": "semifinals", "label": "Semifinal 1", "a": "A", "b": "D", "games": ["r10b1", "r10b3"], "winner": None, "decided_by": None},
            {"id": "sf2", "stage": "semifinals", "label": "Semifinal 2", "a": "B", "b": "C", "games": ["r10b2"], "winner": None, "decided_by": None},
        ],
        "champion": None, "runner_up": None, "third": None,
    },
}


class KnockoutStateTest(unittest.TestCase):
    """A round robin + knockout state passes through /api/tournament untouched, next to the page."""

    def serve(self, state):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / f"{state['id']}-tournament.json"
        path.write_text(json.dumps(state), encoding="utf-8")

        class H(viewer.Handler):
            pass

        H.state_path, H.live_dir = path, Path(tmp.name)
        H.analyzer = H.annotator = H.commentator = None
        server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}"

    def fetch(self, url):
        with urllib.request.urlopen(url, timeout=5) as res:
            return res.status, res.headers.get("Content-Type", ""), res.read()

    def test_knockout_fields_pass_through(self):
        base = self.serve(KNOCKOUT_STATE)
        status, kind, body = self.fetch(base + "/api/tournament")
        self.assertEqual(status, 200)
        self.assertIn("application/json", kind)
        state = json.loads(body)
        for key in ("format", "stage", "knockout", "rounds", "games", "standings"):
            self.assertEqual(state[key], KNOCKOUT_STATE[key], key)
        self.assertTrue(state["games"]["r10b3"]["armageddon"])
        self.assertEqual(state["games"]["r10b3"]["draw_odds"], "black")

    def test_champion_state_and_page(self):
        crowned = json.loads(json.dumps(KNOCKOUT_STATE))
        crowned.update(stage="finished", finished=True, winner="A")
        crowned["knockout"].update(champion="A", runner_up="B", third="C")
        base = self.serve(crowned)
        status, _, body = self.fetch(base + "/api/tournament")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["knockout"]["champion"], "A")
        status, kind, page = self.fetch(base + "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", kind)
        self.assertIn(b'id="bracketCard"', page)
        self.assertIn(b'id="champOverlay"', page)

    def test_old_state_without_new_fields(self):
        old = {k: v for k, v in KNOCKOUT_STATE.items() if k not in ("format", "stage", "knockout")}
        old["rounds"] = old["rounds"][:1]
        base = self.serve(old)
        status, _, body = self.fetch(base + "/api/tournament")
        self.assertEqual(status, 200)
        state = json.loads(body)
        self.assertNotIn("knockout", state)
        self.assertNotIn("format", state)


if __name__ == "__main__":
    unittest.main()
