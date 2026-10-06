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
        text, image = seen["payload"]["messages"][1]["content"]
        self.assertIn("Time budget for this move: about 14 seconds", text["text"])
        self.assertIn("8 r n b q k b n r 8", text["text"], "FEN diagram with coordinates")
        self.assertTrue(image["image_url"]["url"].startswith("data:image/png;base64,"), "board image every turn")
        self.assertAlmostEqual(seen["cutoff"], sp.THINK_SHARE * 1.5 * 600 / 44, places=3)
        self.assertIn("/zen/go/", seen["url"])

    def test_at_the_cap_the_model_answers_from_its_own_full_thinking_at_the_same_effort(self):
        import chess
        import os

        os.environ.setdefault("OPENROUTER_API_KEY", "test-key")
        client = sp.SubscriptionChessClient("openrouter-chat", lambda _m: None)
        calls = []
        thoughts = "Candidates: Nf3 and e4. " * 50

        def fake_stream(url, payload, headers, timeout, cutoff):
            calls.append((payload, cutoff))
            if len(calls) == 1:
                return {"content": "", "reasoning": thoughts, "usage": {}, "cut": True}
            return {"content": '{"move": "Nf3", "comment": "from my analysis"}', "reasoning": "", "usage": {}, "cut": False}

        client.http_stream = fake_stream
        move, comment = client.choose_move(chess.Board(), {"wtime": 600000, "btime": 600000, "winc": 10000}, [])
        self.assertEqual(move, "g1f3")
        self.assertEqual(len(calls), 2)
        follow, limit = calls[1]
        self.assertEqual(follow["reasoning"], {"effort": "high"}, "same effort as the thinking")
        self.assertEqual(follow["messages"][-2]["role"], "assistant")
        self.assertIn(thoughts, follow["messages"][-2]["content"], "all of its own thinking comes back")
        self.assertIn("time for this move is up", follow["messages"][-1]["content"])
        self.assertAlmostEqual(limit, sp.SAME_SHARE * sp.arbiter_cutoff_seconds(chess.Board(), 600000, 10000))
        self.assertEqual(client.last_report["hurried"], 1)
        self.assertEqual(client.last_report["tries"], 1)
        self.assertEqual(client.last_report["illegal"], [])
        self.assertIn("your thinking is stopped", calls[0][0]["messages"][1]["content"][0]["text"])

    def test_lowest_effort_only_when_the_same_effort_answer_brings_no_move(self):
        import chess
        import os

        os.environ.setdefault("OPENROUTER_API_KEY", "test-key")
        client = sp.SubscriptionChessClient("openrouter-chat", lambda _m: None)
        calls = []

        def fake_stream(url, payload, headers, timeout, cutoff):
            calls.append(payload)
            if len(calls) < 3:
                return {"content": "", "reasoning": "still weighing d4", "usage": {}, "cut": True}
            return {"content": '{"move": "d4"}', "reasoning": "", "usage": {}, "cut": False}

        client.http_stream = fake_stream
        move, _ = client.choose_move(chess.Board(), {"wtime": 600000, "btime": 600000}, [])
        self.assertEqual(move, "d2d4")
        self.assertEqual([c["reasoning"]["effort"] for c in calls], ["high", "high", "low"])

    def test_running_out_of_output_space_also_returns_the_thinking(self):
        import chess
        import os

        os.environ.setdefault("OPENCODE_GO_API_KEY", "test-key")
        client = sp.SubscriptionChessClient("opencode-go", lambda _m: None)
        client.model = "deepseek-v4.1-flash"
        calls = []

        def fake_stream(url, payload, headers, timeout, cutoff):
            calls.append(payload)
            if len(calls) == 1:
                return {"content": "", "reasoning": "x" * 200_000, "usage": {}, "cut": False, "finish": "length"}
            return {"content": '{"move": "e4"}', "usage": {}, "cut": False}

        client.http_stream = fake_stream
        move, _ = client.choose_move(chess.Board(), {"wtime": 600000, "btime": 600000}, [])
        self.assertEqual(move, "e2e4")
        self.assertEqual(calls[1]["reasoning_effort"], "high")
        returned = calls[1]["messages"][-2]["content"]
        self.assertIn("middle of the thinking omitted", returned)
        self.assertLess(len(returned), 130_000)

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
        self.assertNotIn("fp4", payload["provider"]["quantizations"])

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
        self.assertIsNone(sp.latest_note("BEST SO FAR: Nf6", board), "an illegal note is not played")
        self.assertEqual(sp.latest_note("BEST SO FAR: e4 ... write BEST SO FAR lines", board), chess.Move.from_uci("e2e4"))
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
        self.assertAlmostEqual(sp.arbiter_cutoff_seconds(board, 600000), 1.5 * 600 / 44)
        self.assertAlmostEqual(sp.arbiter_cutoff_seconds(board, 600000, 10000), 1.5 * 600 / 44 + 9)
        # Cut on every move and still sustainable: near an empty clock the cap stays under the increment.
        self.assertLess(sp.arbiter_cutoff_seconds(board, 20000, 10000), 10)
        self.assertEqual(sp.arbiter_cutoff_seconds(board, 3_600_000), 60.0)
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


