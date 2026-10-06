import json
import sys
import tempfile
import time
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
        c.cost_async = False
        c.gating = False
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
        c.gating = False
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
        c.cost_async = False
        c.gating = False
        c.always = False
        self.assertIsNone(c.tick())
        self.assertEqual(c.http.calls, [])
        c.clips_all(0)  # an unmuted page polls
        lc.time.sleep = lambda _s: None
        self.assertIsNotNone(c.tick())


class RoundShowTest(unittest.TestCase):
    """Every round opens with a preview and closes with a recap; quiet stretches read the live thinking."""

    def tournament(self, moves=0, status="live", ended=None):
        import time as _t
        pairings = [{"board": 1, "white": "Gemini 3.8 Flash", "black": "Stockfish 19 (depth 4)", "game_id": "r2b1"},
                    {"board": 2, "white": "Muse Spark 1.3", "black": "Grok 4.7", "game_id": "r2b2"}]
        games = {p["game_id"]: {"id": p["game_id"], "round": 2, "board": p["board"], "white": p["white"],
                                "black": p["black"], "status": status, "result": "1-0" if status == "finished" else "*",
                                "termination": "checkmate" if status == "finished" else "", "end": ended,
                                "moves": [{"ply": i + 1, "side": "white" if i % 2 == 0 else "black", "san": "e4"}
                                          for i in range(moves)]} for p in pairings}
        standings = [{"name": n, "rank": i + 1, "points": 1.0 - i * 0.5 if i < 3 else 0.0, "played": 1, "wins": 1 if i < 2 else 0,
                      "draws": 0, "losses": 0 if i < 2 else 1}
                     for i, n in enumerate(["Gemini 3.8 Flash", "Stockfish 19 (depth 4)", "Grok 4.7", "Muse Spark 1.3"])]
        return {"id": "t1", "current_round": 2, "format": {"type": "round-robin+knockout", "rr_rounds": 9, "ko_size": 4},
                "rounds": [{"round": 1, "status": "finished", "pairings": []},
                           {"round": 2, "status": status, "pairings": pairings}],
                "games": games, "standings": standings, "updated_epoch_ms": _t.time() * 1000}

    def test_a_new_round_gets_a_preview_with_standings_and_pairings(self):
        self.assertIsNone(lc.next_event(self.tournament(), {"opening"}), "paired but no move yet: wait for the first move")
        event = lc.next_event(self.tournament(moves=1), {"opening"})
        self.assertEqual((event["key"], event["game"]), ("round-2", "r2b1"), "the leader's board carries the preview")
        self.assertIn("1. Gemini three point eight Flash 1 point (1 win, 0 draws, 0 losses)", event["facts"])
        self.assertIn("top 4 go through to the knockouts", event["facts"])
        self.assertIn("Board 2: Muse Spark one point three", event["facts"])
        self.assertIsNone(lc.next_event(self.tournament(moves=31), {"opening"}), "too far in: no late preview")
        self.assertIsNotNone(lc.next_event(self.tournament(moves=20), {"opening"}), "after a results card the first minute still counts")
        self.assertIsNone(lc.next_event(self.tournament(moves=1), {"opening", "round-2"}), "once per round")

    def test_a_paused_round_is_not_previewed(self):
        c, path = CommentaryTest().make(self.tournament(moves=1))
        state = self.tournament(moves=1)
        state["updated_epoch_ms"] = 0  # the runner stopped writing: paused
        path.write_text(json.dumps(state), encoding="utf-8")
        c._done_ply = {"r2b1": 1, "r2b2": 1}  # the move was already covered
        self.assertIsNone(c.tick())
        self.assertEqual(c.http.calls, [])

    def test_a_finished_round_gets_a_recap_with_a_tease(self):
        import datetime as dt
        state = self.tournament(status="finished", ended=dt.datetime.now().astimezone().isoformat())
        state["rounds"].append({"round": 3, "status": "live", "pairings": [
            {"board": 1, "white": "Grok 4.7", "black": "Gemini 3.8 Flash", "game_id": "r3b1"}]})
        event = lc.next_event(state, {"opening"})
        self.assertEqual(event["key"], "recap-2")
        self.assertIn("Gemini three point eight Flash beat Stockfish at depth four (checkmate)", event["facts"])
        self.assertIn("Next round, board 1: Grok four point seven against Gemini three point eight Flash", event["facts"])
        old = self.tournament(status="finished", ended="2020-01-01T00:00:00+02:00")
        self.assertIsNone(lc.next_event(old, {"opening"}), "an old round (resume after a pause) gets no recap")

    def test_quiet_boards_read_out_the_live_thinking_once(self):
        state = self.tournament(moves=8)
        c, path = CommentaryTest().make(state)
        c._done_ply = {"r2b1": 8, "r2b2": 8}  # nothing new on any board
        c._events_done.add("round-2")
        lc.THINK_LINES = True
        self.addCleanup(setattr, lc, "THINK_LINES", False)
        (path.parent / "t1-r2b2-ply9.thinking.txt").write_text("I weigh Nf3 against the pin on e5. " * 20, encoding="utf-8")
        clip = c.tick()
        self.assertTrue(clip and clip.get("thinking"))
        self.assertEqual(clip["ply"], 8)
        prompt = [b for p, b in c.http.calls if p == "/chat/completions"][-1]["messages"]
        self.assertEqual(prompt[0]["content"], lc.THINKING_PROMPT)
        self.assertIn("the pin on e5", prompt[1]["content"])
        self.assertIn("Muse Spark one point three (white) is thinking", prompt[1]["content"])
        c._busy_until = c._quiet_from = 0
        self.assertIsNone(c.tick(), "the same thinking is read out once")

    def test_clips_carry_their_age_so_the_viewer_can_drop_late_ones(self):
        c, _ = CommentaryTest().make(state_with([{"ply": 1, "side": "white", "san": "Nf3"}]))
        clip = c.tick()
        got = c.clips_all(0)[0]
        self.assertEqual(got["seq"], clip["seq"])
        self.assertGreaterEqual(got["age_s"], 0)
        self.assertLess(got["age_s"], 5)


