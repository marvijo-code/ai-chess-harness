"""Loop guard (owner 2026-10-16): a reasoning stream that repeats one line is stopped, and the model is asked for
its move from its thinking up to the repetition. DeepSeek V4.1 Flash repeated one Bxh7 line for 384,000 tokens."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import threading
import time

import chess
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engines/llm-chess-engine"))
import subscription_providers as sp

LOOP = "What about 10.Bxh7+? Kxh7. What about 10.Qh5? g6. "
PREFIX = "The queen on g4 hits g7. Castling walks into Bxh7+. I should cover g7 before anything else. "
GO = {"wtime": 3_600_000, "btime": 3_600_000, "winc": 10_000, "binc": 10_000}


def feed_until_loop(text: str, step: int = 37):
    guard = sp.LoopGuard()
    for at in range(0, len(text), step):
        phrase = guard.feed(text[at:at + step])
        if phrase:
            return at + step, phrase
    return None, None


def test_guard_stops_the_historical_loop_early():
    text = PREFIX + LOOP * 2000
    at, phrase = feed_until_loop(text)
    assert phrase is not None and "Bxh7+?" in phrase
    assert at < len(PREFIX) + 3000, "stopped within about 60 repeats, not 384,000 tokens"


def test_guard_leaves_varied_candidate_analysis_alone():
    lines = []
    for n, (move, reply) in enumerate([("Nf6", "Bg5"), ("Be7", "Qg4"), ("g6", "h4"), ("Kf8", "Bd3"),
                                       ("Qc7", "Nb5"), ("Rb8", "O-O"), ("d5", "exd6"), ("h6", "Bh7")] * 25):
        lines.append(f"What about {9 + n % 7}...{move}? Then {10 + n % 5}.{reply} and I must check line {n}. ")
    at, phrase = feed_until_loop("".join(lines))
    assert phrase is None


class Looping(BaseHTTPRequestHandler):
    disconnected = threading.Event()
    repeats = 0  # 0 = forever

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            self.event({"reasoning_content": PREFIX})
            n = 0
            while not self.repeats or n < self.repeats:
                self.event({"reasoning_content": LOOP})
                n += 1
                time.sleep(0.002)
            self.event({"content": '{"move": "g6"}'}, "stop")
            self.wfile.write(b"data: [DONE]\n\n")
        except OSError:
            Looping.disconnected.set()

    def event(self, delta, finish=None):
        self.wfile.write(f"data: {json.dumps({'choices': [{'delta': delta, 'finish_reason': finish}]})}\n\n".encode())
        self.wfile.flush()

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    Looping.disconnected.clear()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Looping)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/chat"
    httpd.shutdown()
    httpd.server_close()


def test_a_looping_stream_is_stopped_and_disconnected(server, tmp_path):
    Looping.repeats = 0
    client = sp.SubscriptionChessClient("opencode-go", lambda _: None)
    client.thinking_file = tmp_path / "ply18.thinking.txt"
    started = time.monotonic()
    result = client._http_stream(server, {"model": "deepseek-v4.1-flash"}, {}, 60, None)
    assert time.monotonic() - started < 10
    assert result["cut"] is True and "Bxh7+?" in result["loop"]
    assert result["content"] == ""
    assert Looping.disconnected.wait(3), "the provider sees the disconnect and stops generating"
    client._think_close()
    assert len(client.thinking_file.read_text()) < 20_000


def test_the_guard_can_be_switched_off(server, tmp_path, monkeypatch):
    Looping.repeats = 300
    monkeypatch.setenv("LLM_LOOP_GUARD", "0")
    client = sp.SubscriptionChessClient("opencode-go", lambda _: None)
    client.thinking_file = tmp_path / "ply18.thinking.txt"
    result = client._http_stream(server, {"model": "deepseek-v4.1-flash"}, {}, 60, None)
    client._think_close()
    assert result["cut"] is False and "loop" not in result
    assert result["content"] == '{"move": "g6"}'


def test_the_move_is_asked_from_the_thinking_before_the_repeats(monkeypatch):
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "test-key")
    client = sp.SubscriptionChessClient("opencode-go", lambda _: None)
    client.model, client.board_image, client.conversation = "deepseek-v4.1-flash", False, True
    client.context = {"memory": "", "header": "Test game."}
    client.move_cap, client.keep_reasoning = False, True
    calls = []
    looped = {"content": "", "reasoning": PREFIX + LOOP * 400, "cut": True, "loop": " ".join(LOOP.split()[:8]),
              "finish": None, "usage": {"prompt_tokens": 1000, "completion_tokens": 9000}}
    answer = {"content": '{"move": "Nf3"}', "reasoning": "Nf3.", "cut": False, "finish": "stop",
              "usage": {"prompt_tokens": 1200, "completion_tokens": 20}}
    replies = iter([looped, answer])

    def stream(url, payload, headers, timeout, cutoff):
        calls.append(json.loads(json.dumps(payload)))
        return next(replies)

    client.http_stream = stream
    board = chess.Board()
    for uci in ("e2e4", "e7e5"):
        board.push_uci(uci)
    move, _ = client.choose_move(board, GO, ["e2e4", "e7e5"])
    assert move == "g1f3"
    returned = calls[1]["messages"][-2]["content"]
    assert PREFIX.strip() in returned
    assert returned.count("Bxh7+?") == 1, "one copy of the repeated line, not 400"
    assert "repeats are left out" in returned
    assert "kept repeating the same line" in calls[1]["messages"][-1]["content"]
    assert calls[1]["max_tokens"] == sp.ANSWER_MAX_TOKENS
    assert client._last_reasoning.count("Bxh7+?") <= 2, "the next turn does not carry the loop forward"
    assert client.last_report.get("loops") == 1


def scripted_client(monkeypatch, replies):
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "test-key")
    client = sp.SubscriptionChessClient("opencode-go", lambda _: None)
    client.model, client.board_image, client.conversation = "deepseek-v4.1-flash", False, True
    client.context = {"memory": "", "header": "Test game."}
    client.move_cap, client.keep_reasoning, client.max_attempts = False, True, 3
    replies = iter(replies)

    def stream(url, payload, headers, timeout, cutoff):
        content, reasoning = next(replies)
        return {"content": content, "reasoning": reasoning, "cut": False, "finish": "stop",
                "usage": {"prompt_tokens": 1000, "completion_tokens": 20}}

    client.http_stream = stream
    return client


def opening():
    board = chess.Board()
    for uci in ("e2e4", "e7e5"):
        board.push_uci(uci)
    return board


def test_rules_ask_every_player_for_best_so_far_lines():
    assert "BEST SO FAR: <move>" in sp.RULES_TEXT
    assert sp.latest_note("I like Nc3. BEST SO FAR: Nc3 ... no, BEST SO FAR: Nf3", opening()) == chess.Move.from_uci("g1f3")


def test_referee_plays_the_latest_best_so_far_instead_of_a_forfeit(monkeypatch):
    client = scripted_client(monkeypatch, [("not json", "BEST SO FAR: Nc3"), ("Qxf7", "BEST SO FAR: Nf3"),
                                           ("still not json", "Thinking again.")])
    move, comment = client.choose_move(opening(), GO, ["e2e4", "e7e5"])
    assert move == "g1f3"
    assert "BEST SO FAR" in comment
    assert client.last_report["referee"] == "best so far"


def test_no_best_so_far_line_is_still_a_forfeit_and_old_moves_never_count(monkeypatch):
    client = scripted_client(monkeypatch, [("not json", ""), ("not json", ""), ("not json", "")])
    client._last_reasoning = "BEST SO FAR: Nf3"  # left over from an earlier move
    move, _ = client.choose_move(opening(), GO, ["e2e4", "e7e5"])
    assert move == "0000"
