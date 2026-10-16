"""Owner 2026-10-16: 1 hour clocks, no move cap, reasoning kept across moves, visible Claude thinking,
output-token limits, and compaction of a long game by a helper model on OpenCode Go."""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import chess

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "engines" / "llm-chess-engine"))

import play_llm_series as series  # noqa: E402
import play_llm_swiss as swiss  # noqa: E402
import subscription_providers as sp  # noqa: E402

GO = {"wtime": 3_600_000, "btime": 3_600_000, "winc": 10_000, "binc": 10_000}
CONTEXT = {"memory": "# Memory\n- Count every recapture before a queen move.\n",
           "header": "Tournament #43 (t43), Round 1, board 1.\nYou play White against Rival."}


def board_after(moves):
    board = chess.Board()
    for uci in moves:
        board.push_uci(uci)
    return board


def http_client(replies, reasoning=None, usage_tokens=None):
    os.environ.setdefault("OPENCODE_GO_API_KEY", "test-key")
    client = sp.SubscriptionChessClient("opencode-go", lambda _m: None)
    client.model, client.board_image, client.conversation = "deepseek-v4.1-flash", False, True
    client.context = dict(CONTEXT)
    client.move_cap, client.keep_reasoning = False, True
    calls = []
    replies, reasoning = iter(replies), iter(reasoning or [])
    usage_tokens = iter(usage_tokens or [])

    def stream(url, payload, headers, timeout, cutoff):
        calls.append({"payload": json.loads(json.dumps(payload)), "timeout": timeout, "cutoff": cutoff})
        reply = next(replies)
        if isinstance(reply, dict):
            return reply
        return {"content": reply, "reasoning": next(reasoning, ""), "cut": False, "finish": "stop",
                "usage": {"prompt_tokens": next(usage_tokens, 1000), "completion_tokens": 20}}

    client.http_stream = stream
    return client, calls


def claude_line(thinking: str) -> str:
    return json.dumps({"type": "assistant", "message": {"content": [
        {"type": "thinking", "thinking": thinking, "signature": "sig"}]}})


def claude_result(reply: str, read: int = 0, write: int = 400) -> str:
    usage = {"input_tokens": 3, "cache_creation_input_tokens": write, "cache_read_input_tokens": read, "output_tokens": 30}
    return json.dumps({"type": "result", "result": reply, "duration_api_ms": 1000, "usage": usage})


class NoMoveCapTest(unittest.TestCase):
    def test_no_cap_streams_without_a_cutoff_and_waits_for_the_clock(self):
        client, calls = http_client(['{"move": "Nf3"}'])
        move, _ = client.choose_move(board_after(["e2e4", "e7e5"]), GO, ["e2e4", "e7e5"])
        self.assertEqual(move, "g1f3")
        self.assertIsNone(calls[0]["cutoff"], "no move cap: the stream is never cut")
        self.assertGreaterEqual(calls[0]["timeout"], 3600, "one reply may use the whole clock")
        user = calls[0]["payload"]["messages"][-1]["content"]
        self.assertNotIn("your thinking is stopped", user)
        self.assertIn("Time budget for this move", user, "the advisory budget stays")

    def test_the_cap_is_still_there_when_switched_on(self):
        client, calls = http_client(['{"move": "Nf3"}'])
        client.move_cap = True
        client.choose_move(board_after(["e2e4", "e7e5"]), GO, ["e2e4", "e7e5"])
        self.assertIsNotNone(calls[0]["cutoff"])

    def test_attempt_timeout_follows_the_clock_without_a_cap(self):
        client = sp.SubscriptionChessClient("codex", lambda _m: None)
        client.timeout_seconds = 900
        client.move_cap = False
        self.assertEqual(client._attempt_timeout(3_000_000), 3005)
        client.move_cap = True
        self.assertEqual(client._attempt_timeout(3_000_000), 900)

    def test_cli_is_never_cut_without_a_cap(self):
        client = sp.SubscriptionChessClient("codex", lambda _m: None)
        client.board_image, client.conversation, client.context, client.move_cap = False, True, dict(CONTEXT), False
        seen = []

        def runner(argv, prompt, timeout, workdir):
            seen.append((client._cli_cut_s, timeout))
            Path(argv[argv.index("-o") + 1]).write_text('{"move": "e5"}', encoding="utf-8")
            return ('{"type":"thread.started","thread_id":"t-1"}\n{"type":"turn.started"}\n'
                    '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}\n')

        client.runner = runner
        client.choose_move(board_after(["e2e4"]), GO, ["e2e4"])
        self.assertEqual(seen[0][0], None)
        self.assertGreater(seen[0][1], 3000)


