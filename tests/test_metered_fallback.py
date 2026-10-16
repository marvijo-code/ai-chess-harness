"""Subscription first, metered second: the daily cap, the per-game route choice and the roster check (no network)."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "engines" / "llm-chess-engine"))

import ai_chess_forever as forever  # noqa: E402
import ai_chess_memory as mem  # noqa: E402

PLAYER = {"name": "DeepSeek V4.1 Flash", "memoryFolder": "deepseek", "provider": "alibaba", "model": "deepseek-v4.1-flash",
          "effort": "high", "route": "subscription",
          "fallback": {"provider": "openrouter-chat", "model": "deepseek/deepseek-v4.1-flash", "route": "metered"}}


def budget(spent=0.0, balance=50.0, daily=10.0, fail=False, limit_remaining=None):
    def fetch(path):
        if fail:
            raise OSError("no network")
        return ({"usage_daily": spent, "limit_remaining": limit_remaining} if path == "/key"
                else {"total_credits": 100.0, "total_usage": 100.0 - balance})
    return forever.MeteredBudget(daily, 0.5, fetch=fetch)


class BudgetTest(unittest.TestCase):
    def test_what_is_left_is_the_cap_minus_what_openrouter_says_was_spent_today(self):
        self.assertAlmostEqual(budget(spent=3.25).remaining(), 6.75)
        self.assertEqual(budget(spent=10.0).remaining(), 0.0)
        self.assertEqual(budget(spent=12.0).remaining(), 0.0)

    def test_the_account_balance_keeps_a_floor(self):
        self.assertAlmostEqual(budget(balance=2.0).remaining(), 1.5)
        self.assertEqual(budget(balance=0.4).remaining(), 0.0)

    def test_a_limit_on_the_key_counts_too(self):
        self.assertAlmostEqual(budget(limit_remaining=0.75).remaining(), 0.75)

    def test_no_answer_or_a_zero_cap_means_nothing_may_be_spent(self):
        self.assertEqual(budget(fail=True).remaining(), 0.0)
        self.assertIn("did not answer", budget(fail=True).note())
        self.assertEqual(budget(daily=0).remaining(), 0.0)


class RouteChoiceTest(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp())
        cfg = forever.load_forever_config(ROOT / "configs" / "ai-chess-vps.json")
        self.hooks = forever.ForeverHooks(cfg, mem.MemoryRepo(root / "memory"), None, root / "live")
        self.probed = []
        self.addCleanup(setattr, forever, "BUDGET", forever.BUDGET)

    def prober(self, answers):
        answers = list(answers)

        def probe(player, cfg, live_dir, timeout):
            self.probed.append(player["provider"])
            ok = answers.pop(0)
            return {"name": player["name"], "ok": ok, "kind": None if ok else "subscription limit", "reason": "" if ok else "HTTP 429"}
        return probe

    def test_the_subscription_plays_when_it_answers(self):
        forever.BUDGET = budget()
        self.hooks.prober = self.prober([True])
        self.assertIs(self.hooks.player_for_game(PLAYER), PLAYER)

    def test_the_metered_route_plays_one_game_while_the_subscription_is_down(self):
        forever.BUDGET = budget(spent=2.0)
        self.hooks.prober = self.prober([False, True])
        second = self.hooks.player_for_game(PLAYER)
        self.assertEqual((second["provider"], second["model"], second["metered"]),
                         ("openrouter-chat", "deepseek/deepseek-v4.1-flash", True))
        self.assertEqual((second["name"], second["memoryFolder"], second["effort"]), (PLAYER["name"], "deepseek", "high"))
        self.assertNotIn("fallback", second)
        self.assertEqual(self.probed, ["alibaba"], "the metered route is not probed: a probe costs money")
        # the next game asks again, and the subscription is back
        self.assertIs(self.hooks.player_for_game(PLAYER), PLAYER)

    def test_with_the_cap_used_up_the_game_waits_and_checks_again(self):
        forever.BUDGET = budget(spent=10.0)
        self.hooks.prober = self.prober([False, False, True])
        waits = []
        self.hooks.sleep = waits.append
        self.assertIs(self.hooks.player_for_game(PLAYER), PLAYER)
        self.assertEqual(len(waits), 2)

    def test_a_timeout_or_server_error_with_credits_left_never_goes_metered(self):
        forever.BUDGET = budget(spent=0.0)
        answers = [{"ok": False, "reason": "no answer within 120s"}, {"ok": False, "reason": "HTTP 502 bad gateway"},
                   {"ok": True}]

        def probe(player, cfg, live_dir, timeout):
            self.probed.append(player["provider"])
            return dict(answers.pop(0), name=player["name"])
        self.hooks.prober = probe
        waits = []
        self.hooks.sleep = waits.append
        self.assertIs(self.hooks.player_for_game(PLAYER), PLAYER)
        self.assertEqual(self.probed, ["alibaba"] * 3)
        self.assertEqual(waits, [60.0, 60.0])

    def test_opencode_go_asks_its_usage_page_whether_the_plan_is_empty(self):
        forever.BUDGET = budget(spent=0.0)
        glm = dict(PLAYER, name="GLM 5.3 Flash", provider="opencode-go", model="glm-5.3-flash",
                   fallback={"provider": "openrouter-chat", "model": "z-ai/glm-5.3-flash"})
        usage = {"rolling": {"status": "ok"}, "weekly": {"status": "ok"}, "monthly": {"status": "ok"}}
        self.addCleanup(setattr, forever, "opencode_go_usage", forever.opencode_go_usage)
        forever.opencode_go_usage = lambda timeout=15: usage
        # a 429 with every window "ok" is a short rate limit, not an empty plan: ask the plan again
        self.hooks.prober = self.prober([False, True])
        self.hooks.sleep = lambda s: None
        self.assertIs(self.hooks.player_for_game(glm), glm)
        # the monthly window is used up: OpenRouter plays this game
        usage["monthly"] = {"status": "rate-limited", "resetsAt": "2026-11-14T07:24:17.000Z"}
        self.hooks.prober = self.prober([False])
        self.assertEqual(self.hooks.player_for_game(glm)["provider"], "openrouter-chat")

    def test_without_a_budget_or_a_second_route_nothing_changes(self):
        forever.BUDGET = None
        self.hooks.prober = self.prober([])
        self.assertIs(self.hooks.player_for_game(PLAYER), PLAYER)
        forever.BUDGET = budget()
        plain = {k: v for k, v in PLAYER.items() if k != "fallback"}
        self.assertIs(self.hooks.player_for_game(plain), plain)
        self.assertEqual(self.probed, [])

    def test_the_notes_call_uses_the_route_the_game_was_played_on(self):
        game = {"white": PLAYER["name"], "black": "Stockfish 19",
                "route": {"white": {"provider": "openrouter-chat", "model": "deepseek/deepseek-v4.1-flash", "metered": True}}}
        self.assertEqual(self.hooks.route_player(PLAYER, game)["provider"], "openrouter-chat")
        game["route"]["white"] = {"provider": "alibaba", "model": "deepseek-v4.1-flash", "metered": False}
        self.assertIs(self.hooks.route_player(PLAYER, game), PLAYER)
        self.assertIs(self.hooks.route_player(PLAYER, {"white": PLAYER["name"]}), PLAYER)


class RosterFallbackTest(unittest.TestCase):
    CFG = {"forever": {"meteredReserveUsd": 1.0}}

    def probe(self, down):
        def prober(player, cfg, live_dir, timeout):
            ok = player["provider"] not in down
            return {"name": player["name"], "ok": ok, "route": f"{player['provider']} {player['model']}",
                    "reason": "" if ok else "HTTP 429", "kind": None if ok else "subscription limit"}
        return prober

    def test_a_player_joins_on_the_metered_route_when_the_cap_has_room_for_a_tournament(self):
        row = forever.probe_with_fallback(PLAYER, self.CFG, Path("."), 5, budget(spent=2.0), self.probe({"alibaba"}))
        self.assertTrue(row["ok"])
        self.assertEqual((row["route"], row["metered"], row["name"]),
                         ("openrouter-chat deepseek/deepseek-v4.1-flash", True, PLAYER["name"]))

    def test_it_is_benched_when_the_cap_has_no_room_or_both_routes_are_down(self):
        row = forever.probe_with_fallback(PLAYER, self.CFG, Path("."), 5, budget(spent=9.5), self.probe({"alibaba"}))
        self.assertFalse(row["ok"])
        self.assertIn("second route not used", row["metered"])
        row = forever.probe_with_fallback(PLAYER, self.CFG, Path("."), 5, budget(), self.probe({"alibaba", "openrouter-chat"}))
        self.assertFalse(row["ok"])
        self.assertIn("failed too", row["metered"])

    def test_a_failure_with_credits_left_is_benched_not_sent_to_the_metered_route(self):
        tried = []

        def prober(player, cfg, live_dir, timeout):
            tried.append(player["provider"])
            return {"name": player["name"], "ok": False, "reason": "no answer within 300s", "kind": "preflight failed"}
        row = forever.probe_with_fallback(PLAYER, self.CFG, Path("."), 5, budget(), prober)
        self.assertFalse(row["ok"])
        self.assertIn("still has credits", row["metered"])
        self.assertEqual(tried, ["alibaba"])

    def test_the_subscription_answer_is_kept_and_no_budget_means_no_second_try(self):
        row = forever.probe_with_fallback(PLAYER, self.CFG, Path("."), 5, budget(), self.probe(set()))
        self.assertEqual((row["ok"], row["route"]), (True, "alibaba deepseek-v4.1-flash"))
        row = forever.probe_with_fallback(PLAYER, self.CFG, Path("."), 5, None, self.probe({"alibaba"}))
        self.assertFalse(row["ok"])


if __name__ == "__main__":
    unittest.main()
