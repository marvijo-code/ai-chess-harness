import argparse
import asyncio
import json
import shutil
import socket
import subprocess
import sys
import uuid
from pathlib import Path

import websockets

from harness_config import config_value


ROOT = Path(__file__).resolve().parents[1]


def resolve_codex_command() -> str:
    windows_cmd = Path.home() / "AppData" / "Roaming" / "npm" / "codex.cmd"
    if windows_cmd.exists():
        return str(windows_cmd)
    found = shutil.which("codex.cmd") or shutil.which("codex.exe") or shutil.which("codex")
    if found:
        return found
    raise FileNotFoundError("Could not find Codex CLI. Expected codex.cmd or codex on PATH.")


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def error_text(error: dict | None) -> tuple[str, str]:
    if not isinstance(error, dict):
        return "unknown Codex app-server error", ""
    return str(error.get("message") or error), str(error.get("codexErrorInfo") or error.get("code") or "")


async def request(ws, method: str, params: dict):
    request_id = str(uuid.uuid4())
    await ws.send(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}))
    while True:
        raw = await ws.recv()
        msg = json.loads(raw)
        if msg.get("id") != request_id:
            continue
        if "error" in msg:
            raise RuntimeError(json.dumps(msg["error"]))
        return msg.get("result")


async def check_model(model: str, effort: str, timeout: float) -> None:
    url = f"ws://127.0.0.1:{free_port()}"
    codex_cmd = resolve_codex_command()
    proc = subprocess.Popen(
        [codex_cmd, "app-server", "--listen", url],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    ws = None
    try:
        last_error = None
        for _ in range(80):
            try:
                ws = await websockets.connect(url)
                break
            except Exception as exc:
                last_error = exc
                await asyncio.sleep(0.25)
        if ws is None:
            raise RuntimeError(f"could not connect to Codex app-server: {last_error}")

        await request(
            ws,
            "initialize",
            {"clientInfo": {"name": "Codex chess model preflight", "version": "0.1.0"}, "capabilities": None},
        )
        thread = await request(
            ws,
            "thread/start",
            {
                "cwd": str(ROOT),
                "model": model,
                "approvalPolicy": "never",
                "sandbox": "read-only",
                "ephemeral": True,
                "baseInstructions": "Return only strict JSON.",
                "developerInstructions": "Return only JSON matching the schema. Do not call tools.",
            },
        )
        turn = await request(
            ws,
            "turn/start",
            {
                "threadId": thread["thread"]["id"],
                "effort": effort,
                "input": [{"type": "text", "text": '{"legal_moves":["e2e4"],"side_to_move":"white"}'}],
                "outputSchema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["uci", "comment"],
                    "properties": {"uci": {"type": "string"}, "comment": {"type": "string"}},
                },
            },
        )
        turn_id = turn["turn"]["id"]
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            msg = json.loads(raw)
            method = msg.get("method")
            params = msg.get("params", {})
            if method == "error" and params.get("turnId") == turn_id:
                message, code = error_text(params.get("error"))
                raise RuntimeError(f"{code or 'codexError'}: {message}")
            if method == "turn/completed" and params.get("turn", {}).get("id") == turn_id:
                completed = params["turn"]
                if completed.get("status") == "failed" or completed.get("error"):
                    message, code = error_text(completed.get("error"))
                    raise RuntimeError(f"{code or 'codexError'}: {message}")
                return
    finally:
        if ws is not None:
            await ws.close()
        proc.kill()


def main() -> int:
    parser = argparse.ArgumentParser(description="Fail fast when a Codex app-server model cannot answer a chess move.")
    parser.add_argument("--model", default=str(config_value("codex.model", "gpt-5.3-codex")))
    parser.add_argument("--effort", default=str(config_value("codex.effort", "low")))
    parser.add_argument("--timeout", type=float, default=float(config_value("codex.preflightTimeoutSeconds", 45)))
    args = parser.parse_args()
    try:
        asyncio.run(check_model(args.model, args.effort, args.timeout))
    except Exception as exc:
        print(f"Codex model preflight failed for {args.model}: {exc}", file=sys.stderr)
        return 1
    print(f"Codex model preflight ok: {args.model}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
