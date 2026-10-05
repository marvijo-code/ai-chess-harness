import json
import sys
import tempfile
import unittest
from pathlib import Path

import chess
import chess.pgn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engines" / "llm-chess-engine"))
sys.path.insert(0, str(ROOT / "tools"))

import play_llm_series as series_mod  # noqa: E402
import subscription_providers as sp  # noqa: E402


class ProviderCommandTests(unittest.TestCase):
    def test_codex_command_is_isolated_and_carries_model_and_effort(self):
        with tempfile.TemporaryDirectory() as tmp:
            argv, last = sp.build_command("codex", "gpt-6-sol", "high", "codex.exe", Path(tmp))
        self.assertIn("--ignore-user-config", argv)
        self.assertIn("--ephemeral", argv)
        self.assertEqual(argv[argv.index("-m") + 1], "gpt-6-sol")
        self.assertIn("model_reasoning_effort=high", argv)
        self.assertIn("project_doc_max_bytes=0", argv)
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
        self.assertIsNotNone(last)

    def test_claude_command_has_no_tools_hooks_or_user_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            argv, last = sp.build_command("claude", "claude-sonnet-5-5", "high", "claude.exe", Path(tmp))
            settings = json.loads((Path(tmp) / "claude-settings.json").read_text(encoding="utf-8"))
        self.assertEqual(argv[argv.index("--model") + 1], "claude-sonnet-5-5")
        self.assertEqual(argv[argv.index("--effort") + 1], "high")
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "")
        self.assertIn("--no-session-persistence", argv)
        self.assertIn("--strict-mcp-config", argv)
        self.assertTrue(settings["disableAllHooks"])
        self.assertIsNone(last)

    def test_metered_keys_are_stripped(self):
        self.assertIn("OPENAI_API_KEY", sp.STRIPPED_ENV["codex"])
        self.assertIn("ANTHROPIC_API_KEY", sp.STRIPPED_ENV["claude"])
        self.assertIn("ANTHROPIC_BASE_URL", sp.STRIPPED_ENV["claude"])


class ReplyParsingTests(unittest.TestCase):
    def test_accepts_uci_and_san(self):
        board = chess.Board()
        self.assertEqual(sp.parse_reply('{"move": "e2e4", "comment": "centre"}', board)[0].uci(), "e2e4")
        move, comment, _ = sp.parse_reply('Sure: {"move": "Nf3", "comment": "develop"}', board)
        self.assertEqual(move.uci(), "g1f3")
        self.assertEqual(comment, "develop")
        self.assertEqual(sp.parse_reply("e4", board)[0].uci(), "e2e4")

    def test_rejects_illegal_ambiguous_and_prose(self):
        board = chess.Board()
        with self.assertRaisesRegex(ValueError, "not a legal move"):
            sp.parse_reply('{"move": "e2e5"}', board)
        with self.assertRaisesRegex(ValueError, "not the requested JSON"):
            sp.parse_reply("I think the best idea is to push the king pawn two squares", board)
        ambiguous = chess.Board("4k3/8/8/8/8/8/8/R3K2R w - - 0 1")
        ambiguous.remove_piece_at(chess.E1)
        ambiguous.set_piece_at(chess.H2, chess.Piece(chess.KING, chess.WHITE))
        with self.assertRaises(ValueError):
            sp.parse_reply('{"move": "Rd1"}', ambiguous)
        with self.assertRaisesRegex(ValueError, "empty"):
            sp.parse_reply("", board)


class AttemptLoopTests(unittest.TestCase):
    def make_client(self, replies):
        client = sp.SubscriptionChessClient("claude", lambda _msg: None)
        client.max_attempts = 3
        prompts = []

        def fake_ask(prompt, timeout):
            prompts.append(prompt)
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply

        client._ask = fake_ask
        return client, prompts

    def test_retry_feeds_back_rejection_and_then_plays(self):
        client, prompts = self.make_client(['{"move": "Ke3"}', '{"move": "e4", "comment": "ok"}'])
        move, comment = client.choose_move(chess.Board(), {}, [])
        self.assertEqual(move, "e2e4")
        self.assertEqual(comment, "ok")
        self.assertEqual(len(prompts), 2)
        self.assertIn("Attempt 1 was rejected", prompts[1])
        self.assertIn("'Ke3' is not a legal move", prompts[1])
        self.assertEqual(client.last_report["tries"], 2)
        self.assertEqual(client.last_report["illegal"], ["Ke3"])
        self.assertEqual(client.invalid_model_moves, 0)

    def test_three_failures_forfeit_without_fallback(self):
        client, prompts = self.make_client(["nonsense words here", '{"move": "Qh5"}', RuntimeError("cli died")])
        move, _ = client.choose_move(chess.Board(), {}, [])
        self.assertEqual(move, "0000")
        self.assertEqual(len(prompts), 3)
        self.assertEqual(client.last_report["illegal"], ["invalid", "Qh5", "error"])

    def test_cli_crash_retries_without_spending_a_chance(self):
        client = sp.SubscriptionChessClient("claude", lambda _msg: None)
        calls = []

        def fake_ask(prompt, timeout):
            calls.append(prompt)
            if len(calls) <= 2:
                raise sp.CliCrash("claude exited 3221226505 with no output")
            return '{"move": "d4", "comment": "centre"}'

        client._ask = fake_ask
        move, _ = client.choose_move(chess.Board(), {}, [])
        self.assertEqual(move, "d2d4")
        self.assertEqual(client.last_report["tries"], 1)
        self.assertEqual(client.last_report["illegal"], [])

    def test_expired_clock_forfeits_without_calling_model(self):
        client, prompts = self.make_client([])
        move, _ = client.choose_move(chess.Board(), {"wtime": 0, "btime": 1000}, [])
        self.assertEqual(move, "0000")
        self.assertEqual(prompts, [])

    def test_prompt_has_san_history_clocks_and_legal_lists(self):
        board = chess.Board()
        board.push_uci("e2e4")
        prompt = sp.build_prompt(board, {"wtime": 60000, "btime": 59000, "winc": 30000, "binc": 30000}, ["e2e4"], [])
        self.assertIn("You are playing Black", prompt)
        self.assertIn("Moves so far: 1. e4", prompt)
        self.assertIn("Clocks: you 0:59, opponent 1:00, +30s per move.", prompt)
        self.assertIn("Legal moves (SAN):", prompt)
        self.assertIn("e7e5", prompt)


