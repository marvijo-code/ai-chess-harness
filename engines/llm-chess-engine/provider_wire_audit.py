"""Bounded evidence for repeated provider output. This never controls model generation."""
from __future__ import annotations

import base64
from collections import Counter, deque
import hashlib
import json
import os
from pathlib import Path
import re
import time

RAW_LIMIT = 512 * 1024
REQUEST_LIMIT = 1024 * 1024


class ProviderWireAudit:
    def __init__(self, url: str, payload: dict, thinking_file: Path, logger, clock=time.monotonic, session_id=None):
        self.path = Path(str(thinking_file) + ".provider-wire.json")
        body = json.dumps(payload).encode("utf-8")
        self.request = payload if len(body) <= REQUEST_LIMIT else None
        self.request_sha = hashlib.sha256(body).hexdigest()
        self.request_bytes = len(body)
        self.url = url.split("?", 1)[0]
        self.session_id = session_id
        self.logger, self.clock = logger, clock
        self.raw, self.raw_bytes, self.received_bytes = deque(), 0, 0
        self.tail, self.reasoning_chars = "", 0
        self.repeats = 0
        self.detected = False
        self.last_check = float("-inf")
        self.last_save = float("-inf")

    def raw_event(self, raw: bytes) -> None:
        self.received_bytes += len(raw)
        piece = raw[-RAW_LIMIT:]
        self.raw.append(piece)
        self.raw_bytes += len(piece)
        while self.raw_bytes > RAW_LIMIT and len(self.raw) > 1:
            self.raw_bytes -= len(self.raw.popleft())

    def reasoning(self, piece: str) -> None:
        self.reasoning_chars += len(piece)
        self.tail = (self.tail + piece)[-8192:]
        now = self.clock()
        if now - self.last_check < 1:
            return
        self.last_check = now
        words = re.findall(r"\S+", self.tail)
        phrases = Counter(tuple(words[i:i + 8]) for i in range(max(0, len(words) - 7)))
        self.repeats = max(phrases.values(), default=0)
        if self.repeats >= 20:
            self.detected = True
        if self.detected and now - self.last_save >= 10:
            self.save("repetition observed")

    def finish(self, state: dict) -> None:
        if self.detected:
            self.save("stream ended", finish=state.get("finish"), usage=state.get("usage"))

    def save(self, status: str, **extra) -> None:
        self.last_save = self.clock()
        record = {"status": status, "captured_epoch": time.time(), "url": self.url,
                  "client_session_id": self.session_id,
                  "request": self.request, "request_sha256": self.request_sha,
                  "request_bytes": self.request_bytes, "request_omitted_for_size": self.request is None,
                  "received_raw_bytes": self.received_bytes, "retained_raw_bytes": self.raw_bytes,
                  "raw_tail_is_partial": self.received_bytes > self.raw_bytes,
                  "raw_sse_tail_base64": base64.b64encode(b"".join(self.raw)).decode("ascii"),
                  "reasoning_chars": self.reasoning_chars, "repeated_eight_word_phrase_count": self.repeats,
                  **extra}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # The request and provider output are private diagnostics. No headers are accepted or stored.
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(record, handle, ensure_ascii=True)
        except (OSError, TypeError, ValueError) as exc:
            self.logger(f"provider wire evidence write failed: {type(exc).__name__}")
