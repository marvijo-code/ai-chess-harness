import importlib.util
import io
import json
import os
import re
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path

import chess


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def fake_engine_script() -> str:
    return (
        "import os, sys\n"
        "import chess\n"
        "board = chess.Board()\n"
        "for raw in sys.stdin:\n"
        "    line = raw.strip()\n"
        "    if line == 'uci':\n"
        "        print('id name fake-uci', flush=True)\n"
        "        print('uciok', flush=True)\n"
        "    elif line == 'isready':\n"
        "        print('readyok', flush=True)\n"
        "    elif line.startswith('ucinewgame'):\n"
        "        board = chess.Board()\n"
        "    elif line.startswith('position fen '):\n"
        "        board = chess.Board(line[len('position fen '):])\n"
        "    elif line.startswith('go'):\n"
        "        if os.environ.get('FAKE_ENGINE_FORFEIT') == '1':\n"
        "            print('bestmove 0000', flush=True)\n"
        "        else:\n"
        "            move = next(iter(board.legal_moves))\n"
        "            print('bestmove ' + move.uci(), flush=True)\n"
        "    elif line == 'quit':\n"
        "        break\n"
    )


class LlmAttemptTests(unittest.TestCase):
    def setUp(self):
        self.llm = load_module(
            "llm_chess_uci_attempt_test",
            ROOT / "engines" / "llm-chess-engine" / "llm_chess_uci.py",
        )

    def test_openrouter_forfeits_only_after_configured_attempts(self):
        client = self.llm.OpenRouterChessClient()
        client.max_attempts = 3
        calls = {"count": 0}

        def always_fail(api_key, payload, timeout):
            calls["count"] += 1
            raise RuntimeError("network down")

        client._post = always_fail
        move, reason = client.choose_move(chess.Board(), {}, [])

        self.assertEqual(move, "0000")
        self.assertEqual(calls["count"], 3)
        self.assertIn("after 3 attempts", reason)
        self.assertEqual(client.invalid_model_moves, 3)

    def test_openrouter_succeeds_on_third_attempt_and_resets_streak(self):
        client = self.llm.OpenRouterChessClient()
        client.max_attempts = 3
        client.invalid_model_moves = 2
        calls = {"count": 0}

        def fail_twice_then_legal(api_key, payload, timeout):
            calls["count"] += 1
            if calls["count"] < 3:
                raise RuntimeError("temporary failure")
            return {
                "choices": [
                    {"message": {"content": json.dumps({"uci": "e2e4", "comment": "ok"})}}
                ]
            }

        client._post = fail_twice_then_legal
        move, comment = client.choose_move(chess.Board(), {}, [])

        self.assertEqual(move, "e2e4")
        self.assertEqual(comment, "ok")
        self.assertEqual(calls["count"], 3)
        self.assertEqual(client.invalid_model_moves, 0)

    def test_openrouter_attempt_limit_is_configurable_by_option_and_env(self):
        client = self.llm.OpenRouterChessClient()
        client.set_option("MaxAttempts", "5")
        self.assertEqual(client.max_attempts, 5)

        client.set_option("MaxAttempts", "99")
        self.assertEqual(client.max_attempts, self.llm.MAX_ATTEMPTS_CEILING)

        self.assertEqual(self.llm.clamp_max_attempts("0"), 1)
        self.assertEqual(self.llm.clamp_max_attempts("not-a-number"), self.llm.DEFAULT_MAX_ATTEMPTS)

        os.environ["OPENROUTER_MAX_ATTEMPTS"] = "4"
        try:
            self.assertEqual(self.llm.OpenRouterChessClient().max_attempts, 4)
        finally:
            del os.environ["OPENROUTER_MAX_ATTEMPTS"]

    def test_openrouter_max_tokens_is_configurable(self):
        client = self.llm.OpenRouterChessClient()
        self.assertEqual(client.max_tokens, self.llm.DEFAULT_MAX_TOKENS)
        client.set_option("MaxTokens", "2500")
        self.assertEqual(client.max_tokens, 2500)
        self.assertEqual(self.llm.clamp_max_tokens("1"), 64)

    def test_openrouter_payload_controls_reasoning_and_provider(self):
        client = self.llm.OpenRouterChessClient()
        payload = client._build_payload(chess.Board(), {}, [], ["e2e4", "d2d4"])
        self.assertEqual(payload["reasoning"], {"effort": client.reasoning_effort})
        self.assertEqual(payload["provider"]["sort"], client.provider_sort)
        self.assertTrue(payload["provider"]["allow_fallbacks"])
        self.assertFalse(client.use_schema)
        self.assertNotIn("response_format", payload)

    def test_openrouter_json_schema_is_opt_in(self):
        client = self.llm.OpenRouterChessClient()
        client.set_option("UseJsonSchema", "true")
        payload = client._build_payload(chess.Board(), {}, [], ["e2e4"])
        self.assertIn("response_format", payload)

    def test_openrouter_empty_reasoning_response_bumps_tokens_then_succeeds(self):
        client = self.llm.OpenRouterChessClient()
        client.max_attempts = 2
        client.max_tokens = 1500
        responses = [
            {"choices": [{"message": {"content": None, "reasoning": "long"}, "finish_reason": "length"}]},
            {"choices": [{"message": {"content": json.dumps({"uci": "e2e4", "comment": ""})}, "finish_reason": "stop"}]},
        ]

        def fake_post(api_key, payload, timeout):
            return responses.pop(0)

        client._post = fake_post
        move, _ = client.choose_move(chess.Board(), {}, [])
        self.assertEqual(move, "e2e4")
        self.assertEqual(client.max_tokens, 3000)

    def test_openrouter_400_on_reasoning_downgrades_without_spending_attempt(self):
        client = self.llm.OpenRouterChessClient()
        client.max_attempts = 2
        calls = {"count": 0}

        def make_http_error(code, body):
            return urllib.error.HTTPError(
                "https://openrouter.ai/api/v1/chat/completions",
                code,
                "error",
                {"Content-Type": "application/json"},
                io.BytesIO(body.encode()),
            )

        def fake_post(api_key, payload, timeout):
            calls["count"] += 1
            if "reasoning" in payload:
                raise make_http_error(400, '{"error":{"message":"Reasoning is mandatory for this endpoint"}}')
            return {"choices": [{"message": {"content": json.dumps({"uci": "e2e4", "comment": ""})}, "finish_reason": "stop"}]}

        client._post = fake_post
        move, _ = client.choose_move(chess.Board(), {}, [])
        self.assertEqual(move, "e2e4")
        self.assertFalse(client.use_reasoning)
        self.assertEqual(calls["count"], 2)

    def test_openrouter_clock_expiry_does_not_spend_an_attempt(self):
        client = self.llm.OpenRouterChessClient()

        def fail_post(*args, **kwargs):
            raise AssertionError("_post must not run when clock is expired")

        client._post = fail_post
        move, reason = client.choose_move(chess.Board(), {"wtime": 0, "btime": 1000}, [])
        self.assertEqual(move, "0000")
        self.assertIn("clock expired", reason)
        self.assertEqual(client.invalid_model_moves, 0)


