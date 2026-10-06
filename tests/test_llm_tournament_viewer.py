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

                def clips(self, game, after):
                    return [{"seq": 1, "ply": 4, "text": "hi", "audio": "c1.mp3"}] if after < 1 else []

                def audio_path(self, name):
                    return audio if name == "c1.mp3" else None

                def focus(self, game):
                    Fake.focused = game

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
                req = urllib.request.Request(base + "/api/commentary/focus?game=r2b1", method="POST", data=b"")
                with urllib.request.urlopen(req, timeout=5) as res:
                    self.assertTrue(json.loads(res.read())["enabled"])
                self.assertEqual(Fake.focused, "r2b1")
            finally:
                server.shutdown()
                server.server_close()


class PageTest(unittest.TestCase):
    def test_no_long_dashes(self):
        text = Path(viewer.__file__).read_text(encoding="utf-8")
        self.assertNotIn("—", text)
        self.assertNotIn("–", text)

    def test_rules_note_present(self):
        self.assertIn("Move marks (?? ? ?! !) come from Stockfish 19 for viewers only.", viewer.PAGE)


if __name__ == "__main__":
    unittest.main()
