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
            {"path": "MEMORY.md", "content": "# Index\n- lesson " + EM + " with a dash\n"},
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

    def test_reads_other_players_writes_only_own_folder_and_retries_an_oversize_memory(self):
        root = tmpdir(self)
        repo = mem.MemoryRepo(root, folders={"Sonnet 5.5": "claude", "GPT-6.1 Sol": "gpt"})
        repo.write_file("agents/gpt/MEMORY.md", "# GPT\n- Against Sonnet: the Chigorin holds.\n")
        repo.write_file("agents/deepseek/MEMORY.md", "# DeepSeek\n- Count attackers before captures.\n")
        cfg = forever.load_forever_config(ROOT / "configs" / "ai-chess-vps.json")
        replies = [json.dumps({"summary": "Lost the c-file.", "edits": [
                       {"path": "MEMORY.md", "content": "x" * (mem.MEMORY_MAX_BYTES + 50)},
                       {"path": "notes/c-file.md", "content": "Contest the open c-file."},
                       {"path": "agents/gpt/MEMORY.md", "content": "overwritten by Sonnet"}]}),
                   json.dumps({"summary": "", "edits": [
                       {"path": "MEMORY.md", "content": "# MEMORY\n- Contest open files.\n- Notes: notes/c-file.md\n"}]})]
        prompts = []

        class FakeClient:
            last_report = {}

            def ask_text(self, system, prompt, timeout):
                prompts.append(prompt)
                return replies[len(prompts) - 1]

        game = {"id": "r1b1", "white": "Sonnet 5.5", "black": "GPT-6.1 Sol", "result": "0-1", "termination": "checkmate",
                "round": 1, "pgn": "1. e4 e5 *", "notes": [], "moves": []}
        out = forever.reflect({"name": "Sonnet 5.5", "provider": "claude", "model": "m"}, {"id": "t1", "number": 1},
                              game, repo, cfg, {}, lambda p, c: FakeClient())
        self.assertIn("Your folder is agents/claude/", prompts[0])
        self.assertIn("--- agents/gpt/MEMORY.md ---\n# GPT\n- Against Sonnet: the Chigorin holds.", prompts[0])
        self.assertIn("--- agents/deepseek/MEMORY.md ---", prompts[0])
        self.assertEqual(out["read_others"], ["deepseek", "gpt"])
        self.assertEqual(len(prompts), 2)
        self.assertIn("YOUR REPLY WAS NOT FULLY ACCEPTED", prompts[1])
        self.assertIn("over the 6144-byte cap", prompts[1])
        self.assertIn("outside your own folder", prompts[1])
        self.assertEqual(out["retries"], 1)
        self.assertEqual(sorted(out["applied"]), ["MEMORY.md", "notes/c-file.md"])
        self.assertEqual(out["rejected"], [])
        self.assertIn("Contest open files", (root / "agents/claude/MEMORY.md").read_text(encoding="utf-8"))
        self.assertEqual((root / "agents/gpt/MEMORY.md").read_text(encoding="utf-8"),
                         "# GPT\n- Against Sonnet: the Chigorin holds.\n")
        self.assertFalse((root / "agents/claude/agents").exists())
        self.assertEqual(out["memory_after"], mem.text_sha(repo.memory_text("Sonnet 5.5")))

    def test_reflection_json_with_raw_line_breaks_parses(self):
        data, why = mem.parse_reflection('{"summary": "ok", "edits": [{"path": "MEMORY.md", "content": "# M\n- a\n"}]}')
        self.assertEqual(why, "")
        self.assertEqual(data["edits"][0]["content"], "# M\n- a\n")

    def test_folders_move_to_the_model_family_once_with_history(self):
        root = tmpdir(self)
        mem.ensure_repo(root, None)
        mem.MemoryRepo(root).write_file("agents/sonnet-5-5/MEMORY.md", "# Sonnet\n")
        subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "first"], check=True)
        cfg = forever.load_forever_config(ROOT / "configs" / "ai-chess-vps.json")
        folders = mem.player_folders(cfg["players"])
        self.assertEqual(folders["Sonnet 5.5"], "claude")
        self.assertEqual(folders["DeepSeek V4.1 Flash"], "deepseek")
        self.assertNotIn("Stockfish 19", folders)
        repo = mem.MemoryRepo(root, folders=folders)
        self.assertEqual(repo.migrate_folders(), ["agents/sonnet-5-5 -> agents/claude"])
        self.assertEqual(repo.memory_text("Sonnet 5.5"), "# Sonnet\n")
        self.assertFalse((root / "agents/sonnet-5-5").exists())
        self.assertEqual(repo.migrate_folders(), [])
        status = subprocess.run(["git", "-C", str(root), "status", "--porcelain"], capture_output=True, text=True).stdout
        self.assertIn("R  agents/sonnet-5-5/MEMORY.md -> agents/claude/MEMORY.md", status)

    def test_learning_check_counts_read_and_written_memory(self):
        sha = mem.text_sha("# M\n")
        state = {"id": "t1", "number": 1, "games": {
            "r1b1": {"status": "finished", "white": "A", "black": "B",
                     "memory_read": {"white": {"player": "A", "sha": sha, "expected": sha, "bytes": 4},
                                     "black": {"player": "B", "sha": sha, "expected": "stale0000000", "bytes": 4}},
                     "moves": [{"side": "white", "memory_sha": sha}, {"side": "black", "memory_sha": sha},
                               {"side": "white", "memory_sha": sha}, {"side": "black"}],
                     "reflection": {"A": {"applied": ["MEMORY.md", "notes/x.md"], "rejected": [], "retries": 1},
                                    "B": {"applied": [], "rejected": ["too big"], "error": ""}}},
            "r1b2": {"status": "live", "memory_read": {"white": {"player": "A", "sha": sha}}, "moves": []}}}
        rows = mem.learning_rows(state)
        self.assertEqual({k: rows["A"][k] for k in ("games", "read_in_prompt", "read_latest", "memory_updates",
                                                    "note_updates", "retries")},
                         {"games": 1, "read_in_prompt": 1, "read_latest": 1, "memory_updates": 1, "note_updates": 1, "retries": 1})
        self.assertEqual((rows["B"]["read_in_prompt"], rows["B"]["read_latest"], rows["B"]["rejected"]), (0, 0, 1))
        root = tmpdir(self)
        repo = mem.MemoryRepo(root, folders={"A": "a"})
        repo.record_learning(state, {"A": sha})
        self.assertEqual(json.loads((root / "tournaments/learning.json").read_text())["last_written"], {"A": sha})
        self.assertIn("| A | 1 | 1 | 1 | 1 | 1 | 1 | 0 | 1 | 0 |", (root / "tournaments/learning.md").read_text())

    def test_every_move_request_reports_the_memory_it_carried(self):
        client = sp.SubscriptionChessClient("claude", lambda m: None)
        client.context = dict(CONTEXT)
        client.last_report = {}
        text = client._turn_text(chess.Board(), GO, [], [], None)
        self.assertIn(CONTEXT["memory"].strip(), text)
        self.assertEqual(client.last_report["memory"], {"sha": mem.text_sha(CONTEXT["memory"]),
                                                        "bytes": len(CONTEXT["memory"].encode("utf-8"))})
        import play_llm_series as series
        self.assertEqual(series.parse_memory(["info string usage {}", "info string memory abc123def456 31"]), "abc123def456")
        # The fingerprint line is never taken for the move comment.
        self.assertEqual(series.parse_info(["info string memory abc123def456 31"])[0], "")

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

    def test_claude_game_blocks_grow_append_only_with_the_breakpoint_on_the_last(self):
        client = sp.SubscriptionChessClient("claude", lambda _m: None)
        client.board_image, client.conversation, client.context = False, True, dict(CONTEXT)
        calls = []

        def runner(argv, prompt, timeout, workdir):
            calls.append((list(argv), json.loads(prompt)))
            usage = {"input_tokens": 3, "cache_creation_input_tokens": 400, "cache_read_input_tokens": 3000 * len(calls) - 3000, "output_tokens": 30}
            reply = '{"move": "e5"}' if len(calls) == 1 else '{"move": "Nc6"}'
            return json.dumps({"type": "result", "result": reply, "duration_api_ms": 1000, "usage": usage}) + "\n"

        client.runner = runner
        client.choose_move(self.board_after(["e2e4"]), GO, ["e2e4"])
        client.choose_move(self.board_after(["e2e4", "e7e5", "g1f3"]), GO, ["e2e4", "e7e5", "g1f3"])
        (argv1, msg1), (argv2, msg2) = calls
        for argv in (argv1, argv2):  # one fresh request per move, the game travels in the blocks
            self.assertIn("--no-session-persistence", argv)
            self.assertEqual(argv[argv.index("--input-format") + 1], "stream-json")
            self.assertNotIn("--resume", argv)
        first, second = msg1["message"]["content"], msg2["message"]["content"]
        self.assertEqual(len(first), 1)
        self.assertTrue(first[0]["text"].startswith(sp.RULES_TEXT))
        self.assertIn("cache_control", first[0])
        # The second request starts with the first one's turn, byte for byte, then the reply, then the new turn.
        self.assertEqual(second[0]["text"], first[0]["text"])
        self.assertEqual(second[1]["text"], 'YOUR REPLY\n{"move": "e5"}')
        self.assertTrue(second[2]["text"].startswith("MOVES SINCE YOUR LAST TURN\n1... e5 2. Nf3"), second[2]["text"][:50])
        self.assertEqual([("cache_control" in b) for b in second], [False, False, True], "one breakpoint, on the last block")
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
        self.assertNotIn("MiMo V2.6 Pro", [p["name"] for p in cfg["players"]])
        self.assertEqual(cfg["rounds"], 5, "5 players: Sonnet, GPT, DeepSeek, GLM, Stockfish")
        cfg["forever"]["rosterCheck"] = False
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


