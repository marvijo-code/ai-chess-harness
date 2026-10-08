#!/usr/bin/env python3
"""Tiny Chrome DevTools Protocol helper for the stream browser (Python standard library only).

The stream's Chrome runs with --remote-debugging-port on 127.0.0.1. This helper:

  cdp.py --port 9223 info                         # page URL and title (JSON)
  cdp.py --port 9223 seed '{"swissCommentary":"on"}'   # set localStorage keys, reload when any changed
  cdp.py --port 9223 reload                       # reload the page
  cdp.py --port 9223 eval 'document.title'        # evaluate an expression, print the JSON value
  cdp.py --port 9223 screenshot out.png           # PNG of the page as Chrome renders it

Exit 0 on success, 1 on failure (message on stderr). No secrets pass through here.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import struct
import sys
import urllib.request
from urllib.parse import urlparse


class WebSocket:
    """Minimal RFC 6455 client: text frames, client masking, no extensions."""

    def __init__(self, url: str, timeout: float = 15.0):
        u = urlparse(url)
        self.sock = socket.create_connection((u.hostname, u.port or 80), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET {u.path or '/'} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\nUpgrade: websocket\r\n"
               f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("websocket handshake: connection closed")
            head += chunk
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise ConnectionError("websocket handshake refused: " + head.split(b"\r\n", 1)[0].decode(errors="replace"))
        self.buf = head.split(b"\r\n\r\n", 1)[1]

    def _read(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("websocket closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, text: str) -> None:
        data = text.encode()
        head = bytearray([0x81])
        n = len(data)
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head.append(0x80 | 126)
            head += struct.pack(">H", n)
        else:
            head.append(0x80 | 127)
            head += struct.pack(">Q", n)
        mask = os.urandom(4)
        self.sock.sendall(bytes(head) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def recv(self) -> str:
        parts = []
        while True:
            b0, b1 = self._read(2)
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            payload = self._read(n)
            op = b0 & 0x0F
            if op == 0x8:
                raise ConnectionError("websocket closed by peer")
            if op in (0x9, 0xA):
                continue
            parts.append(payload)
            if b0 & 0x80:
                return b"".join(parts).decode("utf-8", "replace")

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class Page:
    def __init__(self, port: int):
        self.port = port
        targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5))
        pages = [t for t in targets if t.get("type") == "page"]
        if not pages:
            raise RuntimeError("no page target")
        self.target = pages[0]
        self.ws = WebSocket(self.target["webSocketDebuggerUrl"])
        self.next_id = 0

    def call(self, method: str, **params):
        self.next_id += 1
        mid = self.next_id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def eval(self, expr: str):
        r = self.call("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        if r.get("exceptionDetails"):
            raise RuntimeError(f"eval failed: {r['exceptionDetails'].get('text')}")
        return r.get("result", {}).get("value")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=int(os.environ.get("AICHESS_CDP_PORT", "9223")))
    ap.add_argument("command", choices=["info", "seed", "reload", "eval", "screenshot"])
    ap.add_argument("arg", nargs="?")
    args = ap.parse_args(argv)
    try:
        if args.command == "info":
            targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{args.port}/json/list", timeout=5))
            page = next((t for t in targets if t.get("type") == "page"), {})
            print(json.dumps({"url": page.get("url"), "title": page.get("title")}))
            return 0
        page = Page(args.port)
        if args.command == "seed":
            wanted = json.loads(args.arg or "{}")
            changed = page.eval(
                "(() => { const w = " + json.dumps(wanted) + "; let c = 0;"
                " for (const [k, v] of Object.entries(w)) { if (localStorage.getItem(k) !== v) { localStorage.setItem(k, v); c++; } }"
                " return c; })()")
            if changed:
                page.call("Page.reload", ignoreCache=True)
            print(json.dumps({"changed": changed}))
        elif args.command == "reload":
            page.call("Page.reload", ignoreCache=True)
            print("reloaded")
        elif args.command == "eval":
            print(json.dumps(page.eval(args.arg or "document.title")))
        elif args.command == "screenshot":
            r = page.call("Page.captureScreenshot", format="png")
            with open(args.arg or "page.png", "wb") as fh:
                fh.write(base64.b64decode(r["data"]))
            print(args.arg or "page.png")
        page.ws.close()
        return 0
    except Exception as exc:  # noqa: BLE001 - a helper for shell scripts: one line, exit 1
        print(f"cdp: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
