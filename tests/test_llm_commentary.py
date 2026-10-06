import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import llm_commentary as lc  # noqa: E402


def state_with(moves, status="live", result="*", termination=""):
    return {"id": "t1", "games": {"r1b1": {"id": "r1b1", "white": "Grok 4.7", "black": "GPT-6.1 Sol", "status": status,
                                           "result": result, "termination": termination, "moves": moves}}}


class FakeHttp:
    def __init__(self):
        self.calls = []

    def __call__(self, path, body):
        self.calls.append((path, body))
        if path == "/chat/completions":
            return json.dumps({"choices": [{"message": {"content": "Grok **storms** in — Knight to f3!"}}],
                               "usage": {"cost": 0.0001}}).encode(), {}
        if path == "/audio/speech":
            return b"\x00\x00" * 24000, {"X-Generation-Id": "gen-1"}
        return json.dumps({"data": {"total_cost": 0.0028}}).encode(), {}


class CommentaryTest(unittest.TestCase):
    def make(self, state, budget=1.0):
        tmp = Path(tempfile.mkdtemp())
        path = tmp / "t1-tournament.json"
        path.write_text(json.dumps(state), encoding="utf-8")
        c = lc.Commentator(path, tmp / "out", log=lambda _m: None, budget_usd=budget)
        c.http = FakeHttp()
        c.always = True
        lc.time.sleep = lambda _s: None
        return c, path

    def test_a_new_move_gets_one_spoken_clip_with_clean_text(self):
        c, _ = self.make(state_with([{"ply": 1, "side": "white", "san": "Nf3", "comment": "develop"}]))
        clip = c.tick()
        self.assertEqual(clip["ply"], 1)
        self.assertNotIn("*", clip["text"])
        self.assertNotIn("—", clip["text"], "no dashes in spoken lines")
        self.assertAlmostEqual(clip["seconds"], 1.0)
        self.assertIsNotNone(c.audio_path(clip["audio"]))
        self.assertEqual(c.clips("r1b1", 0)[0]["seq"], clip["seq"])
        self.assertAlmostEqual(c.spent_usd, 0.0029)
        tts = [b for p, b in c.http.calls if p == "/audio/speech"][0]
        self.assertEqual((tts["model"], tts["response_format"]), (lc.TTS_MODEL, "pcm"))
        self.assertEqual(tts["input"], clip["text"], "only the commentary line is spoken, never the style prompt")
        self.assertEqual(tts["instructions"], lc.TTS_STYLE)

    def test_no_new_move_means_no_call_and_the_gap_is_respected(self):
        c, _ = self.make(state_with([{"ply": 1, "side": "white", "san": "e4"}]))
        c.tick()
        calls = len(c.http.calls)
        self.assertIsNone(c.tick(), "same position: nothing to say")
        self.assertEqual(len(c.http.calls), calls)

    def test_budget_cap_stops_all_calls(self):
        c, _ = self.make(state_with([{"ply": 1, "side": "white", "san": "e4"}]), budget=0.0)
        self.assertIsNone(c.tick())
        self.assertEqual(c.http.calls, [])

    def test_audio_names_cannot_escape_the_folder(self):
        c, _ = self.make(state_with([]))
        self.assertIsNone(c.audio_path("../secret.wav"))
        self.assertIsNone(c.audio_path("clip-1.mp3"))

    def test_context_uses_spoken_names_marks_and_reasons(self):
        game = state_with([{"ply": 1, "side": "white", "san": "e4", "comment": "center"},
                           {"ply": 2, "side": "black", "san": "f6"}])["games"]["r1b1"]
        text = lc.build_context(game, {"2": "?"}, 1)
        self.assertIn("Grok four point seven", text)
        self.assertIn("played f6?", text)
        self.assertIn("Its own reason: center", text)