class RosterTest(unittest.TestCase):
    def cfg(self):
        cfg = forever.load_forever_config(ROOT / "configs" / "ai-chess-vps.json")
        cfg["forever"]["reflection"] = False
        return cfg

    def test_players_that_hit_a_limit_are_benched_and_the_rest_play(self):
        root = tmpdir(self)
        live = root / "live"
        repo = mem.MemoryRepo(root / "memory")
        cfg = self.cfg()
        failing = {"GLM 5.3 Flash": 'HTTP 429: {"error":{"type":"GoUsageLimitError","message":"Go usage limit exceeded"}}',
                   "DeepSeek V4.1 Flash": "HTTP 403: AccessDenied.Unpurchased"}

        def prober(player, cfg_, live_dir, timeout):
            if player["name"] in failing:
                reason = failing[player["name"]]
                return {"name": player["name"], "ok": False, "reason": reason, "kind": forever.bench_kind(reason),
                        "resets_at": "2026-10-14T07:24:17.000Z" if "Go usage" in reason else None, "route": player["provider"]}
            return {"name": player["name"], "ok": True, "move": "1...e5"}

        seen = []
        original_run, original_check = swiss.run_tournament, forever.check_roster
        swiss.run_tournament = lambda state, c, path, log: (seen.append(state), state.update(finished=True), 0)[-1]
        forever.check_roster = lambda c, d: original_check(c, d, 5, prober)
        self.addCleanup(setattr, swiss, "run_tournament", original_run)
        self.addCleanup(setattr, forever, "check_roster", original_check)
        hooks = forever.ForeverHooks(cfg, repo, None, live)
        forever.run_forever(cfg, live, repo, None, hooks, max_tournaments=1, sleep=lambda s: None)
        state = seen[0]
        self.assertEqual([p["name"] for p in state["players"]], ["Sonnet 5.5", "GPT-6.1 Sol", "Stockfish 19"])
        self.assertEqual(state["config"]["rounds"], 3)
        kinds = {b["name"]: (b["kind"], b["resets_at"]) for b in state["benched"]}
        self.assertEqual(kinds["GLM 5.3 Flash"], ("subscription limit", "2026-10-14T07:24:17.000Z"))
        self.assertEqual(kinds["DeepSeek V4.1 Flash"][0], "no subscription funds or plan")
        md = (root / "memory" / "tournaments" / f"{state['id']}.md").read_text(encoding="utf-8")
        self.assertIn("## Benched for this tournament", md)
        self.assertIn("GLM 5.3 Flash", md)
        self.assertEqual(forever.bench_label(state["benched"][0]).split(":")[0], "benched")

    def test_too_few_players_waits_and_checks_again(self):
        root = tmpdir(self)
        live = root / "live"
        repo = mem.MemoryRepo(root / "memory")
        cfg = self.cfg()
        calls = {"n": 0}

        def prober(player, cfg_, live_dir, timeout):
            ok = calls["n"] >= len(cfg["players"]) or player["provider"] == "uci"
            calls["n"] += 1
            return {"name": player["name"], "ok": ok, "reason": "" if ok else "usage limit", "kind": "subscription limit"}

        slept, seen = [], []
        original_run, original_check = swiss.run_tournament, forever.check_roster
        swiss.run_tournament = lambda state, c, path, log: (seen.append(state), state.update(finished=True), 0)[-1]
        forever.check_roster = lambda c, d: original_check(c, d, 5, prober)
        self.addCleanup(setattr, swiss, "run_tournament", original_run)
        self.addCleanup(setattr, forever, "check_roster", original_check)
        forever.run_forever(cfg, live, repo, None, forever.ForeverHooks(cfg, repo, None, live), max_tournaments=1,
                            sleep=slept.append)
        self.assertEqual(slept[0], cfg["forever"]["rosterRetrySeconds"])
        self.assertEqual(len(seen[0]["players"]), 5, "the second check found everyone")

    def test_bench_kinds(self):
        self.assertEqual(forever.bench_kind('HTTP 429: {"error":{"code":"1113","message":"Insufficient balance"}}'),
                         "no subscription funds or plan")
        self.assertEqual(forever.bench_kind("Throttling.AllocationQuota: 5-hour allowance used"), "subscription limit")
        self.assertEqual(forever.bench_kind("claude error: Not logged in. Please run /login"), "login or key problem")
        self.assertEqual(forever.bench_kind("illegal move after 3 attempts"), "preflight failed")


