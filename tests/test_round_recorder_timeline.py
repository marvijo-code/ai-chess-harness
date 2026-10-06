"""Pure-python tests of the series recorder timeline (no ffmpeg, no browser)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "round_recorder"))

import timeline as tl  # noqa: E402

try:
    import mix  # noqa: E402  (needs numpy)
except ImportError:  # pragma: no cover
    mix = None

T0 = 1_000_000.0      # x11grab start epoch of the take


def page(kind, at, **kw):
    """A page event `at` seconds after T0."""
    return {"kind": kind, "t": (T0 + at) * 1000.0, **kw}


def tour(a, at, **kw):
    return page("director", at, k="tour", a=a, **kw)


class SpanTests(unittest.TestCase):
    def test_pairs_start_with_next_end_and_reads_speed(self):
        evs = [tour("start", 10, speed=10), tour("board", 20, game="r5b2"), tour("end", 100),
               tour("start", 200, speed=6), tour("end", 260)]
        spans = tl.tour_spans(evs)
        self.assertEqual(len(spans), 2)
        self.assertAlmostEqual(spans[0][0] - T0, 10, places=3)
        self.assertAlmostEqual(spans[0][1] - T0, 100, places=3)
        self.assertEqual(spans[0][2], 10)
        self.assertEqual(spans[1][2], 6)

    def test_default_speed_and_open_span(self):
        spans = tl.tour_spans([tour("start", 5)])
        self.assertEqual(spans, [(T0 + 5, None, tl.DEFAULT_SPEED)])

    def test_start_without_end_closes_at_window_end(self):
        valid, _ = tl.lapse_spans([tour("start", 50, speed=10)], T0, T0 + 120)
        self.assertEqual(len(valid), 1)
        self.assertAlmostEqual(valid[0]["end"], T0 + 120)

    def test_span_clamped_to_window(self):
        evs = [tour("start", 10, speed=10), tour("end", 100)]
        valid, _ = tl.lapse_spans(evs, T0 + 40, T0 + 80)
        self.assertAlmostEqual(valid[0]["start"], T0 + 40)
        self.assertAlmostEqual(valid[0]["end"], T0 + 80)

    def test_span_with_a_clip_start_is_normal_speed(self):
        evs = [tour("start", 10, speed=10), page("clip_playing", 15, src="x/clip-1.wav"), tour("end", 30)]
        valid, rejected = tl.lapse_spans(evs, T0, T0 + 100)
        self.assertEqual(valid, [])
        self.assertEqual(rejected[0]["why_normal"], "clip starts inside")

    def test_clip_before_span_does_not_block_it(self):
        evs = [page("clip_playing", 5, src="x/clip-1.wav"), tour("start", 10, speed=10), tour("end", 30)]
        valid, _ = tl.lapse_spans(evs, T0, T0 + 100)
        self.assertEqual(len(valid), 1)

    def test_short_span_ignored(self):
        evs = [tour("start", 10, speed=10), tour("end", 17.9)]
        valid, rejected = tl.lapse_spans(evs, T0, T0 + 100)
        self.assertEqual(valid, [])
        self.assertIn("shorter", rejected[0]["why_normal"])


class SegmentTests(unittest.TestCase):
    def test_segments_cover_window_and_align_to_frames(self):
        segs = tl.build_segments(0.0, 300.0, [(100.013, 250.0, 10)])
        self.assertEqual(segs[0][0], 0.0)
        self.assertEqual(segs[-1][1], 300.0)
        for (a, b, s), nxt in zip(segs, segs[1:] + [None]):
            self.assertAlmostEqual(a * 30, round(a * 30), places=4)
            self.assertAlmostEqual(b * 30, round(b * 30), places=4)
            if nxt:
                self.assertAlmostEqual(b, nxt[0])
            if s > 1:
                self.assertEqual(round((b - a) * 30) % s, 0)
        self.assertEqual([s for _, _, s in segs], [1, 10, 1])

    def test_output_duration_is_sum_of_segments(self):
        segs = tl.build_segments(0.0, 300.0, [(100.0, 250.0, 10)])
        self.assertAlmostEqual(tl.output_duration(segs), 100 + 15 + 50)
        rows = tl.output_table(segs)
        self.assertEqual(sum(r[5] for r in rows), round(165 * 30))
        self.assertAlmostEqual(rows[-1][4], 165.0)

    def test_remap_piecewise_linear(self):
        segs = tl.build_segments(0.0, 300.0, [(100.0, 250.0, 10)])
        self.assertAlmostEqual(tl.remap(50, segs), 50)
        self.assertAlmostEqual(tl.remap(100, segs), 100)
        self.assertAlmostEqual(tl.remap(175, segs), 107.5)
        self.assertAlmostEqual(tl.remap(250, segs), 115)
        self.assertAlmostEqual(tl.remap(280, segs), 145)

    def test_remap_with_window_offset(self):
        segs = tl.build_segments(2.0, 60.0, [])
        self.assertAlmostEqual(tl.remap(2.0, segs), 0.0)
        self.assertAlmostEqual(tl.remap(12.0, segs), 10.0)
        self.assertAlmostEqual(tl.remap(1.0, segs), -1.0)

    def test_in_lapse(self):
        segs = tl.build_segments(0.0, 300.0, [(100.0, 250.0, 10)])
        self.assertTrue(tl.in_lapse(120, segs))
        self.assertFalse(tl.in_lapse(99.9, segs))
        self.assertFalse(tl.in_lapse(250.0, segs))

    def test_overlapping_spans_do_not_overlap_segments(self):
        segs = tl.build_segments(0.0, 100.0, [(10.0, 50.0, 10), (40.0, 80.0, 5)])
        for (a, b, _), (c, d, _) in zip(segs, segs[1:]):
            self.assertLessEqual(b, c + 1e-9)
        self.assertEqual(segs[-1][1], 100.0)

    def test_video_filter_text(self):
        segs = tl.build_segments(0.0, 300.0, [(100.0, 250.0, 10)])
        vf = tl.video_filter(segs)
        self.assertIn("split=3[s0][s1][s2]", vf)
        self.assertIn("trim=start_frame=3000:end_frame=7500,setpts=(PTS-STARTPTS)/10,fps=30[g1]", vf)
        self.assertIn("[g0][g1][g2]concat=n=3:v=1:a=0[v]", vf)
        one = tl.video_filter(tl.build_segments(0.0, 10.0, []))
        self.assertNotIn("split", one)


class WindowTests(unittest.TestCase):
    def test_take_t0_prefers_x11grab_start(self):
        self.assertEqual(tl.take_t0({"ffmpeg_input_start": 10.1, "ffmpeg_popen_epoch": 10.0}), (10.1, "x11grab start"))
        self.assertEqual(tl.take_t0({"ffmpeg_input_start": None, "ffmpeg_popen_epoch": 10.0}), (10.0, "popen"))
        self.assertEqual(tl.take_t0({"ffmpeg_input_start": 99.0, "ffmpeg_popen_epoch": 10.0}), (10.0, "popen"))

    def test_window_assignment_uses_page_t_then_py(self):
        rounds = [{"round": 5, "key": "5", "start_epoch": 100.0, "end_epoch": 200.0},
                  {"round": 6, "key": "6", "start_epoch": 200.0, "end_epoch": 300.0}]
        evs = [{"kind": "click", "t": 150_000.0}, {"kind": "round_done", "py": 250.0}, {"kind": "click", "t": 350_000.0}]
        self.assertEqual(len(tl.window_events(evs, tl.find_entry(rounds, 5))), 1)
        self.assertEqual(tl.window_events(evs, tl.find_entry(rounds, "6"))[0]["kind"], "round_done")
        self.assertIsNone(tl.find_entry(rounds, 7))


def st(status="live", results=("*", "*"), stage=None, **kw):
    games = [{"id": f"g{i}", "status": "finished" if r != "*" else "live", "result": r, "plies": 10}
             for i, r in enumerate(results)]
    base = {"exists": True, "status": status, "stage": stage, "games": games, "finished": False, "last_round": 9}
    base.update(kw)
    return base


DONE = ("1-0", "0-1")


class BoundaryTests(unittest.TestCase):
    def test_round_done_needs_results_and_finished_status(self):
        self.assertFalse(tl.round_done(st("live", DONE)))
        self.assertFalse(tl.round_done(st("finished", ("1-0", "*"))))
        self.assertTrue(tl.round_done(st("finished", DONE)))
        self.assertFalse(tl.round_done(None))

    def test_round_robin_cuts_after_results_card_hide(self):
        b = tl.RoundBoundary(5)
        self.assertIsNone(b.on_poll(st(), 100))
        self.assertEqual(b.on_poll(st("finished", DONE), 200), "round_done")
        self.assertIsNone(b.cut_at(st("finished", DONE), 210))
        b.on_director({"k": "rcard", "a": "hide", "mode": "results", "round": 4}, 215)   # other round: ignored
        self.assertIsNone(b.cut_at(st("finished", DONE), 216))
        b.on_director({"k": "rcard", "a": "hide", "mode": "intro", "round": 5}, 217)     # intro: ignored
        self.assertIsNone(b.cut_at(st("finished", DONE), 218))
        b.on_director({"k": "rcard", "a": "hide", "mode": "results", "round": 5}, 230)
        self.assertEqual(b.cut_at(st("finished", DONE), 230.1), (230.5, "results_card_closed"))
        self.assertEqual(b.cut_at(st("finished", DONE), 231.4), (231.4, "results_card_closed"))

    def test_round_robin_fallback_90s(self):
        b = tl.RoundBoundary(5)
        b.on_poll(st("finished", DONE), 200)
        self.assertIsNone(b.cut_at(st("finished", DONE), 289))
        self.assertEqual(b.cut_at(st("finished", DONE), 290), (290, "round_done_fallback"))
        self.assertFalse(b.series_over_after(st("finished", DONE)))

    def test_knockout_tail_and_reopen(self):
        b = tl.RoundBoundary(10)
        b.on_poll(st("live", stage="semifinals"), 100)
        self.assertEqual(b.on_poll(st("finished", DONE, stage="semifinals"), 200), "round_done")
        self.assertIsNone(b.cut_at(st("finished", DONE, stage="semifinals"), 210))
        # an Armageddon decider appears: the round reopens and the tail restarts later
        self.assertEqual(b.on_poll(st("live", DONE + ("*",), stage="semifinals"), 212), "round_reopened")
        self.assertIsNone(b.cut_at(st("live", DONE + ("*",), stage="semifinals"), 240))
        b.on_poll(st("finished", DONE + ("1-0",), stage="semifinals"), 400)
        self.assertIsNone(b.cut_at(None, 419))
        self.assertEqual(b.cut_at(None, 420), (420, "ko_round_done"))
        self.assertFalse(b.series_over_after(st("finished", DONE, stage="semifinals")))

    def test_final_waits_for_champion_show_line_and_quiet(self):
        b = tl.RoundBoundary(11)
        fin = dict(stage="final", last_round=11)
        b.on_poll(st("finished", DONE, **fin), 100)
        self.assertIsNone(b.cut_at(None, 200))                 # round_done alone does not end the final
        b.on_poll(st("finished", DONE, champion="X", **fin), 210)
        self.assertIsNone(b.cut_at(None, 230))
        b.on_director({"k": "champion", "a": "show"}, 212)
        b.on_poll(st("finished", DONE, champion="X", heard_champion=True, clip_playing=True, **fin), 220)
        self.assertIsNone(b.cut_at(None, 240))                 # still talking
        b.on_poll(st("finished", DONE, champion="X", heard_champion=True, clip_playing=False, **fin), 241)
        self.assertIsNone(b.cut_at(None, 252))
        self.assertEqual(b.cut_at(None, 253), (253, "tournament_over"))
        self.assertTrue(b.series_over_after(None))

    def test_final_cap_after_champion(self):
        b = tl.RoundBoundary(11)
        b.on_poll(st("finished", DONE, stage="final", champion="X"), 100)
        self.assertIsNone(b.cut_at(None, 279))
        self.assertEqual(b.cut_at(None, 280), (280, "tournament_over_cap"))

    def test_round_robin_end_of_tournament_without_knockouts(self):
        b = tl.RoundBoundary(9)
        self.assertTrue(b.series_over_after(st("finished", DONE, finished=True, last_round=9)))
        self.assertFalse(b.series_over_after(st("finished", DONE, finished=False, last_round=9)))


@unittest.skipIf(mix is None, "numpy not installed")
class MixPlanTests(unittest.TestCase):
    def test_round_clips_window_and_carry_over(self):
        evs = [page("clip_playing", 5, src="a/clip-1.wav"), page("clip_ended", 13, src="a/clip-1.wav", ct=8.0),
               page("clip_playing", 20, src="a/clip-2.wav"), page("clip_ended", 24, src="a/clip-2.wav", ct=4.0),
               page("clip_playing", 70, src="a/clip-3.wav")]
        clips = mix.round_clips(evs, T0, 10.0, 60.0)
        self.assertEqual([c["name"] for c in clips], ["clip-1.wav", "clip-2.wav"])
        self.assertAlmostEqual(clips[0]["seek"], 5.0)
        self.assertNotIn("seek", clips[1])

    def test_round_clicks_dropped_inside_lapse(self):
        segs = tl.build_segments(0.0, 300.0, [(100.0, 250.0, 10)])
        evs = [page("click", 50, state="running"), page("click", 120, state="running"),
               page("click", 260, state="running", delay=0.1), page("click", 270, state="suspended")]
        kept, dropped = mix.round_clicks(evs, T0, 0.0, 300.0, segs)
        self.assertEqual(dropped, 1)
        self.assertEqual(len(kept), 2)
        self.assertAlmostEqual(kept[1], 260.1, places=3)

    def test_speech_parts_offsets(self):
        parts, n = mix.speech_parts([{"out_offset": 1.5, "play": 2.0}, {"out_offset": 3.0, "play": 1.0, "seek": 0.5}], 10.0)
        self.assertEqual(n, 2)
        self.assertIn("adelay=1500:all=1", parts[0])
        self.assertIn("atrim=0.500:1.500", parts[1])
        silent, n0 = mix.speech_parts([], 5.0)
        self.assertEqual(n0, 0)
        self.assertIn("anullsrc", silent[0])


class SeriesBookTests(unittest.TestCase):
    def test_rounds_json_and_rec_done_markers(self):
        import json
        import tempfile

        import rec

        class FakeEv:
            def __init__(self):
                self.rows = []

            def write(self, o):
                self.rows.append(o)

        class FakeTake:
            def __init__(self, run, key):
                self.key, self.raw, self.popen_epoch, self.input_start, self.rc = key, run / f"raw-r{key}.mkv", 1.0, 1.1, None

            def stop(self):
                self.rc = 0

        with tempfile.TemporaryDirectory() as d:
            run = Path(d)
            book = rec.SeriesBook(run, FakeEv())
            t5 = FakeTake(run, "5")
            e5 = book.open_entry(5, t5, 1.1)
            t6 = FakeTake(run, "6")
            book.close(t5, e5, "results_card_closed", 50.0, sync=False)
            e6 = book.open_entry(6, t6, 50.0)
            book.close(t6, e6, "tournament_over", 90.0, sync=True, last=True)
            book.join()
            rows = json.loads((run / "rounds.json").read_text())
            self.assertEqual([r["key"] for r in rows], ["5", "6"])
            self.assertEqual(rows[0]["end_epoch"], rows[1]["start_epoch"])
            self.assertTrue(rows[1]["last"])
            self.assertEqual((run / "R5_REC_DONE").read_text(), "0")
            self.assertTrue((run / "R6_REC_DONE").exists())
            for k in ("round", "raw", "ffmpeg_popen_epoch", "ffmpeg_input_start", "start_epoch", "end_epoch", "stop_reason"):
                self.assertIn(k, rows[0])


if __name__ == "__main__":
    unittest.main()