class ClaudeKeepsItsReasoningTest(unittest.TestCase):
    def test_one_resumed_session_with_visible_thinking(self):
        client = sp.SubscriptionChessClient("claude", lambda _m: None)
        client.board_image, client.conversation, client.context = False, True, dict(CONTEXT)
        client.move_cap, client.keep_reasoning = False, True
        with tempfile.TemporaryDirectory() as tmp:
            calls = []

            def runner(argv, prompt, timeout, workdir):
                calls.append((list(argv), prompt))
                reply = '{"move": "e5"}' if len(calls) == 1 else '{"move": "Nc6"}'
                lines = [claude_line(f"thinking about move {len(calls)}"), claude_result(reply, read=700 * (len(calls) - 1))]
                for line in lines:  # what _run_cli's pump does with each streamed line
                    visible = sp.cli_visible_thinking(line)
                    if visible:
                        client._cli_thoughts.append(visible)
                        client._think(visible + "\n")
                return "\n".join(lines) + "\n"

            client.runner = runner
            client.thinking_file = Path(tmp) / "ply2.thinking.txt"
            client.choose_move(board_after(["e2e4"]), GO, ["e2e4"])
            client.thinking_file = Path(tmp) / "ply4.thinking.txt"
            client.choose_move(board_after(["e2e4", "e7e5", "g1f3"]), GO, ["e2e4", "e7e5", "g1f3"])
            text2 = (Path(tmp) / "ply4.thinking.txt").read_text(encoding="utf-8")
        (argv1, prompt1), (argv2, prompt2) = calls
        self.assertEqual(argv1[argv1.index("--thinking-display") + 1], "summarized")
        self.assertIn("--session-id", argv1)
        self.assertNotIn("--no-session-persistence", argv1)
        self.assertNotIn("--input-format", argv1, "a real session: the turn goes in as plain text")
        self.assertEqual(argv2[argv2.index("--resume") + 1], argv1[argv1.index("--session-id") + 1])
        self.assertTrue(prompt1.startswith(sp.RULES_TEXT))
        self.assertTrue(prompt2.startswith("MOVES SINCE YOUR LAST TURN\n1... e5 2. Nf3"), prompt2[:60])
        self.assertIn("thinking about move 2", text2, "Claude's thinking reaches the thinking file")

    def test_without_keep_reasoning_claude_keeps_the_old_flat_request(self):
        client = sp.SubscriptionChessClient("claude", lambda _m: None)
        client.board_image, client.conversation, client.context = False, True, dict(CONTEXT)
        seen = []

        def runner(argv, prompt, timeout, workdir):
            seen.append(argv)
            return claude_result('{"move": "e5"}') + "\n"

        client.runner = runner
        client.choose_move(board_after(["e2e4"]), GO, ["e2e4"])
        self.assertIn("--no-session-persistence", seen[0])
        self.assertIn("--input-format", seen[0])


class HttpCarriesItsThinkingTest(unittest.TestCase):
    def test_previous_thinking_comes_back_once_in_the_next_turn(self):
        client, calls = http_client(['{"move": "Nf3"}', '{"move": "Bb5"}', '{"move": "O-O"}'],
                                    reasoning=["I plan d4 and must watch the e5 pawn.", "", ""])
        h1 = ["e2e4", "e7e5"]
        client.choose_move(board_after(h1), GO, h1)
        h2 = h1 + ["g1f3", "b8c6"]
        client.choose_move(board_after(h2), GO, h2)
        second = calls[1]["payload"]["messages"][-1]["content"]
        self.assertTrue(second.startswith("YOUR OWN THINKING ON YOUR PREVIOUS MOVE"), second[:60])
        self.assertIn("I plan d4 and must watch the e5 pawn.", second)
        self.assertIn("MOVES SINCE YOUR LAST TURN\n2. Nf3 Nc6", second)
        # The earlier turns stay a byte-identical prefix (input cache).
        first = calls[0]["payload"]["messages"]
        self.assertEqual(json.dumps(calls[1]["payload"]["messages"][:2]), json.dumps(first))
        h3 = h2 + ["f1b5", "a7a6"]
        client.choose_move(board_after(h3), GO, h3)
        third = calls[2]["payload"]["messages"][-1]["content"]
        self.assertFalse(third.startswith("YOUR OWN THINKING"), "a move with no visible thinking carries nothing")

    def test_very_long_thinking_is_trimmed_to_its_newest_part(self):
        text = sp.carried_thoughts_section("A" * 10 + "B" * (sp.CARRY_THOUGHTS_MAX_CHARS + 50))
        self.assertIn("[... start omitted ...]", text)
        self.assertNotIn("A", text.split("\n", 2)[-1])