class RoamingTest(unittest.TestCase):
    def state(self):
        quiet = {"id": "r1b1", "board": 1, "round": 2, "white": "Muse Spark 1.3", "black": "Qwen 3.8 Omni Flash",
                 "status": "live", "result": "*", "moves": [{"ply": 1, "side": "white", "san": "e4"}]}
        sharp = {"id": "r1b2", "board": 2, "round": 2, "white": "Grok 4.7", "black": "GPT-6.1 Sol", "status": "live",
                 "result": "*", "moves": [{"ply": 1, "side": "white", "san": "e4"}, {"ply": 2, "side": "black", "san": "Qh4"}]}
        standings = [{"name": "Grok 4.7", "rank": 1, "points": 1, "played": 1},
                     {"name": "GPT-6.1 Sol", "rank": 2, "points": 1, "played": 1},
                     {"name": "Muse Spark 1.3", "rank": 9, "points": 0, "played": 1}]
        return {"id": "t1", "games": {"r1b1": quiet, "r1b2": sharp}, "standings": standings}

    def make(self, state):
        tmp = Path(tempfile.mkdtemp())
        path = tmp / "t1-tournament.json"
        path.write_text(json.dumps(state), encoding="utf-8")
        (tmp / "t1-annotations.json").write_text(json.dumps({"annotations": {"r1b2": {"2": "??"}}}), encoding="utf-8")
        c = lc.Commentator(path, tmp / "out", log=lambda _m: None, budget_usd=1.0)
        c.http = FakeHttp()
        c.always = True
        lc.time.sleep = lambda _s: None
        return c

    def test_leaders_and_a_blunder_win_the_commentary(self):
        c = self.make(self.state())
        c._events_done.add("opening")
        clip = c.tick()
        self.assertEqual(c.clips_all(0)[0]["game"], "r1b2")
        context = [b for p, b in c.http.calls if p == "/chat/completions"][0]["messages"][1]["content"]
        self.assertIn("Board 2", context)
        self.assertIn("just moved to this board", context)
        self.assertIn("Grok four point seven 1 point;", context)
        self.assertEqual(clip["ply"], 2)

    def test_a_pinned_board_keeps_the_commentary_and_a_hint_does_not(self):
        c = self.make(self.state())
        c.focus("r1b1")  # unpinned hint from the all-boards grid: still roaming
        self.assertEqual(c.pick(self.state(), {"r1b2": {"2": "??"}}), "r1b2")
        c.focus("r1b1", pinned=True)
        self.assertEqual(c.pick(self.state(), {"r1b2": {"2": "??"}}), "r1b1")
        c.focus(None)
        self.assertEqual(c.pick(self.state(), {"r1b2": {"2": "??"}}), "r1b2")

    def test_a_fresh_result_gets_one_closing_line(self):
        state = self.state()
        state["games"]["r1b1"].update(status="finished", result="0-1", termination="checkmate")
        state["games"]["r1b2"]["moves"] = state["games"]["r1b2"]["moves"][:1]
        c = self.make(state)
        c._done_ply = {"r1b1": 1, "r1b2": 1}
        self.assertEqual(c.pick(state, {}), "r1b1")
        c.tick()
        self.assertIsNone(c.pick(state, {}), "result said once, no new moves elsewhere")


class EventTest(unittest.TestCase):
    def test_the_big_moments_are_announced_once_each(self):
        state = RoamingTest().state()
        state["format"] = {"type": "round-robin+knockout", "rr_rounds": 9, "ko_size": 4}
        state["players"] = [{"name": "Grok 4.7"}, {"name": "GPT-6.1 Sol"}]
        done = set()
        self.assertEqual(lc.next_event(state, done)["key"], "opening")
        self.assertIn("round robin of 9 rounds", lc.next_event(state, done)["facts"])
        done.add("opening")
        self.assertIsNone(lc.next_event(state, done), "nothing big mid round robin")
        state["stage"] = "semifinals"
        state["knockout"] = {"seeds": [{"seed": i + 1, "name": n, "points": 6 - i} for i, n in enumerate("ABCD")],
                             "matches": [{"id": "sf1", "stage": "semifinals", "label": "Semifinal 1", "a": "A", "b": "D",
                                          "games": ["r10b1", "r10b3"]}]}
        state["games"]["r10b1"] = {"id": "r10b1", "white": "A", "black": "D", "status": "finished", "result": "1/2-1/2", "moves": []}
        state["games"]["r10b3"] = {"id": "r10b3", "white": "D", "black": "A", "status": "live", "result": "*",
                                   "armageddon": True, "moves": []}
        event = lc.next_event(state, done)
        self.assertEqual(event["key"], "arm-r10b3", "a live Armageddon decider beats the knockout intro")
        done.add(event["key"])
        self.assertEqual(lc.next_event(state, done)["key"], "knockouts")
        state["knockout"].update(champion="A", runner_up="B", third="C")
        state["knockout"]["matches"].append({"id": "final", "stage": "final", "label": "Final", "a": "A", "b": "B",
                                             "games": ["r11b1"], "decided_by": "game"})
        event = lc.next_event(state, done)
        self.assertEqual((event["key"], event["game"]), ("champion", "r11b1"))
        self.assertIn("outro", event["facts"])


class ListenerTest(unittest.TestCase):
    def test_no_listener_means_no_spending(self):
        tmp = Path(tempfile.mkdtemp())
        path = tmp / "t1-tournament.json"
        path.write_text(json.dumps(state_with([{"ply": 1, "side": "white", "san": "e4"}])), encoding="utf-8")
        c = lc.Commentator(path, tmp / "out", log=lambda _m: None, budget_usd=1.0)
        c.http = FakeHttp()
        c.always = False
        self.assertIsNone(c.tick())
        self.assertEqual(c.http.calls, [])
        c.clips_all(0)  # an unmuted page polls
        lc.time.sleep = lambda _s: None
        self.assertIsNotNone(c.tick())


if __name__ == "__main__":
    unittest.main()
