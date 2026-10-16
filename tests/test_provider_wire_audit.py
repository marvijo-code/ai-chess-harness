import base64
import io
import json
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engines/llm-chess-engine"))
from provider_wire_audit import ProviderWireAudit, RAW_LIMIT
import subscription_providers as sp


def test_exact_request_and_raw_response_are_captured_without_headers(tmp_path):
    now = [0.0]
    payload = {"model": "deepseek-v4.1-flash", "messages": [{"role": "user", "content": "Real move prompt"}]}
    audit = ProviderWireAudit("https://example.invalid/chat", payload, tmp_path / "thinking.txt", lambda _: None, lambda: now[0])
    raw = b'data: {"choices":[{"delta":{"reasoning_content":"What about a move?"}}]}\n\n'
    audit.raw_event(raw)
    audit.reasoning("I will compare the same possible move again carefully. " * 40)
    saved = json.loads(audit.path.read_text())
    assert saved["request"] == payload
    assert base64.b64decode(saved["raw_sse_tail_base64"]) == raw
    assert "headers" not in saved
    audit.finish({"finish": "length", "usage": {"completion_tokens": 384000}})
    assert json.loads(audit.path.read_text())["usage"]["completion_tokens"] == 384000


def test_normal_reply_creates_no_evidence_file_and_buffer_is_bounded(tmp_path):
    audit = ProviderWireAudit("https://example.invalid/chat", {}, tmp_path / "thinking.txt", lambda _: None)
    for _ in range(20):
        audit.raw_event(b"x" * 100000)
    assert audit.raw_bytes <= RAW_LIMIT
    audit.reasoning("Compare legal moves and choose one.")
    audit.finish({"finish": "stop"})
    assert not audit.path.exists()


def test_observation_does_not_cut_or_change_a_repeating_stream(tmp_path):
    phrase = "I will compare the same possible move again carefully. " * 40
    raw = ("data: " + json.dumps({"choices": [{"delta": {"reasoning_content": phrase}}]}) + "\n\n"
           + 'data: {"choices":[{"delta":{"content":"{\\\"move\\\":\\\"g6\\\"}"},"finish_reason":"stop"}]}\n\n'
           + 'data: {"usage":{"completion_tokens":500},"choices":[]}\n\ndata: [DONE]\n\n').encode()
    client = sp.SubscriptionChessClient("opencode-go", lambda _: None)
    client.thinking_file = tmp_path / "thinking.txt"
    with patch.object(sp.urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(raw)):
        result = client._http_stream("https://example.invalid", {"model": "deepseek-v4.1-flash"},
                                     {"Authorization": "secret-only-in-memory"}, 30, None)
    client._think_close()
    assert result["reasoning"] == phrase
    assert result["content"] == '{"move":"g6"}'
    assert result["cut"] is False
    assert client.thinking_file.read_text() == phrase
    saved = Path(str(client.thinking_file) + ".provider-wire.json").read_text()
    assert "secret-only-in-memory" not in saved
    assert json.loads(saved)["finish"] == "stop"


def test_audit_failure_does_not_break_the_provider_reply(tmp_path):
    raw = b'data: {"choices":[{"delta":{"reasoning_content":"Thinking","content":"answer"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
    client = sp.SubscriptionChessClient("opencode-go", lambda _: None)
    client.thinking_file = tmp_path / "thinking.txt"
    with patch.object(sp.urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(raw)), \
         patch.object(ProviderWireAudit, "reasoning", side_effect=OSError("diagnostic storage failed")):
        result = client._http_stream("https://example.invalid", {}, {}, 30, None)
    client._think_close()
    assert result["reasoning"] == "Thinking"
    assert result["content"] == "answer"
    assert result["cut"] is False