class KnockoutTest(unittest.TestCase):
    def test_round_robin_then_semis_armageddon_final_and_a_champion(self):
        import tempfile

        names = [f"P{i}" for i in range(10)]
        state = play_out(new_state(names, 9), random.Random(3))
        pairs = {swiss.pair_key(p["white"], p["black"]) for r in state["rounds"] for p in r["pairings"]}
        self.assertEqual(len(pairs), 45, "nine rounds = full round robin")
        state["config"].update(format="round-robin+knockout", knockoutSize=4)
        state.update(id="t", title="t")
        seeds = [r["name"] for r in swiss.compute_standings(state)[:4]]
        # sf1 draws then the Armageddon is drawn too (Black wins); sf2 decisive; final decisive; third: draw, then White wins.
        script = {"Semifinal 1 - Game 1": "1/2-1/2", "Semifinal 1 - Armageddon decider": "1/2-1/2",
                  "Semifinal 2 - Game 1": "0-1", "Final": "1-0", "Third place": "1/2-1/2",
                  "Third place - Armageddon decider": "1-0"}
        starts = {}

        def fake_play(game_id, white, black, cfg, ts, live_pgn, log, replace_engine, start_ms=None):
            game = ts.state["games"][game_id]
            starts[game["label"]] = start_ms
            game.update(result=script[game["label"]], status="finished", end_kind="board")

        tmp = Path(tempfile.mkdtemp())
        old = swiss.play_game, swiss.write_archive
        swiss.play_game, swiss.write_archive = fake_play, (lambda _s: None)
        try:
            ts = swiss.TournamentState(state, tmp / "t-tournament.json")
            ok = swiss.run_knockouts(state, ts, dict(swiss.DEFAULTS, **state["config"]), lambda n: n, lambda e: None,
                                     lambda _m: None)
        finally:
            swiss.play_game, swiss.write_archive = old
        self.assertTrue(ok)
        ko = state["knockout"]
        sf1, sf2 = ko["matches"][:2]
        self.assertEqual((sf1["a"], sf1["b"], sf2["a"], sf2["b"]), (seeds[0], seeds[3], seeds[1], seeds[2]))
        self.assertEqual(sf1["winner"], seeds[0], "Armageddon colours swapped: seed 1 had Black and draw odds")
        self.assertEqual(sf1["decided_by"], "armageddon")
        self.assertEqual(sf2["winner"], seeds[2])
        self.assertEqual(ko["champion"], seeds[0], "the final is 1-0 and seed 1 (a) has White")
        self.assertEqual(ko["runner_up"], seeds[2])
        self.assertIsNotNone(ko["third"])
        self.assertEqual(starts["Semifinal 1 - Armageddon decider"], {"white": 600_000, "black": 450_000})
        self.assertIsNone(starts["Final"])
        self.assertEqual(state["stage"], "finished")
        table = swiss.compute_standings(state)
        self.assertAlmostEqual(sum(r["points"] for r in table), 45, msg="knockout games stay out of the table")


class AnswerStepTest(unittest.TestCase):
    def test_the_lowest_effort_answer_waits_on_the_clock_not_a_share(self):
        import chess
        import os

        os.environ.setdefault("OPENROUTER_API_KEY", "test-key")
        client = sp.SubscriptionChessClient("openrouter-chat", lambda _m: None)
        calls = []

        def fake_stream(url, payload, headers, timeout, cutoff):
            calls.append((timeout, cutoff))
            if len(calls) < 3:
                return {"content": "", "reasoning": "thinking", "usage": {}, "cut": True}
            return {"content": '{"move": "e4"}', "usage": {}, "cut": False}

        client.http_stream = fake_stream
        move, _ = client.choose_move(chess.Board(), {"wtime": 300000, "btime": 300000, "winc": 10000}, [])
        self.assertEqual(move, "e2e4")
        timeout, cutoff = calls[2]
        self.assertIsNone(cutoff, "no share cut on the last answer step")
        self.assertGreater(timeout, 200, "bounded by the remaining clock")

    def test_a_cut_off_reply_still_states_its_move(self):
        import chess

        move, _comment, raw = sp.parse_reply('{"move": "e4", "comment": "I take the centre and keep the bish', chess.Board())
        self.assertEqual((move.uci(), raw), ("e2e4", "e4"))


class BoardViewTest(unittest.TestCase):
    def test_diagram_has_coordinates_and_matches_the_fen(self):
        import chess

        board = chess.Board()
        board.push_san("e4")
        rows = sp.board_diagram(board).splitlines()
        self.assertEqual(rows[0], "  a b c d e f g h")
        self.assertEqual(rows[5], "4 . . . . P . . . 4")
        self.assertEqual(rows[1], "8 r n b q k b n r 8")

    def test_board_image_is_a_png(self):
        import chess

        png = sp.board_png(chess.Board())
        self.assertTrue(png.startswith(b"\x89PNG"))
        self.assertGreater(len(png), 5000)


class StockfishPlayerTest(unittest.TestCase):
    def test_fixed_depth_go_and_comment(self):
        import play_llm_series as series

        engine = series.LlmEngine.__new__(series.LlmEngine)
        engine.is_uci, engine.player = True, {"depth": 4}
        self.assertEqual(engine.go_command(600000, 600000, 10000), "go depth 4")
        engine.is_uci = False
        self.assertEqual(engine.go_command(1, 2, 3), "go wtime 1 btime 2 winc 3 binc 3")
        lines = ["info depth 4 seldepth 6 multipv 1 score cp -35 nodes 900 pv e7e5 g1f3", "bestmove e7e5"]
        self.assertIn("-0.35", swiss.uci_comment(lines, {"depth": 4}))


if __name__ == "__main__":
    unittest.main()