class SmallTournamentTest(unittest.TestCase):
    def test_three_players_play_a_final_only_and_games_respect_the_board_cap(self):
        names = ["A", "B", "C"]
        cfg = dict(swiss.DEFAULTS, format="round-robin+knockout", knockoutSize=4, rounds=3, players=[{"name": n} for n in names])
        state = {"id": "t3", "title": "t3", "config": cfg, "players": cfg["players"], "seed_order": names, "rounds": [], "games": {}}
        for number in range(1, 4):
            pairings, bye = swiss.make_pairings(state, number)
            rnd = {"round": number, "bye": bye, "pairings": []}
            for board, (white, black) in enumerate(pairings, start=1):
                gid = f"r{number}b{board}"
                rnd["pairings"].append({"white": white, "black": black, "game_id": gid, "board": board})
                state["games"][gid] = {"result": "1-0" if white < black else "0-1", "end_kind": "board", "white": white, "black": black}
            state["rounds"].append(rnd)
        running, peak = [0], [0]
        lock = threading.Lock()

        def fake_play(game_id, white, black, cfg_, ts, live_pgn, log, replace_engine, start_ms=None):
            with lock:
                running[0] += 1
                peak[0] = max(peak[0], running[0])
            import time as _t
            _t.sleep(0.05)
            ts.state["games"][game_id].update(result="1-0", status="finished", end_kind="board")
            with lock:
                running[0] -= 1

        tmp = tmpdir(self)
        old = swiss._play_game, swiss.write_archive, swiss.GAME_SLOTS
        swiss._play_game, swiss.write_archive = fake_play, (lambda _s: None)
        swiss.GAME_SLOTS = threading.Semaphore(1)
        try:
            ts = swiss.TournamentState(state, tmp / "t3-tournament.json")
            ok = swiss.run_knockouts(state, ts, cfg, lambda n: n, lambda e: None, lambda _m: None)
            threads = [threading.Thread(target=swiss.play_game, args=(gid, None, None, cfg, ts, None, None, None))
                       for gid in list(state["games"])[:3]]
            for th in threads:
                th.start()
            for th in threads:
                th.join()
        finally:
            swiss._play_game, swiss.write_archive, swiss.GAME_SLOTS = old
        self.assertTrue(ok)
        ko = state["knockout"]
        self.assertEqual([m["id"] for m in ko["matches"]], ["final"])
        self.assertEqual(ko["champion"], ko["matches"][0]["a"])
        self.assertIsNone(ko["third"])
        self.assertEqual(peak[0], 1, "GAME_SLOTS caps the boards played at once")