class CodexAttemptTests(unittest.TestCase):
    def setUp(self):
        self.codex = load_module(
            "codex_chess_uci_attempt_test",
            ROOT / "engines" / "codex-chess" / "codex_chess_uci.py",
        )

    def test_codex_default_and_env_attempt_limit(self):
        os.environ.pop("CODEX_CHESS_MAX_ATTEMPTS", None)
        self.assertEqual(self.codex.max_invalid_attempts(), 3)

        os.environ["CODEX_CHESS_MAX_ATTEMPTS"] = "5"
        try:
            self.assertEqual(self.codex.max_invalid_attempts(), 5)
            client = self.codex.CodexAppServer("gpt-test", "low")
            self.assertEqual(client.max_attempts, 5)
        finally:
            del os.environ["CODEX_CHESS_MAX_ATTEMPTS"]

    def test_codex_option_sets_attempt_limit(self):
        module = self.codex
        engine = module.CodexChessUci()
        engine.set_option(["name", "MaxAttempts", "value", "4"])
        self.assertEqual(engine.codex.max_attempts, 4)


class MatchRunnerAttemptTests(unittest.TestCase):
    def setUp(self):
        self.runner = load_module("play_engine_match_attempt_test", ROOT / "tools" / "play_engine_match.py")

    def _run_match(self, tmp: Path, forfeit_white: bool = False, black_openrouter: bool = False,
                   time_control_ms: int = 0):
        engine = tmp / "fake_engine.py"
        engine.write_text(fake_engine_script(), encoding="utf-8")
        launcher = tmp / "fake_engine.cmd"
        launcher.write_text(f'@echo off\npython "%~dp0fake_engine.py"\n', encoding="utf-8")
        live = tmp / "openrouter-test-live.pgn"
        argv = [
            "play_engine_match.py",
            "--white-path", str(launcher),
            "--black-path", str(launcher),
            "--white-name", "WhiteModel",
            "--black-name", "BlackModel",
            "--max-plies", "4",
            "--max-attempts", "3",
            "--live-pgn", str(live),
        ]
        if time_control_ms:
            argv += ["--time-control-ms", str(time_control_ms), "--increment-ms", "0"]
        if forfeit_white:
            argv += ["--white-env", "FAKE_ENGINE_FORFEIT=1"]
        if black_openrouter:
            argv += ["--black-openrouter-model", "meta/muse-spark-1.3-contributor"]
        old_argv = sys.argv
        sys.argv = argv
        try:
            try:
                self.runner.main()
            except SystemExit:
                pass
        finally:
            sys.argv = old_argv

        status = live.with_suffix(".status.json")
        return live, status

    def test_runner_writes_live_pgn_and_status_for_two_model_sides(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            live, status = self._run_match(tmp_path)
            self.assertTrue(live.exists(), "live PGN was not written")
            self.assertTrue(status.exists(), "live status sidecar was not written")
            payload = json.loads(status.read_text(encoding="utf-8"))
            self.assertEqual(payload["games"][0]["white"], "WhiteModel")
            self.assertEqual(payload["games"][0]["black"], "BlackModel")
            pgn_text = live.read_text(encoding="utf-8")
            self.assertIn("[MaxAttempts \"3\"]", pgn_text)

    def test_runner_records_model_forfeit_after_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            live, status = self._run_match(tmp_path, forfeit_white=True)
            payload = json.loads(status.read_text(encoding="utf-8"))
            self.assertTrue(payload["games"][0]["finished"])
            self.assertEqual(payload["games"][0]["result"], "0-1")
            self.assertIn("forfeited", payload["games"][0]["reason"])

    def test_runner_writes_ticking_clock_headers_in_time_control_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            live, _ = self._run_match(tmp_path, time_control_ms=120000)
            pgn_text = live.read_text(encoding="utf-8")
            self.assertIn("WhiteClockMs", pgn_text)
            self.assertIn("BlackClockMs", pgn_text)
            self.assertIn("ClockUpdatedAtEpochMs", pgn_text)
            self.assertIn("[%clk", pgn_text)
            clocks = re.findall(r"\[%clk [0-9:]+\]", pgn_text)
            self.assertTrue(clocks, "expected clock comments on played moves")


if __name__ == "__main__":
    unittest.main()