def real_moves(san_list):
    """Moves as the tournament state stores them (ply, side, san, uci), replayed with python-chess."""
    import chess
    board, out = chess.Board(), []
    for i, san in enumerate(san_list):
        move = board.parse_san(san)
        out.append({"ply": i + 1, "side": "white" if i % 2 == 0 else "black", "san": san, "uci": move.uci()})
        board.push(move)
    return out


OPENING = ["e4", "c5", "Nf3", "d6", "d4", "cxd4", "Nxd4", "Nf6", "Nc3", "a6"]


class DirectorTest(unittest.TestCase):
    """The host only speaks for a reason; the rest is quiet (the viewer time-lapses it)."""

    def make(self, moves, marks=None, positions=None, clocks=None):
        game = {"id": "r5b1", "board": 1, "round": 5, "white": "Grok 4.7", "black": "GPT-6.1 Sol", "status": "live",
                "result": "*", "moves": moves, "clocks": clocks or {"white": 300000, "black": 300000}}
        state = {"id": "t1", "games": {"r5b1": game}, "standings": [
            {"name": "Grok 4.7", "rank": 1, "points": 2, "played": 3},
            {"name": "GPT-6.1 Sol", "rank": 6, "points": 1, "played": 3}],
            "current_round": 5, "updated_epoch_ms": time.time() * 1000}
        tmp = Path(tempfile.mkdtemp())
        path = tmp / "t1-tournament.json"
        path.write_text(json.dumps(state), encoding="utf-8")
        (tmp / "t1-annotations.json").write_text(json.dumps({"annotations": {"r5b1": marks or {}},
                                                              "positions": positions or {}}), encoding="utf-8")
        c = lc.Commentator(path, tmp / "out", log=lambda _m: None, budget_usd=1.0)
        c.http = FakeHttp()
        c.cost_async = False
        c.always = True
        c._events_done.update({"opening", "round-5"})
        lc.time.sleep = lambda _s: None
        return c, state

    def prompt(self, c):
        return [b for p, b in c.http.calls if p == "/chat/completions"][-1]["messages"][1]["content"]

    def test_a_boring_start_is_quiet_and_the_opening_is_named_once(self):
        c, _ = self.make(real_moves(OPENING[:6]))
        self.assertIsNone(c.tick(), "6 half-moves: nothing to say yet")
        self.assertIsNotNone(c._quiet_since)
        self.assertGreaterEqual(c.quiet_s(), 0.0)
        c, _ = self.make(real_moves(OPENING))
        clip = c.tick()
        self.assertEqual(clip["reason"], "opening")
        self.assertIsNone(c._quiet_since, "talking is not quiet")
        self.assertIn("Opening moves: 1. e4 c5 2. Nf3 d6 3. d4 cxd4", self.prompt(c))
        self.assertIn("name the opening", self.prompt(c))
        c._busy_until = 0
        self.assertIsNone(c.tick(), "named once; the next plies are boring")

    def test_critical_blunders_are_called_out_but_folded_together(self):
        c, _ = self.make(real_moves(OPENING), marks={"9": "??"})
        c._said["r5b1"] = {"opening"}
        c._done_ply["r5b1"] = 8
        clip = c.tick()
        self.assertEqual(clip["reason"], "blunder")
        self.assertIn("CRITICAL MISTAKE", self.prompt(c))
        self.assertEqual(c._last_blunder["r5b1"], 10)
        game = {"id": "r5b1", "white": "Grok 4.7", "black": "GPT-6.1 Sol", "status": "live", "clocks": {},
                "moves": real_moves(OPENING + ["Be3", "e5"])}
        c._done_ply["r5b1"] = 10
        self.assertEqual(c._board_reason("r5b1", game, {"11": "??"}, {}, {})[0], "", "2 plies after a blunder line: folded")
        game["moves"] = real_moves(OPENING + ["Be3", "e5", "Nb3", "Be7", "f3", "O-O", "Qd2", "Nbd7"])
        self.assertEqual(c._board_reason("r5b1", game, {"17": "?"}, {}, {})[0], "mistake", "8 plies later: a new story")

    def test_endgame_decided_and_drawish_each_get_one_line(self):
        c, _ = self.make(real_moves(OPENING))
        moves = [{"ply": i + 1, "side": "white" if i % 2 == 0 else "black", "san": "Kf1"} for i in range(30)]
        game = {"id": "r5b1", "white": "Grok 4.7", "black": "GPT-6.1 Sol", "status": "live", "clocks": {}, "moves": moves}
        c._said["r5b1"] = {"opening"}
        c._done_ply["r5b1"] = 28
        real = lc.analyse_game
        self.addCleanup(setattr, lc, "analyse_game", real)
        lc.analyse_game = lambda g, pos: {"cps": [120] * 30, "material": 20}
        self.assertEqual(c._board_reason("r5b1", game, {}, {}, {})[0], "endgame")
        c._said["r5b1"].add("endgame")
        lc.analyse_game = lambda g, pos: {"cps": [900] * 30, "material": 20}
        self.assertEqual(c._board_reason("r5b1", game, {}, {}, {})[0], "decided")
        c._said["r5b1"].add("decided")
        self.assertEqual(c._board_reason("r5b1", game, {}, {}, {})[0], "", "decided: only critical things are said")
        c._said["r5b1"] = {"opening", "endgame"}
        game["moves"] = moves + moves[:14]               # 44 half-moves
        lc.analyse_game = lambda g, pos: {"cps": [10] * 44, "material": 20}
        c._done_ply["r5b1"] = 43
        self.assertEqual(c._board_reason("r5b1", game, {}, {}, {})[0], "draw")
        c._said["r5b1"].add("draw")
        c._done_ply["r5b1"] = 0
        self.assertEqual(c._board_reason("r5b1", game, {}, {}, {"Grok 4.7": 1})[0], "", "drawish and said: quiet")

    def test_leaders_and_updates_only_after_a_silence(self):
        c, _ = self.make(real_moves(OPENING))
        game = {"id": "r5b1", "white": "Grok 4.7", "black": "GPT-6.1 Sol", "status": "live", "clocks": {},
                "moves": real_moves(OPENING)}
        c._said["r5b1"] = {"opening"}
        ranks = {"Grok 4.7": 1, "GPT-6.1 Sol": 6}
        c._done_ply["r5b1"] = 5                    # 5 plies since the last line
        self.assertEqual(c._board_reason("r5b1", game, {}, {}, ranks)[0], "")
        c._done_ply["r5b1"] = 0                    # 10 silent plies on a top-two board
        self.assertEqual(c._board_reason("r5b1", game, {}, {}, ranks)[0], "leaders")
        self.assertEqual(c._board_reason("r5b1", game, {}, {}, {})[0], "", "a low board waits for 20 plies")

    def test_scores_are_read_from_whites_side(self):
        import chess
        moves = real_moves(["e4", "e5"])
        board = chess.Board()
        positions = {}
        board.push_san("e4")
        positions[board.fen()] = {"cp": -33}       # Black to move, 33 centipawns for White
        board.push_san("e5")
        positions[board.fen()] = {"cp": 25}        # White to move, +25 for White
        got = lc.analyse_game({"moves": moves}, positions)
        self.assertEqual(got["cps"], [33, 25])
        self.assertEqual(got["material"], 62)
        self.assertEqual(lc.band(-520), "winning for Black")
        self.assertEqual(lc.band(40), "roughly equal")

    def test_a_tour_holds_the_host_then_sums_up_what_changed(self):
        c, state = self.make(real_moves(OPENING))
        c._said["r5b1"] = {"opening"}
        c._done_ply["r5b1"] = 10
        c.tour(True)
        self.assertTrue(c.held())
        self.assertIsNone(c.tick())
        self.assertEqual(c.http.calls, [])
        state["games"]["r5b1"]["moves"] = real_moves(OPENING + ["Be3", "e5"])   # time passes
        c.state_path.write_text(json.dumps(state), encoding="utf-8")
        c.tour(False)
        self.assertFalse(c.held())
        clip = c.tick()
        self.assertEqual(clip["event"], "tour-1")
        self.assertIn("time-lapse tour", self.prompt(c))
        self.assertIn("Board 1: Grok four point seven against GPT six point one Sol", self.prompt(c))

    def test_a_stuck_tour_releases_the_host(self):
        c, _ = self.make(real_moves(OPENING[:4]))
        c.tour(True)
        c._tour_until = time.time() - 1
        self.assertFalse(c.held())


if __name__ == "__main__":
    unittest.main()