class AnthropicRouteTest(unittest.TestCase):
    def client(self):
        os.environ.setdefault("ALIBABA_TOKEN_PLAN_API_KEY", "test-key")
        client = sp.SubscriptionChessClient("alibaba", lambda _m: None)
        client.model, client.board_image, client.conversation = "deepseek-v4.1-flash", False, True
        client.context = dict(CONTEXT)
        return client

    def test_payload_cache_breakpoints_thinking_and_session_prefix(self):
        client = self.client()
        seen = []
        replies = iter(['{"move": "Nf3"}', '{"move": "Bb5"}'])

        def stream(url, payload, headers, timeout, cutoff):
            seen.append((url, json.loads(json.dumps(payload)), headers))
            return {"content": next(replies), "reasoning": "x", "cut": False, "finish": "end_turn",
                    "usage": {"input_tokens": 69, "cache_read_input_tokens": 1920, "cache_creation_input_tokens": 0, "output_tokens": 900}}

        client.http_stream = stream
        h1 = ["e2e4", "e7e5"]
        board = chess.Board()
        for m in h1:
            board.push_uci(m)
        client.choose_move(board, GO, h1)
        h2 = h1 + ["g1f3", "b8c6"]
        for m in h2[2:]:
            board.push_uci(m)
        client.choose_move(board, GO, h2)
        (url, first, headers), (_u, second, _h) = seen
        self.assertTrue(url.endswith("/apps/anthropic/v1/messages"))
        self.assertEqual(headers["anthropic-version"], "2023-06-01")
        self.assertTrue(headers["Authorization"].startswith("Bearer "))
        self.assertEqual(first["system"][0]["text"], sp.SYSTEM_PROMPT)
        self.assertIn("cache_control", first["system"][0])
        self.assertEqual(first["thinking"], {"type": "enabled", "budget_tokens": 16000})
        self.assertEqual(first["max_tokens"], sp.HTTP_MAX_TOKENS)
        self.assertTrue(first["stream"])
        self.assertNotIn("stream_options", first)
        self.assertNotIn("system", [m["role"] for m in first["messages"]])
        self.assertTrue(first["messages"][0]["content"][0]["text"].startswith(sp.RULES_TEXT))
        self.assertIn("cache_control", first["messages"][-1]["content"][-1])
        # The second request starts with the first one's text, then the reply, then the new turn (breakpoint last).
        self.assertEqual(second["messages"][0]["content"], first["messages"][0]["content"][0]["text"])
        self.assertEqual(second["messages"][1], {"role": "assistant", "content": '{"move": "Nf3"}'})
        self.assertIn("cache_control", second["messages"][2]["content"][-1])
        self.assertEqual(client.last_report["usage"], {"calls": 1, "input": 1989, "cached": 1920, "output": 900})

    def test_sse_stream_parsing_and_max_tokens_as_length(self):
        from http.server import BaseHTTPRequestHandler

        events = [{"type": "message_start", "message": {"usage": {"input_tokens": 69, "cache_read_input_tokens": 1920}}},
                  {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "Hmm, c5."}},
                  {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": '{"move": "c5"}'}},
                  {"type": "message_delta", "delta": {"stop_reason": "max_tokens"}, "usage": {"output_tokens": 950}},
                  {"type": "message_stop"}]

        class SSE(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for event in events:
                    self.wfile.write(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode())

            def log_message(self, *a):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), SSE)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        out = self.client()._http_stream(f"http://127.0.0.1:{server.server_address[1]}/", {"stream": True}, {}, 10, None)
        self.assertEqual(out["content"], '{"move": "c5"}')
        self.assertEqual(out["reasoning"], "Hmm, c5.")
        self.assertEqual(out["finish"], "length")
        self.assertEqual(sp.http_usage(out["usage"]), {"input": 1989, "cached": 1920, "output": 950})

    def test_alibaba_errors(self):
        self.assertTrue(sp.provider_unavailable("HTTP 403: AccessDenied.Unpurchased"))
        self.assertTrue(sp.limit_error("HTTP 429: Throttling.AllocationQuota"))
        epoch = sp.limit_reset_epoch('{"code":"Throttling.AllocationQuota","reset_at":"2026-10-08T15:00:00Z"}')
        import datetime as dt
        self.assertEqual(epoch, dt.datetime(2026, 10, 8, 15, 0, tzinfo=dt.timezone.utc).timestamp())


if __name__ == "__main__":
    unittest.main()
