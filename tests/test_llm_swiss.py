import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engines" / "llm-chess-engine"))

import play_llm_swiss as swiss  # noqa: E402
import subscription_providers as sp  # noqa: E402

NAMES = ["Grok", "Opus", "GPT", "DeepSeek", "GLM"]


def new_state(names, rounds, seed=1):
    cfg = dict(swiss.DEFAULTS)
    cfg.update({"rounds": rounds, "players": [{"name": n} for n in names]})
    return {"config": cfg, "players": cfg["players"], "seed_order": list(names), "rounds": [], "games": {}}


def play_out(state, rng):
    for number in range(1, state["config"]["rounds"] + 1):
        pairings, bye = swiss.make_pairings(state, number)
        rnd = {"round": number, "bye": bye, "pairings": []}
        for board, (white, black) in enumerate(pairings, start=1):
            gid = f"r{number}b{board}"
            rnd["pairings"].append({"white": white, "black": black, "game_id": gid, "board": board})
            state["games"][gid] = {"result": rng.choice(["1-0", "0-1", "1/2-1/2"]), "end_kind": "board"}
        state["rounds"].append(rnd)
    return state


class SwissPairingTest(unittest.TestCase):
    def test_five_players_five_rounds_is_a_full_round_robin(self):
        for seed in range(25):
            state = play_out(new_state(NAMES, 5), random.Random(seed))
            pairs = [swiss.pair_key(p["white"], p["black"]) for r in state["rounds"] for p in r["pairings"]]
            self.assertEqual(len(pairs), 10)
            self.assertEqual(len(set(pairs)), 10, "no rematches")
            byes = [r["bye"] for r in state["rounds"]]
            self.assertEqual(sorted(byes), sorted(NAMES), "one bye each")
            standings = swiss.compute_standings(state)
            self.assertAlmostEqual(sum(r["points"] for r in standings), 10, msg="points come from games only")
            for row in standings:
                self.assertEqual(row["points"], row["wins"] + 0.5 * row["draws"])
            whites = {r["name"]: r["whites"] for r in standings}
            self.assertTrue(all(1 <= w <= 3 for w in whites.values()), whites)

    def test_rounds_are_capped_to_a_round_robin(self):
        self.assertEqual(swiss.max_rounds(5), 5)
        self.assertEqual(swiss.max_rounds(6), 5)

    def test_elo_is_zero_sum_and_moves_toward_the_winner(self):
        white, black = swiss.elo_update(1500, 1500, 1.0, 32)
        self.assertAlmostEqual(white, 1516)
        self.assertAlmostEqual(black, 1484)
        white, black = swiss.elo_update(1600, 1400, 0.5, 32)
        self.assertLess(white, 1600)
        self.assertAlmostEqual(white + black, 3000)

    def test_forfeits_and_flags_are_counted_as_losses(self):
        state = new_state(NAMES[:2], 1)
        state["rounds"] = [{"round": 1, "bye": None, "pairings": [{"white": "Grok", "black": "Opus", "game_id": "g"}]}]
        state["games"]["g"] = {"result": "0-1", "end_kind": "forfeit", "invalid_attempts": {"white": 3, "black": 1}}
        rows = {r["name"]: r for r in swiss.compute_standings(state)}
        self.assertEqual(rows["Grok"]["forfeits"], 1)
        self.assertEqual(rows["Grok"]["losses"], 1)
        self.assertEqual(rows["Opus"]["points"], 1)
        self.assertEqual(rows["Grok"]["invalid_attempts"], 3)


