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


if __name__ == "__main__":
    unittest.main()
