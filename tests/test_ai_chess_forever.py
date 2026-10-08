"""Forever tournament: ladder, memory edits, cache-friendly prompts, usage, limit waits, pointer follow."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import chess

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "engines" / "llm-chess-engine"))

import ai_chess_forever as forever  # noqa: E402
import ai_chess_memory as mem  # noqa: E402
import play_llm_swiss as swiss  # noqa: E402
import subscription_providers as sp  # noqa: E402

EM, EN = chr(0x2014), chr(0x2013)  # dashes built from code points: none in this file
GO = {"wtime": 600000, "btime": 600000, "winc": 10000, "binc": 10000}
CONTEXT = {"memory": "# Memory\n- Castle early against GLM.\n", "header": "Tournament #1 (t1), Round 1, board 1.\nYou play White against Rival."}


def tmpdir(test) -> Path:
    path = Path(tempfile.mkdtemp(prefix="aichess-test-"))
    test.addCleanup(shutil.rmtree, path, True)
    return path


class LadderTest(unittest.TestCase):
    def game(self, white, black, result, depth=4):
        return {"id": "r1b1", "white": white, "black": black, "result": result, "stockfish_depth": depth,
                "termination": "checkmate"}

    def test_only_an_ai_win_against_stockfish_raises_the_depth(self):
        ladder = mem.new_ladder("Stockfish 19", 4)
        self.assertIsNone(mem.ladder_step(ladder, self.game("Sonnet", "Stockfish 19", "1/2-1/2"), "Stockfish 19", "t", 1, "now"))
        self.assertIsNone(mem.ladder_step(ladder, self.game("Sonnet", "Stockfish 19", "0-1"), "Stockfish 19", "t", 1, "now"))
        self.assertIsNone(mem.ladder_step(ladder, self.game("Sonnet", "GPT", "1-0"), "Stockfish 19", "t", 1, "now"))
        self.assertIsNone(mem.ladder_step(ladder, self.game("Sonnet", "Stockfish 19", "*"), "Stockfish 19", "t", 1, "now"))
        self.assertEqual(ladder["depth"], 4)
        step = mem.ladder_step(ladder, self.game("Stockfish 19", "GPT", "0-1"), "Stockfish 19", "t", 1, "now")
        self.assertEqual((step["from"], step["to"], step["winner"], step["color"], step["game"]), (4, 5, "GPT", "black", "r1b1"))
        mem.ladder_step(ladder, self.game("Sonnet", "Stockfish 19", "1-0", depth=5), "Stockfish 19", "t", 1, "now")
        self.assertEqual(ladder["depth"], 6)
        self.assertEqual([s["to"] for s in ladder["steps"]], [5, 6])

    def test_ladder_persists_and_later_games_use_the_new_depth(self):
        root = tmpdir(self)
        repo = mem.MemoryRepo(root)
        cfg = forever.load_forever_config(ROOT / "configs" / "ai-chess-vps.json")
        cfg["forever"]["reflection"] = False
        hooks = forever.ForeverHooks(cfg, repo, None, root / "live")
        state = swiss.new_state(cfg, "t-ladder")
        state["number"] = 1
        state["games"] = {"r1b1": {"id": "r1b1", "round": 1, "board": 1, "white": "Sonnet 5.5", "black": "Stockfish 19",
                                   "result": "*", "moves": []},
                          "r2b1": {"id": "r2b1", "round": 2, "board": 1, "white": "Stockfish 19", "black": "GPT-6.1 Sol",
                                   "result": "*", "moves": []}}
        ts = swiss.TournamentState(state, root / "live" / "t-ladder-tournament.json")
        (root / "live").mkdir()
        sent = []

        class FakeEngine:
            def __init__(self, player):
                self.player, self.name, self.is_uci = player, player["name"], player["provider"] == "uci"

            def send(self, line):
                sent.append((self.name, line))

        players = {p["name"]: p for p in state["players"]}
        hooks.before_game(ts, "r1b1", FakeEngine(players["Sonnet 5.5"]), FakeEngine(players["Stockfish 19"]))
        self.assertEqual(state["games"]["r1b1"]["stockfish_depth"], 4)
        self.assertTrue(any(line.startswith("setoption name GameContextFile value ") for _n, line in sent))
        state["games"]["r1b1"].update(result="1-0", termination="checkmate", pgn="")
        hooks.after_game(ts, "r1b1")
        self.assertEqual(json.loads((root / "ladder.json").read_text())["depth"], 5)
        hooks.before_game(ts, "r2b1", FakeEngine(players["Stockfish 19"]), FakeEngine(players["GPT-6.1 Sol"]))
        self.assertEqual(state["games"]["r2b1"]["stockfish_depth"], 5)
        self.assertEqual(players["Stockfish 19"]["depth"], 5)
        self.assertEqual(state["ladder"]["depth"], 5)
        # A restart loads the ladder from the repo, not from the config.
        again = forever.ForeverHooks(cfg, mem.MemoryRepo(root), None, root / "live")
        self.assertEqual(again.ladder["depth"], 5)
        self.assertEqual(again.ladder["steps"][0]["winner"], "Sonnet 5.5")


class MemoryEditTest(unittest.TestCase):
    def test_allowlist_caps_and_traversal(self):
        edits = [
            {"path": "MEMORY.md", "content": "# Index\n- lesson — with a dash\n"},
            {"path": "notes/openings.md", "content": "Sicilian: fine."},
            {"path": "../escape.md", "content": "x"},
            {"path": "notes/../../x.md", "content": "x"},
            {"path": "/etc/passwd", "content": "x"},
            {"path": "notes/Upper.md", "content": "x"},
            {"path": "games/r1b1.md", "content": "x"},
            {"path": "notes/big.md", "content": "y" * (mem.NOTE_MAX_BYTES + 1)},
            {"path": "MEMORY.md", "delete": True},
            {"path": "notes/old.md", "delete": True},
            {"path": "notes/empty.md", "content": "   "},
            "not an object",
        ]
        accepted, rejected = mem.validate_edits(edits, ["notes/old.md"])
        self.assertEqual([e["path"] for e in accepted], ["MEMORY.md", "notes/openings.md", "notes/old.md"])
        self.assertNotIn(EM, accepted[0]["content"])
        self.assertIn("- lesson - with a dash", accepted[0]["content"])
        self.assertEqual(len(rejected), 9)
        self.assertTrue(any("over the 4096-byte cap" in r for r in rejected))

    def test_memory_cap_and_note_count(self):
        accepted, rejected = mem.validate_edits([{"path": "MEMORY.md", "content": "z" * (mem.MEMORY_MAX_BYTES + 10)}], [])
        self.assertEqual(accepted, [])
        self.assertIn("6144-byte cap", rejected[0])
        existing = [f"notes/n{i}.md" for i in range(mem.MAX_NOTE_FILES)]
        accepted, rejected = mem.validate_edits([{"path": "notes/n0.md", "content": "update"},
                                                 {"path": "notes/new.md", "content": "one too many"}], existing)
        self.assertEqual([e["path"] for e in accepted], ["notes/n0.md"])
        self.assertIn("over the limit", rejected[0])
        self.assertEqual(mem.validate_edits("nope", [])[1], ["edits is not a list"])

    def test_reflection_applies_validated_edits_only(self):
        root = tmpdir(self)
        repo = mem.MemoryRepo(root)
        cfg = forever.load_forever_config(ROOT / "configs" / "ai-chess-vps.json")
        reply = json.dumps({"summary": "I hung a knight.", "edits": [
            {"path": "MEMORY.md", "content": "# MEMORY\n- Check knight safety.\n- Notes: notes/tactics.md\n"},
            {"path": "notes/tactics.md", "content": "Knight forks on c7."},
            {"path": "../../steal.md", "content": "no"}]})

        class FakeClient:
            last_report = {"usage": {"calls": 1, "input": 900, "cached": 0, "output": 50}}

            def ask_text(self, system, prompt, timeout):
                FakeClient.prompt = prompt
                return "```json\n" + reply + "\n```"

        game = {"id": "r1b1", "white": "Sonnet 5.5", "black": "GPT-6.1 Sol", "result": "0-1", "termination": "checkmate",
                "round": 1, "pgn": "1. e4 e5 *", "notes": [{"ply": 1, "player": "Sonnet 5.5", "san": "e4", "note": "open game"}],
                "moves": [{"ply": 1, "side": "white", "san": "e4", "uci": "e2e4"}]}
        state = {"id": "t1", "number": 1, "title": "T #1"}
        out = forever.reflect({"name": "Sonnet 5.5", "provider": "claude", "model": "m"}, state, game, repo, cfg,
                              {"1": "?!"}, lambda p, c: FakeClient())
        self.assertEqual(out["applied"], ["MEMORY.md", "notes/tactics.md"])
        self.assertEqual(len(out["rejected"]), 1)
        self.assertIn("Check knight safety", repo.memory_text("Sonnet 5.5"))
        self.assertFalse((root / "steal.md").exists())
        self.assertIn("after ply 1 (e4): open game", FakeClient.prompt)
        self.assertIn("ply 1 e4?!", FakeClient.prompt)
        self.assertIn("1. e4 e5", FakeClient.prompt)

    def test_git_pusher_commits_without_dashes_and_without_a_remote(self):
        root = tmpdir(self)
        mem.ensure_repo(root, None)
        repo = mem.MemoryRepo(root)
        pusher = mem.GitPusher(repo, push=False)
        repo.write_file("agents/x/MEMORY.md", "hello " + EN + " world")
        pusher.request("Tournament #1 r1b1: A 1-0 B " + EM + " ladder")
        self.assertTrue(pusher.flush(30))
        log = subprocess.run(["git", "-C", str(root), "log", "--format=%s"], capture_output=True, text=True, encoding="utf-8")
        self.assertIn("Tournament #1 r1b1: A 1-0 B - ladder", log.stdout)
        self.assertNotIn(EM, log.stdout)
        self.assertEqual((root / "agents/x/MEMORY.md").read_text(encoding="utf-8"), "hello - world\n")


class PromptOrderTest(unittest.TestCase):
    def board_after(self, ucis):
        board = chess.Board()
        for uci in ucis:
            board.push_uci(uci)
        return board

    def test_full_prompt_order_static_first_position_last(self):
        history = ["e2e4", "e7e5", "g1f3", "b8c6"]
        a = sp.build_full_turn(CONTEXT, self.board_after(history), GO, history)
        b = sp.build_full_turn(CONTEXT, self.board_after(history + ["f1b5", "a7a6"]),
                               {**GO, "wtime": 590000, "btime": 581000}, history + ["f1b5", "a7a6"])
        self.assertTrue(a.startswith(sp.RULES_TEXT + "\n\n" + sp.memory_section(CONTEXT) + "\n\n" + sp.game_header_section(CONTEXT)))
        # Everything before the moves line is byte-identical; the move list only grows at its end.
        cut = a.index("MOVES SO FAR\n") + len("MOVES SO FAR\n")
        self.assertEqual(a[:cut], b[:cut])
        self.assertTrue(b[cut:].startswith(a[cut:a.index("\n", cut)]))
        # The position block (FEN, clocks, legal moves) is the very end; nothing that changes comes earlier.
        pos = a.index("POSITION (")
        self.assertNotIn("Clocks:", a[:pos])
        self.assertNotIn("FEN:", a[:pos])
        self.assertTrue(a.rstrip().endswith("Reply with ONLY the JSON object."))
        self.assertIn("Legal moves (UCI):", a[pos:])

    def test_rules_text_is_the_same_for_every_player_and_game(self):
        other = {"memory": "", "header": "Tournament #9 (t9), Final.\nYou play Black against Someone."}
        board = self.board_after(["d2d4"])
        a = sp.build_full_turn(CONTEXT, chess.Board(), GO, [])
        b = sp.build_full_turn(other, board, {**GO, "btime": 1000}, ["d2d4"])
        self.assertEqual(a[:len(sp.RULES_TEXT) + 2], b[:len(sp.RULES_TEXT) + 2])
        for changing in ("FEN:", "Clocks:", "Legal moves", "{side}"):
            self.assertNotIn(changing, sp.RULES_TEXT)

    def fake_http_client(self):
        os.environ.setdefault("OPENCODE_GO_API_KEY", "test-key")
        client = sp.SubscriptionChessClient("opencode-go", lambda _m: None)
        client.model, client.board_image, client.conversation = "deepseek-v4.1-flash", False, True
        client.context = dict(CONTEXT)
        payloads = []
        replies = iter(['{"move": "Nf3", "comment": "develop", "note": "aim for d4 next"}',
                        '{"move": "Bb5", "comment": "pin"}'])

        def stream(url, payload, headers, timeout, cutoff):
            payloads.append(json.loads(json.dumps(payload)))
            return {"content": next(replies), "reasoning": "", "cut": False,
                    "usage": {"prompt_tokens": 1000 * len(payloads), "prompt_tokens_details": {"cached_tokens": 900 * (len(payloads) - 1)},
                              "completion_tokens": 20}}

        client.http_stream = stream
        return client, payloads

    def test_conversation_requests_are_prefixes_of_each_other(self):
        client, payloads = self.fake_http_client()
        h1 = ["e2e4", "e7e5"]
        move, _ = client.choose_move(self.board_after(h1), GO, h1)
        self.assertEqual(move, "g1f3")
        self.assertEqual(client.last_report["note"], "aim for d4 next")
        h2 = h1 + ["g1f3", "b8c6"]
        move, _ = client.choose_move(self.board_after(h2), {**GO, "wtime": 595000}, h2)
        self.assertEqual(move, "f1b5")
        first, second = payloads[0]["messages"], payloads[1]["messages"]
        self.assertEqual(first[0]["content"], sp.SYSTEM_PROMPT)
        self.assertTrue(first[1]["content"].startswith(sp.RULES_TEXT))
        # The second request starts with the first request byte for byte (then the reply, then the new turn).
        self.assertEqual(json.dumps(second[:2]), json.dumps(first))
        self.assertEqual(second[2], {"role": "assistant", "content": '{"move": "Nf3", "comment": "develop", "note": "aim for d4 next"}'})
        self.assertTrue(second[3]["content"].startswith("MOVES SINCE YOUR LAST TURN\n2. Nf3 Nc6\n\nPOSITION (move 3"), second[3]["content"][:60])
        self.assertIn("POSITION (", second[3]["content"])
        self.assertEqual(client.last_report["usage"], {"calls": 1, "input": 2000, "cached": 900, "output": 20})

    def test_claude_session_is_started_then_resumed(self):
        client = sp.SubscriptionChessClient("claude", lambda _m: None)
        client.board_image, client.conversation, client.context = False, True, dict(CONTEXT)
        calls = []

        def runner(argv, prompt, timeout, workdir):
            calls.append((list(argv), prompt))
            usage = {"input_tokens": 3, "cache_creation_input_tokens": 400, "cache_read_input_tokens": 3000 * len(calls) - 3000, "output_tokens": 30}
            reply = '{"move": "e5"}' if len(calls) == 1 else '{"move": "Nc6"}'
            return json.dumps({"type": "result", "result": reply, "duration_api_ms": 1000, "usage": usage}) + "\n"

        client.runner = runner
        board = self.board_after(["e2e4"])
        client.choose_move(board, GO, ["e2e4"])
        board2 = self.board_after(["e2e4", "e7e5", "g1f3"])
        client.choose_move(board2, GO, ["e2e4", "e7e5", "g1f3"])
        (argv1, prompt1), (argv2, prompt2) = calls
        sid = argv1[argv1.index("--session-id") + 1]
        self.assertNotIn("--no-session-persistence", argv1)
        self.assertEqual(argv2[argv2.index("--resume") + 1], sid)
        self.assertTrue(prompt1.startswith(sp.RULES_TEXT))
        self.assertTrue(prompt2.startswith("MOVES SINCE YOUR LAST TURN\n1... e5 2. Nf3"), prompt2[:50])
        self.assertEqual(client.last_report["usage"]["cached"], 3000)
        self.assertEqual(client.last_report["usage"]["input"], 3403)

    def test_codex_resume_usage_is_the_difference_of_session_totals(self):
        client = sp.SubscriptionChessClient("codex", lambda _m: None)
        client.board_image, client.conversation, client.context = False, True, dict(CONTEXT)
        totals = iter([(11247, 6912), (22576, 17000)])
        replies = iter(['{"move": "e5"}', '{"move": "Nc6"}'])
        seen = []

        def runner(argv, prompt, timeout, workdir):
            seen.append(argv)
            total, cached = next(totals)
            Path(argv[argv.index("-o") + 1]).write_text(next(replies), encoding="utf-8")
            return ('{"type":"thread.started","thread_id":"0199aaaa-bbbb-cccc-dddd-eeeeffff0000"}\n'
                    '{"type":"turn.started"}\n'
                    + json.dumps({"type": "turn.completed", "usage": {"input_tokens": total, "cached_input_tokens": cached, "output_tokens": 9}}) + "\n")

        client.runner = runner
        client.choose_move(self.board_after(["e2e4"]), GO, ["e2e4"])
        self.assertEqual(client.last_report["usage"]["input"], 11247)
        client.choose_move(self.board_after(["e2e4", "e7e5", "g1f3"]), GO, ["e2e4", "e7e5", "g1f3"])
        self.assertEqual(client.last_report["usage"], {"calls": 1, "input": 11329, "cached": 10088, "output": 0})
        self.assertEqual(sp.codex_usage('{"type":"turn.completed","usage":{"input_tokens":5,"cached_input_tokens":2,"output_tokens":1}}'),
                         {"input": 5, "cached": 2, "output": 1})
        self.assertNotIn("--ephemeral", seen[0])
        self.assertEqual(seen[1][1:3], ["exec", "resume"])
        self.assertEqual(seen[1][-2:], ["0199aaaa-bbbb-cccc-dddd-eeeeffff0000", "-"])


class UsageParsingTest(unittest.TestCase):
    def test_http_usage_variants(self):
        self.assertEqual(sp.http_usage({"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 64}, "completion_tokens": 5}),
                         {"input": 100, "cached": 64, "output": 5})
        self.assertEqual(sp.http_usage({"prompt_tokens": 100, "prompt_cache_hit_tokens": 96, "prompt_cache_miss_tokens": 4})["cached"], 96)
        self.assertEqual(sp.http_usage({})["input"], 0)

    def test_claude_usage_counts_cache_writes_as_input(self):
        out = json.dumps({"type": "result", "usage": {"input_tokens": 2, "cache_creation_input_tokens": 222, "cache_read_input_tokens": 3204}})
        self.assertEqual(sp.claude_usage(out), {"input": 3428, "cached": 3204, "output": 0, "cache_write": 222})

    def test_cache_stats_per_player_with_warm_rate(self):
        moves = [{"side": "white" if i % 2 == 0 else "black", "usage": {"calls": 1, "input": 1000, "cached": 0 if i < 2 else 950}}
                 for i in range(10)]
        state = {"games": {"g": {"white": "A", "black": "B", "moves": moves}}}
        stats = swiss.compute_cache_stats(state)
        self.assertEqual(stats["A"]["input"], 5000)
        self.assertEqual(stats["A"]["cached"], 3800)
        self.assertEqual(stats["A"]["hit_rate"], 0.76)
        self.assertEqual(stats["A"]["warm_hit_rate"], 0.95)


class LimitWaitTest(unittest.TestCase):
    def test_a_usage_limit_waits_and_asks_the_same_move_again(self):
        os.environ.setdefault("OPENCODE_GO_API_KEY", "test-key")
        client = sp.SubscriptionChessClient("opencode-go", lambda _m: None)
        client.limit_wait, client.board_image = True, False
        waits, slept, prompts = [], [], []
        client.on_limit_wait = lambda secs, why: waits.append((secs, why))
        client.sleep = lambda s: slept.append(s)
        crash = sp.CliCrash('HTTP 429: {"error":{"type":"GoUsageLimitError","message":"Go usage limit exceeded"}}')
        answers = iter([crash, crash, crash,   # two quick infrastructure retries, then the limit wait
                        sp.ProviderError("HTTP 429: rate limit reached, try again in 30s"),
                        {"content": '{"move": "e4"}', "reasoning": "", "cut": False, "usage": {}}])

        def stream(url, payload, headers, timeout, cutoff):
            prompts.append(payload["messages"][-1]["content"])
            item = next(answers)
            if isinstance(item, Exception):
                raise item
            return item

        client.http_stream = stream
        move, _ = client.choose_move(chess.Board(), GO, [])
        self.assertEqual(move, "e2e4")
        self.assertEqual(client.last_report["tries"], 1, "a limit is not a rejected reply")
        self.assertEqual(client.last_report["illegal"], [])
        self.assertEqual(len(waits), 2)
        self.assertEqual(slept, [60.0, 120.0])
        # The CliCrash is retried twice as infrastructure before the limit wait sees it.
        self.assertEqual(len(set(prompts)), 1, "the same request every time")
        self.assertLess(client.last_report["think_ms"], 5000)

    def test_without_limit_wait_a_plan_limit_still_voids(self):
        os.environ.setdefault("OPENCODE_GO_API_KEY", "test-key")
        client = sp.SubscriptionChessClient("opencode-go", lambda _m: None)
        client.limit_wait, client.board_image = False, False

        def limited(url, payload, headers, timeout, cutoff):
            raise sp.CliCrash('HTTP 429: {"error":{"message":"Go usage limit exceeded"}}')

        client.http_stream = limited
        client._ask_with_crash_retries = lambda prompt, timeout: limited(None, None, None, None, None)
        move, comment = client.choose_move(chess.Board(), GO, [])
        self.assertEqual(move, "0000")
        self.assertTrue(comment.startswith("provider unavailable"))

    def test_limit_markers_and_reset_time(self):
        self.assertTrue(sp.limit_error("Claude AI usage limit reached|1791460000"))
        self.assertTrue(sp.limit_error("You've hit your usage limit. Try again at 3:15 PM."))
        self.assertTrue(sp.limit_error("HTTP 429: slow down"))
        self.assertFalse(sp.limit_error('"input_tokens": 4290'))
        self.assertFalse(sp.limit_error("the reply was not the requested JSON object"))
        self.assertEqual(sp.limit_wait_seconds("Claude AI usage limit reached|1791460000", 0, now=1791459900), 105)
        self.assertEqual(sp.limit_wait_seconds("rate limit", 0), 60.0)
        self.assertEqual(sp.limit_wait_seconds("rate limit", 9), sp.LIMIT_WAIT_MAX_SECONDS)

    def test_runner_moves_its_deadline_out_while_the_engine_waits(self):
        class FakeEngine:
            name = "X"

            def __init__(self):
                self.cond = threading.Condition()
                self.lines = []

        engine = FakeEngine()
        seen = []

        def feed():
            import time as _t
            with engine.cond:
                engine.lines.append("info string limitwait 1 usage limit")
                engine.cond.notify_all()
            _t.sleep(1.5)
            with engine.cond:
                engine.lines.append("bestmove e2e4")
                engine.cond.notify_all()

        threading.Thread(target=feed, daemon=True).start()
        lines = swiss.wait_bestmove(engine, 1.0, lambda used: None, lambda s, why: seen.append((s, why)))
        self.assertEqual(lines[-1], "bestmove e2e4")
        self.assertEqual(seen, [(1, "usage limit")])


class PointerFollowTest(unittest.TestCase):
    def test_viewer_follows_the_pointer_without_restart(self):
        import llm_tournament_viewer as viewer

        live = tmpdir(self)
        for sid in ("aichess-0001-a", "aichess-0002-b"):
            (live / f"{sid}-tournament.json").write_text(json.dumps({"id": sid, "games": {}, "players": []}), encoding="utf-8")
        pointer = live / "current.json"
        pointer.write_text(json.dumps({"state_path": "aichess-0001-a-tournament.json", "id": "aichess-0001-a", "number": 1}))
        saved = (viewer.Handler.follow, viewer.Handler.live_dir, viewer.Handler.analyzer, viewer.Handler.annotator,
                 viewer.Handler.commentator, viewer.Handler._follow_cache)
        viewer.Handler.follow, viewer.Handler.live_dir, viewer.Handler._follow_cache = pointer, live, {}
        viewer.Handler.analyzer = viewer.Handler.annotator = viewer.Handler.commentator = None

        def restore():
            (viewer.Handler.follow, viewer.Handler.live_dir, viewer.Handler.analyzer, viewer.Handler.annotator,
             viewer.Handler.commentator, viewer.Handler._follow_cache) = saved

        self.addCleanup(restore)
        server = ThreadingHTTPServer(("127.0.0.1", 0), viewer.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_address[1]}/api/tournament"
        first = json.loads(urllib.request.urlopen(url, timeout=10).read())
        self.assertEqual(first["id"], "aichess-0001-a")
        import time as _t
        _t.sleep(0.05)
        forever.write_pointer(live, live / "aichess-0002-b-tournament.json", {"id": "aichess-0002-b", "number": 2})
        second = json.loads(urllib.request.urlopen(url, timeout=10).read())
        self.assertEqual(second["id"], "aichess-0002-b")
        self.assertEqual(forever.read_pointer(pointer)["number"], 2)


class ForeverLoopTest(unittest.TestCase):
    def test_finished_tournaments_are_followed_by_the_next_number_and_a_restart_resumes(self):
        root = tmpdir(self)
        live = root / "live"
        repo = mem.MemoryRepo(root / "memory")
        cfg = forever.load_forever_config(ROOT / "configs" / "ai-chess-vps.json")
        self.assertNotIn("Gemini 3.8 Flash", [p["name"] for p in cfg["players"]], "disabled players stay out")
        self.assertEqual(cfg["rounds"], 5)
        hooks = forever.ForeverHooks(cfg, repo, None, live)
        played = []
        original = swiss.run_tournament

        def fake_run(state, cfg_, status_path, log):
            played.append((state["id"], state["number"], forever.read_pointer(live / "current.json")["id"]))
            self.assertEqual(state["ladder"]["depth"], 4)
            state["finished"] = True
            swiss.TournamentState(state, status_path).save()
            return 0

        swiss.run_tournament = fake_run
        self.addCleanup(setattr, swiss, "run_tournament", original)
        forever.run_forever(cfg, live, repo, None, hooks, max_tournaments=2, sleep=lambda s: None)
        self.assertEqual([p[1] for p in played], [1, 2])
        self.assertTrue(all(p[0] == p[2] for p in played), "the pointer names the live tournament")
        self.assertTrue(played[0][0].startswith("aichess-0001-"))
        index = json.loads((root / "memory" / "tournaments" / "index.json").read_text())
        self.assertEqual([t["number"] for t in index["tournaments"]], [1, 2])
        # An unfinished tournament named by the pointer is resumed, not replaced.
        pointer = forever.read_pointer(live / "current.json")
        state = json.loads(Path(pointer["state_path"]).read_text())
        state["finished"] = False
        Path(pointer["state_path"]).write_text(json.dumps(state))
        forever.run_forever(cfg, live, repo, None, hooks, max_tournaments=1, sleep=lambda s: None)
        self.assertEqual(played[-1][1], 2)
        self.assertEqual(played[-1][0], played[1][0])


class BinaryResolutionTest(unittest.TestCase):
    def test_linux_finds_local_bin_when_path_is_short(self):
        if os.name == "nt":
            self.skipTest("POSIX lookup")
        home = tmpdir(self)
        exe = home / ".local" / "bin" / "claude"
        exe.parent.mkdir(parents=True)
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
        old = dict(os.environ)
        self.addCleanup(os.environ.update, old)
        os.environ.pop("LLM_CLAUDE_BIN", None)
        os.environ["PATH"] = "/nonexistent"
        os.environ["HOME"] = str(home)
        self.assertEqual(sp.resolve_binary("claude"), str(exe))

    def test_override_wins(self):
        old = os.environ.get("LLM_CODEX_BIN")
        os.environ["LLM_CODEX_BIN"] = "/opt/codex"
        self.addCleanup(lambda: os.environ.pop("LLM_CODEX_BIN") if old is None else os.environ.update(LLM_CODEX_BIN=old))
        self.assertEqual(sp.resolve_binary("codex"), "/opt/codex")


if __name__ == "__main__":
    unittest.main()
