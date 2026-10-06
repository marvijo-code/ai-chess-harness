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


class FastForwardTests(unittest.TestCase):
    def test_merge_and_subtract(self):
        self.assertEqual(tl.merge_intervals([(5, 8), (0, 2), (1, 3), (8, 9)]), [(0, 3), (5, 9)])
        self.assertEqual(tl.merge_intervals([(0, 2), (2.5, 3)], join=1.0), [(0, 3)])
        self.assertEqual(tl.subtract_intervals((0, 100), [(10, 20), (15, 30), (90, 120)]), [(0, 10), (30, 90)])
        self.assertEqual(tl.subtract_intervals((0, 10), [(-5, 50)]), [])

    def test_clip_busy_lead_and_tail(self):
        self.assertEqual(tl.clip_busy([(100.0, 110.0)]), [(99.0, 110.5)])

    def test_gaps_longer_than_min_become_ff(self):
        busy = tl.clip_busy([(20.0, 30.0), (45.0, 50.0), (120.0, 130.0)])
        ff = tl.ff_spans(0.0, 200.0, busy, [])
        # 0..19 (19 s) yes, 30.5..44 (13.5 s) yes, 50.5..119 yes, 130.5..200 yes
        self.assertEqual([(round(a, 3), round(b, 3)) for a, b, _ in ff],
                         [(0.0, 19.0), (30.5, 44.0), (50.5, 119.0), (130.5, 200.0)])
        self.assertTrue(all(s == tl.FF_SPEED for *_, s in ff))

    def test_short_gap_stays_normal(self):
        busy = tl.clip_busy([(20.0, 30.0), (40.0, 50.0)])       # gap 30.5..39 = 8.5 s < 10
        ff = tl.ff_spans(19.0, 50.5, busy, [])
        self.assertEqual(ff, [])
        busy = tl.clip_busy([(20.0, 30.0), (41.5, 50.0)])       # gap 30.5..40.5 = exactly 10 s: not longer
        self.assertEqual(tl.ff_spans(19.0, 50.5, busy, []), [])

    def test_tours_are_not_fast_forwarded_and_ff_fills_next_to_them(self):
        ff = tl.ff_spans(0.0, 300.0, tl.clip_busy([(0.0, 10.0)]), [(50.0, 150.0, 10)])
        self.assertEqual([(a, b) for a, b, _ in ff], [(10.5, 50.0), (150.0, 300.0)])

    def test_cards_and_champion_are_busy(self):
        evs = [page("director", 50, k="rcard", a="show", mode="results", round=5),
               page("director", 80, k="rcard", a="hide", mode="results", round=5),
               page("director", 150, k="champion", a="show"),
               page("director", 400, k="rcard", a="hide", mode="intro")]           # stray hide: ignored
        iv = tl.overlay_intervals(evs, T0, T0 + 200)
        self.assertEqual([(round(a - T0, 3), round(b - T0, 3)) for a, b in iv], [(50.0, 80.0), (150.0, 200.0)])
        plan = tl.plan_spans(evs, T0, T0, T0 + 200, 0.0, 200.0, [])
        self.assertEqual([(round(a, 3), round(b, 3)) for a, b, _ in plan["ff"]], [(0.0, 50.0), (80.0, 150.0)])

    def test_tour_never_starts_inside_speech(self):
        trimmed = tl.trim_tours([(100.0, 200.0, 10)], tl.clip_busy([(95.0, 104.0)]))
        self.assertEqual(trimmed, [(104.5, 200.0, 10)])
        self.assertEqual(tl.trim_tours([(100.0, 106.0, 10)], tl.clip_busy([(95.0, 99.0)])), [])   # too short left

    def test_plan_segments_frame_exact_and_kinds(self):
        evs = [tour("start", 300, speed=10), tour("end", 420)]
        clips = [(5.0, 15.0), (200.0, 212.0), (299.0, 303.0), (500.0, 510.0)]
        plan = tl.plan_spans(evs, T0, T0, T0 + 600, 0.0, 600.0, clips)
        segk = plan["segments"]
        self.assertEqual(segk[0][0], 0.0)
        self.assertEqual(segk[-1][1], 600.0)
        for (a, b, s, k), nxt in zip(segk, segk[1:] + [None]):
            self.assertAlmostEqual(a * 30, round(a * 30), places=4)
            if nxt:
                self.assertAlmostEqual(b, nxt[0])
            if s > 1:
                self.assertEqual(round((b - a) * 30) % s, 0)
            for cs, ce in clips:                        # no sped-up segment overlaps speech
                if s > 1:
                    self.assertFalse(a < ce and cs < b, (a, b, cs, ce))
        kinds = [k for *_, k in segk]
        self.assertIn("ff", kinds)
        self.assertIn("tour", kinds)
        tour_seg = [x for x in segk if x[3] == "tour"][0]
        self.assertGreaterEqual(tour_seg[0], 303.5 - 1 / 30)      # trimmed past the clip tail
        segs = [x[:3] for x in segk]
        self.assertAlmostEqual(tl.output_duration(segs), sum(r[5] for r in tl.output_table(segs)) / 30)
        # clip offsets stay where the plan put them (linear inside normal segments)
        self.assertAlmostEqual(tl.remap(200.0, segs) + 12.0, tl.remap(212.0, segs), places=6)

    def test_no_fast_forward_switch(self):
        plan = tl.plan_spans([], T0, T0, T0 + 100, 0.0, 100.0, [], fast_forward=False)
        self.assertEqual(plan["ff"], [])
        self.assertEqual(plan["segments"], [(0.0, 100.0, 1, "normal")])

    def test_cue_only_on_ff_segments(self):
        segk = tl.build_segments_k(0.0, 100.0, [(10.0, 40.0, 5, "ff"), (50.0, 80.0, 10, "tour")])
        cue = tl.cue_filter(">> x5", "/f/DejaVuSans-Bold.ttf")
        vf = tl.video_filter([s[:3] for s in segk], kinds=[s[3] for s in segk], cue=cue)
        lines = vf.split(";\n")
        self.assertEqual(sum("drawtext" in ln for ln in lines), 1)
        self.assertIn("fps=30,drawtext=", [ln for ln in lines if "/5" in ln][0])
        self.assertIn("fontcolor=0xffd479", cue)


@unittest.skipIf(mix is None, "numpy not installed")
class MasterAudioTests(unittest.TestCase):
    def test_retries_until_loudness_and_peak_fit(self):
        calls = []
        seq = iter([(-16.7, -1.1), (-16.1, -1.7)])

        def fake_ebur(path):
            if str(path).endswith("premix.flac"):
                return -22.0, -8.0, ""
            i, tp = next(seq)
            return i, tp, ""

        old_ebur, old_sh = mix.ebur, mix.sh
        mix.ebur, mix.sh = fake_ebur, lambda cmd: calls.append(cmd)
        try:
            res = mix.master_audio(Path("premix.flac"), Path("final.flac"), 60.0)
        finally:
            mix.ebur, mix.sh = old_ebur, old_sh
        self.assertEqual(res["attempts"], 2)
        self.assertEqual(len(calls), 2)
        self.assertAlmostEqual(res["gain_db"], 6.0 + 0.7, places=3)
        self.assertLess(res["limit_db"], mix.FINAL_LIMIT_DB)        # lowered after the -1.1 dBTP miss
        self.assertIn("latency=1", " ".join(map(str, calls[0])))


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
