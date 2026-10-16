"""A stream the engine stops listening to must stop: no more thinking written, no more tokens spent.

Tournament #5 (2026-10-08), DeepSeek ply 34: the move-cap cut returned after 22 s with 2,669 characters of
thinking, but the thinking file held 12,612 characters before its first marker, mixed word by word from two
streams, and a cut answer stream kept looping into it for about 100,000 characters more.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engines/llm-chess-engine"))
import subscription_providers as sp


class EndlessThinking(BaseHTTPRequestHandler):
    """Streams reasoning forever, like a provider that keeps generating until the client disconnects."""
    disconnected = threading.Event()
    header_delay = 0.0

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        time.sleep(self.header_delay)  # some gateways answer only once the first token exists
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            while True:
                event = {"choices": [{"delta": {"reasoning_content": "What about Bxh7+? Kxh7. "}}]}
                self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
                self.wfile.flush()
                time.sleep(0.02)
        except OSError:
            EndlessThinking.disconnected.set()

    def log_message(self, *args):
        pass


@pytest.mark.parametrize("header_delay", [0.0, 1.0], ids=["cut-while-streaming", "cut-before-headers"])
def test_cut_stream_stops_writing_and_disconnects(tmp_path, header_delay):
    EndlessThinking.disconnected.clear()
    EndlessThinking.header_delay = header_delay
    server = ThreadingHTTPServer(("127.0.0.1", 0), EndlessThinking)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        client = sp.SubscriptionChessClient("opencode-go", lambda _: None)
        client.thinking_file = tmp_path / "ply34.thinking.txt"
        url = f"http://127.0.0.1:{server.server_address[1]}/chat"
        result = client._http_stream(url, {"model": "deepseek-v4.1-flash"}, {}, 30, 0.5)
        assert result["cut"] is True
        client._think_close()
        old = tmp_path / "ply34.thinking.txt"
        cut_text = old.read_text() if old.exists() else ""
        assert cut_text == result["reasoning"]

        # The next move gets its own file. Nothing from the old stream may arrive in either file.
        client.thinking_file = tmp_path / "ply36.thinking.txt"
        time.sleep(1.5)
        client._think_close()
        assert (old.read_text() if old.exists() else "") == cut_text
        assert not client.thinking_file.exists()
        # The provider sees the disconnect, so it stops generating tokens nobody reads.
        assert EndlessThinking.disconnected.wait(3)
    finally:
        server.shutdown()
        server.server_close()
