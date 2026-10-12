"""The waiting screen: what is next, the countdown from the usual wait, and the winners banner."""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import llm_tournament_viewer as viewer  # noqa: E402

NODE = shutil.which("node")
T0 = 1_790_000_000   # a fixed second on the clock


def iso(seconds: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(T0 + seconds, timezone.utc).isoformat(timespec="seconds")


def game(gid, white, black, start, end=None, result="*", **extra):
    out = {"id": gid, "white": white, "black": black, "start": iso(start), "result": result,
           "status": "finished" if end is not None else "live"}
    if end is not None:
        out["end"] = iso(end)
    out.update(extra)
    return out


def tournament(number, games, finished_at=None):
    return {"id": f"t{number:04d}", "number": number, "finished": finished_at is not None,
            "updated_epoch_ms": int((T0 + (finished_at or 0)) * 1000), "games": {g["id"]: g for g in games}}


class WaitStatsTest(unittest.TestCase):
    def setUp(self):
        viewer._timing_cache.clear()
        viewer._wait_cache.update(at=0.0, dir=None, stats={})
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def put(self, state):
        (self.dir / f"{state['id']}-tournament.json").write_text(json.dumps(state), encoding="utf-8")

    def test_gaps_count_only_time_with_no_live_game(self):
        timing = viewer.tournament_timing(tournament(1, [
            game("r1b1", "A", "B", 0, 600, "1-0"), game("r1b2", "C", "D", 10, 300, "0-1"),   # b2 ends first: no gap
            game("r2b1", "A", "C", 800, 1500, "1-0"), game("r2b2", "B", "D", 810, 1200, "1/2-1/2"),
            game("r3b1", "A", "D", 1600, 2000, "1-0")], finished_at=2100))
        self.assertEqual(timing["gaps"], [200, 100])
        self.assertEqual(timing["first_start"], T0)
        self.assertEqual(timing["finished_at"], T0 + 2100)

    def test_a_live_game_closes_no_gap_and_an_outage_is_left_out(self):
        timing = viewer.tournament_timing(tournament(2, [
            game("r1b1", "A", "B", 0, 100, "1-0"), game("r1b2", "C", "D", 0),               # still live
            game("r2b1", "A", "C", 400, 500, "1-0")]))
        self.assertEqual(timing["gaps"], [], "the live game covers the time up to the next start")
        self.assertIsNone(timing["finished_at"])
        outage = viewer.tournament_timing(tournament(3, [
            game("r1b1", "A", "B", 0, 100, "1-0"), game("r2b1", "A", "C", 100 + viewer.WAIT_GAP_MAX_S + 1, 9000, "1-0")]))
        self.assertEqual(outage["gaps"], [])

    def test_medians_over_the_live_dir(self):
        self.put(tournament(1, [game("r1b1", "A", "B", 0, 100, "1-0"), game("r2b1", "A", "C", 300, 400, "1-0")], finished_at=420))
        self.put(tournament(2, [game("r1b1", "A", "B", 550, 700, "1-0"), game("r2b1", "A", "C", 800, 900, "1-0"),
                                game("r3b1", "B", "C", 1300, 1400, "1-0")], finished_at=1410))
        self.put(tournament(3, [game("r1b1", "A", "B", 1560, 1700, "1-0")]))
        stats = viewer.wait_stats(self.dir, now=1.0)
        self.assertEqual(stats, {"game_typical_s": 200, "game_samples": 3, "tournament_typical_s": 140, "tournament_samples": 2})

    def test_no_sample_no_number(self):
        self.put(tournament(1, [game("r1b1", "A", "B", 0)]))
        self.assertEqual(viewer.wait_stats(self.dir, now=1.0), {})
        self.assertEqual(viewer.wait_stats(self.dir / "missing", now=1.0), {})

    def test_a_skipped_tournament_number_is_not_a_break(self):
        self.put(tournament(1, [game("r1b1", "A", "B", 0, 100, "1-0")], finished_at=110))
        self.put(tournament(3, [game("r1b1", "A", "B", 250, 300, "1-0")]))
        self.assertNotIn("tournament_typical_s", viewer.wait_stats(self.dir, now=1.0))


class WaitPageTest(unittest.TestCase):
    def test_page_has_the_waiting_screen(self):
        page = viewer.PAGE
        for needle in ("function waitPlan(", "function waitHtml(", 'el.id = "waitScreen"', "d.wait_stats",
                       "body.stream.waiting .boards > .card[data-game] { display: none; }", "any moment now", "Up next"):
            self.assertIn(needle, page)

    def test_tournament_answer_carries_the_usual_waits(self):
        self.assertIn('state["wait_stats"] = wait_stats(self.live_dir, now)', Path(viewer.__file__).read_text(encoding="utf-8"))


def plan_source() -> str:
    page = viewer.PAGE
    return page[page.index("// WAIT-PLAN-START"):page.index("// WAIT-PLAN-END")]


NOW_MS = (T0 + 5000) * 1000
RR = {"type": "round-robin+knockout", "rr_rounds": 3, "ko_size": 4}


def rr_state(**over):
    """Round 2 of 3 is over (last game ended 60 s ago), nothing is live."""
    games = [game("r1b1", "A", "B", 3000, 3500, "1-0"), game("r1b2", "C", "D", 3000, 3400, "1/2-1/2"),
             game("r2b1", "A", "C", 4000, 4940, "0-1"), game("r2b2", "B", "D", 4000, 4700, "1/2-1/2")]
    state = {"id": "t0007", "number": 7, "finished": False, "stage": "round-robin", "current_round": 2, "format": RR,
             "config": {"rounds": 3}, "updated_epoch_ms": (T0 + 4940) * 1000,
             "games": {g["id"]: g for g in games},
             "rounds": [{"round": 1, "status": "finished", "pairings": [{"game_id": "r1b1"}, {"game_id": "r1b2"}]},
                        {"round": 2, "status": "finished", "pairings": [{"game_id": "r2b1"}, {"game_id": "r2b2"}]}],
             "standings": [{"rank": 1, "name": "C", "points": 1.5, "played": 2}, {"rank": 2, "name": "A", "points": 1, "played": 2}],
             "wait_stats": {"game_typical_s": 200, "game_samples": 9, "tournament_typical_s": 130, "tournament_samples": 4}}
    state.update(over)
    return state


@unittest.skipUnless(NODE, "node is not installed")
class WaitPlanTest(unittest.TestCase):
    """waitPlan() from the page, run in node."""

    def plan(self, state, now_ms=NOW_MS):
        script = plan_source() + f"\nconsole.log(JSON.stringify(waitPlan({json.dumps(state)}, {now_ms})));"
        return json.loads(subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=60, check=True).stdout)

    def test_between_rounds_counts_down_the_usual_wait(self):
        plan = self.plan(rr_state())
        self.assertEqual((plan["kind"], plan["elapsedS"], plan["leftS"], plan["pct"], plan["overdue"]), ("game", 60, 140, 30, False))
        self.assertEqual(plan["next"], {"what": "game", "label": "Round 3 of 3", "detail": ""})
        win = plan["winners"]
        self.assertEqual(win["title"], "Round 2: winners")
        self.assertEqual(win["rows"], [{"tag": "", "winner": "C", "loser": "A", "how": "beat"}, {"tag": "", "draw": ["B", "D"]}])
        self.assertEqual(win["leader"], {"name": "C", "points": 1.5})

    def test_a_live_game_or_no_finished_game_means_no_screen(self):
        live = rr_state()
        live["games"]["r3b1"] = game("r3b1", "A", "D", 4990)
        self.assertIsNone(self.plan(live))
        fresh = rr_state(games={"r1b1": {"id": "r1b1", "status": "pending", "result": "*", "white": "A", "black": "B"}})
        self.assertIsNone(self.plan(fresh))
        self.assertIsNone(self.plan(rr_state(archived=True)))

    def test_past_the_usual_wait_it_never_invents_a_time(self):
        plan = self.plan(rr_state(), now_ms=(T0 + 4940 + 250) * 1000)
        self.assertEqual((plan["leftS"], plan["overdue"], plan["long"]), (0, True, False))
        self.assertTrue(self.plan(rr_state(), now_ms=(T0 + 4940 + 600) * 1000)["long"])
        bare = self.plan(rr_state(wait_stats={}))
        self.assertEqual((bare["typicalS"], bare["leftS"], bare["pct"], bare["elapsedS"]), (None, None, None, 60))

    def test_a_round_that_is_paired_but_not_started_is_the_next_one(self):
        state = rr_state(current_round=3)
        state["rounds"].append({"round": 3, "status": "live", "pairings": [{"game_id": "r3b1"}]})
        state["games"]["r3b1"] = {"id": "r3b1", "status": "pending", "result": "*", "white": "A", "black": "D"}
        self.assertEqual(self.plan(state)["next"]["label"], "Round 3 of 3")

    def test_last_round_robin_round_leads_to_the_knockouts(self):
        state = rr_state(current_round=3)
        state["rounds"].append({"round": 3, "status": "finished", "pairings": [{"game_id": "r3b1"}]})
        state["games"]["r3b1"] = game("r3b1", "A", "D", 4950, 4960, "1-0")
        self.assertEqual(self.plan(state)["next"]["label"], "The Semifinals")
        self.assertEqual(self.plan(dict(state, format=dict(RR, ko_size=2)))["next"]["label"], "The Final")

    def knockout(self, stage, matches, games, **over):
        state = rr_state(stage=stage, current_round=4, **over)
        state["knockout"] = {"seeds": [{"seed": 1, "name": "A"}], "champion": None, "matches": matches}
        state["rounds"].append({"round": 4, "stage": stage, "label": stage.capitalize(), "status": "live",
                                "pairings": [{"game_id": g["id"], "label": g.get("label", "")} for g in games]})
        for g in games:
            state["games"][g["id"]] = g
        return state

    def test_a_drawn_knockout_game_leads_to_the_armageddon(self):
        games = [game("r4b1", "A", "D", 4950, 4990, "1-0", label="Semifinal 1 - Game 1"),
                 game("r4b2", "C", "B", 4950, 4995, "1/2-1/2", label="Semifinal 2 - Game 1")]
        matches = [{"id": "sf1", "stage": "semifinals", "label": "Semifinal 1", "games": ["r4b1"], "winner": "A"},
                   {"id": "sf2", "stage": "semifinals", "label": "Semifinal 2", "games": ["r4b2"], "winner": None}]
        plan = self.plan(self.knockout("semifinals", matches, games))
        self.assertEqual(plan["next"], {"what": "game", "label": "Armageddon decider", "detail": "Semifinal 2"})
        self.assertEqual(plan["winners"]["title"], "Semifinals: winners")
        self.assertIsNone(plan["winners"]["leader"], "no table leader once the knockouts are seeded")
        self.assertEqual(plan["winners"]["rows"][0], {"tag": "Semifinal 1 - Game 1", "winner": "A", "loser": "D", "how": "beat"})

    def test_decided_semifinals_lead_to_the_final_and_a_decided_final_to_the_crown(self):
        games = [game("r4b1", "A", "D", 4950, 4990, "1-0"), game("r4b2", "C", "B", 4950, 4995, "0-1")]
        matches = [{"id": "sf1", "stage": "semifinals", "label": "Semifinal 1", "games": ["r4b1"], "winner": None},
                   {"id": "sf2", "stage": "semifinals", "label": "Semifinal 2", "games": ["r4b2"], "winner": None}]
        self.assertEqual(self.plan(self.knockout("semifinals", matches, games))["next"]["label"], "The Final")
        arma = [game("r4b1", "A", "B", 4950, 4970, "1/2-1/2"), game("r4b2", "B", "A", 4975, 4995, "1/2-1/2", armageddon=True)]
        final = [{"id": "final", "stage": "final", "label": "Final", "games": ["r4b1", "r4b2"], "winner": None}]
        plan = self.plan(self.knockout("final", final, arma))
        self.assertEqual(plan["next"]["what"], "crown")
        self.assertEqual(plan["winners"]["rows"][1]["winner"], "A", "a drawn Armageddon goes to Black")

    def test_after_the_tournament_the_champion_and_the_break(self):
        state = rr_state(finished=True, stage="finished", winner="C", updated_epoch_ms=(T0 + 4970) * 1000)
        state["knockout"] = {"champion": "C", "runner_up": "A", "third": "B", "seeds": [], "matches": []}
        plan = self.plan(state)
        self.assertEqual((plan["kind"], plan["elapsedS"], plan["leftS"]), ("tournament", 30, 100))
        self.assertEqual(plan["next"], {"what": "tournament", "label": "Tournament #8", "detail": ""})
        self.assertEqual({k: plan["winners"][k] for k in ("title", "champion", "runnerUp", "third")},
                         {"title": "Champion of Tournament #7", "champion": "C", "runnerUp": "A", "third": "B"})

    def test_paused_is_reported(self):
        self.assertEqual(self.plan(rr_state(paused="DeepSeek: quota"))["paused"], "DeepSeek: quota")


if __name__ == "__main__":
    unittest.main()