class ThinkTimeTest(unittest.TestCase):
    def test_codex_turn_events_bracket_think_time(self):
        lines = [(10.0, '{"type":"thread.started"}\n'), (12.0, '{"type":"turn.started"}\n'),
                 (20.5, '{"type":"item.completed","item":{"type":"agent_message"}}\n'), (21.0, '{"type":"turn.completed"}\n')]
        self.assertEqual(sp.cli_think_ms("codex", "", lines), 9000)

    def test_claude_reports_api_time(self):
        self.assertEqual(sp.cli_think_ms("claude", '{"result":"x","duration_api_ms":4321}', []), 4321)
        self.assertIsNone(sp.cli_think_ms("claude", "no json", []))

    def test_codex_tool_use_is_detected(self):
        out = '{"type":"item.completed","item":{"type":"command_execution","command":"python engine.py"}}\n'
        self.assertEqual(sp.codex_tool_items(out), {"command_execution"})
        self.assertEqual(sp.codex_tool_items('{"type":"item.completed","item":{"type":"agent_message"}}'), set())

    def test_http_route_uses_the_shared_prompt_and_glm_thinking_switch(self):
        import chess

        client = sp.SubscriptionChessClient("opencode-go", lambda _m: None)
        client.model = "glm-5.3"
        seen = {}

        def fake_stream(url, payload, headers, timeout, cutoff):
            seen.update(payload=payload, url=url, cutoff=cutoff)
            return {"content": '{"move": "e4", "comment": "center"}', "reasoning": "", "usage": {}, "cut": False}

        client.http_stream = fake_stream
        import os

        os.environ.setdefault("OPENCODE_GO_API_KEY", "test-key")
        move, comment = client.choose_move(chess.Board(), {"wtime": 600000, "btime": 600000}, [])
        self.assertEqual(move, "e2e4")
        self.assertEqual(seen["payload"]["thinking"], {"type": "enabled"})
        self.assertTrue(seen["payload"]["stream"])
        self.assertEqual(seen["payload"]["messages"][0]["content"], sp.SYSTEM_PROMPT)
        self.assertIn("Time budget for this move: about 14 seconds", seen["payload"]["messages"][1]["content"])
        self.assertAlmostEqual(seen["cutoff"], 2 * 600 / 44, places=3)
        self.assertIn("/zen/go/", seen["url"])

    def test_referee_plays_the_latest_best_so_far_note_at_the_cap(self):
        import chess
        import os

        os.environ.setdefault("OPENCODE_GO_API_KEY", "test-key")
        client = sp.SubscriptionChessClient("opencode-go", lambda _m: None)
        client.model = "glm-5.3"
        calls = []

        def fake_stream(url, payload, headers, timeout, cutoff):
            calls.append(payload)
            return {"content": "", "reasoning": "BEST SO FAR: e4 ... hmm BEST SO FAR: **Nf3** because", "usage": {}, "cut": True}

        client.http_stream = fake_stream
        move, comment = client.choose_move(chess.Board(), {"wtime": 600000, "btime": 600000, "winc": 10000}, [])
        self.assertEqual(move, "g1f3")
        self.assertEqual(len(calls), 1, "no second, lower-effort request")
        self.assertEqual(client.last_report["hurried"], 1)
        self.assertEqual(client.last_report["tries"], 1)
        self.assertEqual(client.last_report["illegal"], [])
        self.assertIn("BEST SO FAR", comment)
        self.assertIn("referee plays your latest BEST SO FAR", calls[0]["messages"][1]["content"])

    def test_every_route_keeps_high_effort_and_openrouter_is_cache_sticky(self):
        import chess
        import os

        os.environ.setdefault("OPENROUTER_API_KEY", "test-key")
        client = sp.SubscriptionChessClient("openrouter-chat", lambda _m: None)
        seen = {}

        def fake_stream(url, payload, headers, timeout, cutoff):
            seen.update(payload=payload, headers=headers)
            return {"content": '{"move": "e4"}', "reasoning": "", "usage": {}, "cut": False}

        client.http_stream = fake_stream
        client.choose_move(chess.Board(), {"wtime": 600000, "btime": 600000}, [])
        payload = seen["payload"]
        self.assertEqual(payload["reasoning"], {"effort": "high"})
        self.assertEqual(payload["session_id"], client.session_id)
        self.assertEqual(seen["headers"]["x-session-id"], client.session_id)
        self.assertNotIn("order", payload.get("provider", {}), "a provider order turns sticky cache routing off")

    def test_prompt_puts_the_append_only_part_first_for_input_caching(self):
        import chess

        board = chess.Board()
        first = sp.build_prompt(board, {}, [], [])
        board.push_san("e4"); board.push_san("c5")
        later = sp.build_prompt(board, {}, ["e2e4", "c7c5"], [])
        self.assertTrue(later.startswith("You are playing White.\nMoves so far: 1. e4 c5"))
        self.assertLess(later.index("Moves so far"), later.index("FEN:"))
        self.assertTrue(first.startswith("You are playing White.\nMoves so far:"))

    def test_no_note_means_no_cut_and_the_clock_decides(self):
        import chess

        board = chess.Board()
        self.assertIsNone(sp.latest_note("thinking about e4 and d4", board))
        self.assertIsNone(sp.latest_note("BEST SO FAR: Nf6", board), "an illegal latest note is not played")
        self.assertEqual(sp.latest_note("BEST SO FAR: 1. d4", board), chess.Move.from_uci("d2d4"))
        client = sp.SubscriptionChessClient("opencode-go", lambda _m: None)
        move, comment = client.choose_move(board, {"wtime": 0, "btime": 600000}, [])
        self.assertEqual(move, "0000")

    def test_running_out_of_clock_is_a_flag_not_an_invalid_reply(self):
        import chess
        import os
        import subprocess

        os.environ.setdefault("OPENCODE_GO_API_KEY", "test-key")
        client = sp.SubscriptionChessClient("opencode-go", lambda _m: None)
        client.model = "glm-5.3"

        def slow_stream(url, payload, headers, timeout, cutoff):
            raise subprocess.TimeoutExpired(url, timeout)

        client.http_stream = slow_stream
        client._add_think = lambda started: client.last_report.__setitem__("think_ms", 30000)
        move, comment = client.choose_move(chess.Board(), {"wtime": 20000, "btime": 600000}, [])
        self.assertEqual(move, "0000")
        self.assertIn("ran out of clock", comment)
        self.assertEqual(client.last_report["tries"], 2, "stops at once when the clock is gone")

    def test_cap_grows_with_the_budget_and_never_eats_the_clock(self):
        import chess

        board = chess.Board()
        self.assertAlmostEqual(sp.arbiter_cutoff_seconds(board, 600000), 2 * 600 / 44)
        self.assertAlmostEqual(sp.arbiter_cutoff_seconds(board, 600000, 10000), 2 * (600 / 44 + 8))
        self.assertEqual(sp.arbiter_cutoff_seconds(board, 3_600_000), 90.0)
        self.assertAlmostEqual(sp.arbiter_cutoff_seconds(board, 30000), 7.5)
    def test_a_plan_limit_voids_instead_of_forfeiting(self):
        import chess
        import os

        os.environ.setdefault("OPENCODE_GO_API_KEY", "test-key")
        client = sp.SubscriptionChessClient("opencode-go", lambda _m: None)

        def limited(url, payload, headers, timeout, cutoff):
            raise sp.CliCrash('HTTP 429: {"error":{"type":"GoUsageLimitError","message":"Go usage limit exceeded"}}')

        client.http_stream = limited
        move, comment = client.choose_move(chess.Board(), {"wtime": 600000, "btime": 600000}, [])
        self.assertEqual(move, "0000")
        self.assertTrue(comment.startswith("provider unavailable"))
        self.assertEqual(client.last_report["illegal"], [], "not an invalid reply")
        self.assertFalse(sp.provider_unavailable("HTTP 429: rate limited, retry in 2s"))

    def test_an_overrun_adds_a_decide_faster_note_to_the_next_prompt(self):
        self.assertIsNone(sp.overrun_note(20000, 40))
        self.assertIsNone(sp.overrun_note(90000, None), "CLI routes have no cap")
        note = sp.overrun_note(95000, 45)
        self.assertIn("95 seconds", note)
        import chess

        self.assertIn("Decide faster", sp.build_prompt(chess.Board(), {}, [], [], nudge=note))


if __name__ == "__main__":
    unittest.main()