class FakeEngine:
    def __init__(self, name, moves):
        self.name = name
        self.player = {"provider": "fake", "model": name, "effort": "high"}
        self.moves = list(moves)
        self.games = 0

    def new_game(self):
        self.games += 1

    def go(self, history, go_line):
        move = self.moves.pop(0)
        lines = ["info string my plan"]
        if move == "f2f3":
            lines.insert(0, "info string attempts tries=2 illegal=Ke3")
        return move, lines + [f"bestmove {move}"]


class SeriesRunnerTests(unittest.TestCase):
    def cfg(self):
        return {"maxAttempts": 3, "timeControlMs": 600000, "incrementMs": 5000, "maxPlies": 400, "games": 3}

    def series(self):
        return {"id": "llm-match-test", "games": 3, "player1": "A", "player2": "B",
                "score": {"player1": 0, "player2": 0}, "current_game": 1, "finished": False, "winner": None}

    def test_game_writes_series_headers_comments_and_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            live = Path(tmp) / "llm-match-test-live.pgn"
            writer = series_mod.SeriesWriter(live, self.series())
            white = FakeEngine("A", ["f2f3", "g2g4"])
            black = FakeEngine("B", ["e7e5", "d8h4"])
            summary = series_mod.play_game(1, white, black, self.cfg(), self.series(), writer, lambda _m: None)
            game = chess.pgn.read_game(live.open(encoding="utf-8"))
            status = json.loads(live.with_suffix(".status.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["result"], "0-1")
        self.assertEqual(summary["termination"], "checkmate")
        self.assertEqual(game.headers["SeriesId"], "llm-match-test")
        self.assertEqual(game.headers["SeriesGame"], "1")
        self.assertEqual(game.headers["SeriesGames"], "3")
        self.assertEqual(game.headers["WhiteInvalidAttempts"], "1")
        first = game.next()
        self.assertIn("[%clk", first.comment)
        self.assertIn("[%emt", first.comment)
        self.assertIn("[%tries 2]", first.comment)
        self.assertIn("[%illegal Ke3]", first.comment)
        self.assertTrue(first.comment.endswith("my plan"))
        self.assertEqual(status["games"][0]["result"], "0-1")
        self.assertTrue(status["games"][0]["finished"])
        self.assertEqual(status["series"]["id"], "llm-match-test")

    def test_forfeit_and_max_ply_adjudication(self):
        with tempfile.TemporaryDirectory() as tmp:
            writer = series_mod.SeriesWriter(Path(tmp) / "x-live.pgn", self.series())
            summary = series_mod.play_game(1, FakeEngine("A", ["0000"]), FakeEngine("B", []), self.cfg(),
                                           self.series(), writer, lambda _m: None)
            self.assertEqual(summary["result"], "0-1")
            self.assertIn("forfeited after 3 invalid replies", summary["termination"])
            cfg = self.cfg()
            cfg["maxPlies"] = 4
            summary = series_mod.play_game(2, FakeEngine("A", ["g1f3", "f3g1"]), FakeEngine("B", ["g8f6", "f6g8"]),
                                           cfg, self.series(), writer, lambda _m: None)
            self.assertEqual(summary["result"], "1/2-1/2")
            self.assertIn("safety cap", summary["termination"])
            self.assertEqual(len(writer.games), 2)

    def test_series_decided_best_of_three(self):
        self.assertFalse(series_mod.series_decided([1, 0], 1, 3))
        self.assertTrue(series_mod.series_decided([2, 0], 2, 3))
        self.assertFalse(series_mod.series_decided([1.5, 0.5], 2, 3))
        self.assertTrue(series_mod.series_decided([1.5, 0.5], 3, 3))

    def test_player_spec_and_config_defaults(self):
        player = series_mod.parse_player_spec("openrouter:x-ai/grok:4.3:low", "Grok")
        self.assertEqual(player, {"provider": "openrouter", "model": "x-ai/grok:4.3", "effort": "low", "name": "Grok"})
        cfg = series_mod.load_match_config(Path("does-not-exist.json"))
        self.assertEqual(cfg["games"], 3)
        self.assertEqual(cfg["maxAttempts"], 3)
        self.assertEqual(cfg["player1"]["model"], "gpt-6-sol")
        self.assertEqual(cfg["player2"]["model"], "claude-sonnet-5-5")


if __name__ == "__main__":
    unittest.main()