class OutputLimitTest(unittest.TestCase):
    def test_http_asks_for_the_configured_limit_and_answers_from_its_thoughts_without_a_cap(self):
        client, calls = http_client([
            {"content": "", "reasoning": "Long think... BEST SO FAR: Nf3", "cut": False, "finish": "length",
             "usage": {"prompt_tokens": 900, "completion_tokens": 32000}},
            {"content": '{"move": "Nc3"}', "reasoning": "short", "usage": {"prompt_tokens": 950, "completion_tokens": 9}},
        ])
        client.max_output_tokens = 32000
        move, _ = client.choose_move(board_after(["e2e4", "e7e5"]), GO, ["e2e4", "e7e5"])
        self.assertEqual(move, "b1c3", "the model's own answer after its thinking came back")
        self.assertEqual(calls[0]["payload"]["max_tokens"], 32000)
        self.assertIsNone(calls[1]["cutoff"], "no cap: the answer step is not squeezed into seconds")
        self.assertGreater(calls[1]["timeout"], 600)
        self.assertEqual(client.last_report.get("hurried"), 1)

    def test_claude_output_limit_answers_at_low_effort_from_its_thinking(self):
        client = sp.SubscriptionChessClient("claude", lambda _m: None)
        client.board_image, client.conversation, client.context = False, True, dict(CONTEXT)
        client.move_cap, client.keep_reasoning = False, True
        efforts = []

        def runner(argv, prompt, timeout, workdir):
            efforts.append(argv[argv.index("--effort") + 1])
            client._cli_thoughts.append("I keep counting the guards of e5")
            if len(efforts) == 1:
                raise sp.ProviderError("claude exited 1: \"API Error: Claude's response exceeded the 128000 output "
                                       "token maximum. To configure this behavior, set CLAUDE_CODE_MAX_OUTPUT_TOKENS\"")
            self.assertIn("ran out of output space", prompt)
            self.assertIn("I keep counting the guards of e5", prompt)
            return claude_result('{"move": "Nc6"}') + "\n"

        client.runner = runner
        move, _ = client.choose_move(board_after(["e2e4", "e7e5", "g1f3"]), GO, ["e2e4", "e7e5", "g1f3"])
        self.assertEqual(move, "b8c6")
        self.assertEqual(efforts, ["high", "low"])
        self.assertTrue(sp.output_limit_error("Incomplete response: max_output_tokens"))
        self.assertFalse(sp.output_limit_error("rate limit reached"))

    def test_claude_gets_the_configured_output_ceiling(self):
        client = sp.SubscriptionChessClient("claude", lambda _m: None)
        client.max_output_tokens = 128000
        os.environ["LLM_MAX_OUTPUT_TOKENS"] = "128000"
        captured = {}

        class FakeProc:
            returncode = 0
            stdin = type("In", (), {"write": lambda self, s: None, "close": lambda self: None})()
            stdout = iter([claude_result('{"move": "e5"}') + "\n"])

            def poll(self):
                return 0

        def fake_popen(argv, **kwargs):
            captured.update(kwargs["env"])
            return FakeProc()

        real = sp.subprocess.Popen
        sp.subprocess.Popen = fake_popen
        try:
            client._run_cli(["claude"], "x", 30, None)
        finally:
            sp.subprocess.Popen = real
            os.environ.pop("LLM_MAX_OUTPUT_TOKENS", None)
        self.assertEqual(captured.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS"), "128000")


class FakeCompactor:
    def __init__(self, text="You planned d4 and watched e5.", error=None, delay=0.0):
        self.text, self.error, self.delay = text, error, delay
        self.prompts = []
        self.last_report = {"usage": {"input": 5000, "output": 300}}

    def ask_text(self, system_prompt, prompt, timeout=600, effort=None):
        self.prompts.append((system_prompt, prompt))
        time.sleep(self.delay)
        if self.error:
            raise sp.ProviderError(self.error)
        return self.text


class CompactionTest(unittest.TestCase):
    def setUp(self):
        self.client, self.calls = http_client(['{"move": "Nf3", "note": "d4 next"}', '{"move": "Bb5"}', '{"move": "O-O"}'],
                                              reasoning=["My plan is d4; watch e5.", "pin the knight", ""],
                                              usage_tokens=[130_000, 1_500, 2_000])
        self.client.compact_at, self.client.compact_provider, self.client.compact_model = 120_000, "opencode-go", "glm-5.3-flash"
        self.compactor = FakeCompactor()
        self.client.compactor_factory = lambda: self.compactor

    def play(self, moves):
        return self.client.choose_move(board_after(moves), GO, moves)

    def test_a_long_session_is_summarised_and_the_game_goes_on_in_a_fresh_session(self):
        h1 = ["e2e4", "e7e5"]
        self.play(h1)
        self.assertIsNotNone(self.client._compact_thread, "past the threshold: compaction starts after the move")
        h2 = h1 + ["g1f3", "b8c6"]
        self.play(h2)
        system, prompt = self.compactor.prompts[0]
        self.assertEqual(system, sp.COMPACT_SYSTEM)
        self.assertIn("My plan is d4; watch e5.", prompt)
        self.assertIn("note to itself: d4 next", prompt)
        self.assertIn("1. e4 e5 2. Nf3", prompt)
        fresh = self.calls[1]["payload"]["messages"]
        self.assertEqual(len(fresh), 2, "a fresh session: system prompt and one full turn")
        turn = fresh[1]["content"]
        self.assertTrue(turn.startswith(sp.RULES_TEXT))
        self.assertIn("YOUR EARLIER THINKING IN THIS GAME (your own thoughts up to ply 3", turn)
        self.assertIn("You planned d4 and watched e5.", turn)
        self.assertLess(turn.index("YOUR EARLIER THINKING"), turn.index("POSITION ("), "the position stays last")
        self.assertEqual(self.client.last_report["compacted"]["plies"], 3)
        self.assertEqual(self.client.last_report["compacted"]["from_tokens"], 130_000)
        h3 = h2 + ["f1b5", "a7a6"]
        self.play(h3)
        self.assertTrue(self.calls[2]["payload"]["messages"][-1]["content"].startswith("YOUR OWN THINKING ON YOUR PREVIOUS MOVE"))
        self.assertEqual(len(self.compactor.prompts), 1, "under the threshold again: no second compaction")

    def test_a_failed_compaction_keeps_the_session_and_gives_up_after_three(self):
        self.compactor.error = "HTTP 500 upstream"
        h1 = ["e2e4", "e7e5"]
        self.play(h1)
        h2 = h1 + ["g1f3", "b8c6"]
        self.play(h2)
        self.assertEqual(len(self.calls[1]["payload"]["messages"]), 4, "session kept: first turn, reply, new turn")
        self.assertEqual(self.client._compact_failures, 1)
        self.client._compact_failures = sp.COMPACT_GIVE_UP_AFTER - 1
        self.client._context_tokens = 130_000
        self.client._maybe_start_compaction(h2 + ["f1b5"])
        self.client.http_stream = lambda url, payload, headers, timeout, cutoff: (
            self.calls.append({"payload": json.loads(json.dumps(payload)), "timeout": timeout, "cutoff": cutoff})
            or {"content": '{"move": "O-O"}', "reasoning": "", "usage": {"prompt_tokens": 3000, "completion_tokens": 5}})
        self.play(h2 + ["f1b5", "a7a6"])
        self.assertEqual(len(self.calls[-1]["payload"]["messages"]), 2, "gave up: a fresh session without a summary")
        self.assertNotIn("YOUR EARLIER THINKING", self.calls[-1]["payload"]["messages"][1]["content"])

    def test_waiting_for_a_slow_compaction_moves_the_runner_deadline(self):
        self.compactor.delay = 0.5
        waits = []
        self.client.on_infra_wait = lambda secs, why: waits.append((secs, why))
        h1 = ["e2e4", "e7e5"]
        self.play(h1)
        self.play(h1 + ["g1f3", "b8c6"])
        self.assertEqual(waits[0][0], sp.COMPACT_TIMEOUT_SECONDS)
        self.assertIn("compaction", waits[0][1])

    def test_compaction_is_off_without_a_threshold(self):
        self.client.compact_at = 0
        self.play(["e2e4", "e7e5"])
        self.assertIsNone(self.client._compact_thread)


class ConfigAndRunnerTest(unittest.TestCase):
    def test_engine_env_carries_the_new_settings(self):
        cfg = {"maxAttempts": 3, "attemptTimeoutSeconds": 900, "showLegalMoves": True, "conversation": True,
               "limitWait": True, "moveCap": False, "keepReasoning": True,
               "compaction": {"provider": "opencode-go", "model": "glm-5.3-flash", "atTokens": 120000, "effort": "high"}}
        player = {"provider": "claude", "model": "claude-sonnet-5-5", "effort": "high", "maxOutputTokens": 128000}
        env = series.engine_env(player, cfg)
        self.assertEqual(env["LLM_MOVE_CAP"], "false")
        self.assertEqual(env["LLM_KEEP_REASONING"], "true")
        self.assertEqual(env["LLM_MAX_OUTPUT_TOKENS"], "128000")
        self.assertEqual(env["LLM_COMPACT_AT_TOKENS"], "120000")
        self.assertEqual((env["LLM_COMPACT_PROVIDER"], env["LLM_COMPACT_MODEL"]), ("opencode-go", "glm-5.3-flash"))
        self.assertEqual(env["LLM_EFFORT"], "high")

    def test_vps_config_is_one_hour_no_cap_keep_reasoning_and_compaction(self):
        cfg = json.loads((ROOT / "configs" / "ai-chess-vps.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg["timeControlMs"], 3_600_000)
        self.assertIs(cfg["moveCap"], False)
        self.assertIs(cfg["keepReasoning"], True)
        self.assertEqual(cfg["compaction"]["provider"], "opencode-go")
        self.assertIn(cfg["compaction"]["model"], ("glm-5.3-flash", "deepseek-v4.1-flash"))
        players = {p["name"]: p for p in cfg["players"]}
        for name in ("Sonnet 5.5", "GPT-6.1 Sol", "DeepSeek V4.1 Flash", "GLM 5.3 Flash"):
            self.assertEqual(players[name]["effort"], "high", name)
            self.assertGreater(players[name].get("maxOutputTokens", 0), 0, name)
        self.assertEqual(players["DeepSeek V4.1 Flash"]["provider"], "opencode-go")
        self.assertEqual(players["GLM 5.3 Flash"]["provider"], "opencode-go")

    def test_runner_deadline_moves_out_on_an_infrastructure_wait(self):
        class Engine:
            name = "fake"

            def __init__(self, first):
                self.cond = threading.Condition()
                self.lines = list(first)

            def later(self, line, after):
                def push():
                    time.sleep(after)
                    with self.cond:
                        self.lines.append(line)
                        self.cond.notify_all()
                threading.Thread(target=push, daemon=True).start()

        slow = Engine([])
        slow.later("bestmove e2e4", 1.2)
        with self.assertRaises(TimeoutError):
            swiss.wait_bestmove(slow, 0.5, lambda _ms: None)
        waited = Engine(["info string infrawait 2 compaction"])
        waited.later("bestmove e2e4", 1.2)
        seen = swiss.wait_bestmove(waited, 0.5, lambda _ms: None)
        self.assertEqual(seen[-1], "bestmove e2e4", "the infrastructure wait moved the deadline out")
        self.assertEqual(series.parse_compacted(['info string compacted {"plies": 31, "from_tokens": 121000}']),
                         {"plies": 31, "from_tokens": 121000})


if __name__ == "__main__":
    unittest.main()
