"""Harness version (owner 2026-10-16: "show which games ran on which version; it must be bumped when it changes").

Every game records the runner's version and the version of each AI player's engine process (an engine process
can be older than the runner: processes are reused across games). The PGN headers carry HarnessVersion,
WhiteEngineVersion and BlackEngineVersion; the tournament state carries `harness_version` and each game's
`harness`. Games before version 1 have none of these.

SOURCES_SHA256 fingerprints the files below. tests/test_harness_version.py fails when one of them changes
without a bump. Bump with: python tools/bump_harness_version.py
"""
from __future__ import annotations

import hashlib
from pathlib import Path

HARNESS_VERSION = 5
SOURCES_SHA256 = "60447997c7a43fc4a067e4e97dabe72773f09e8b1ef9cd8cb23f660aec3914ee"

ROOT = Path(__file__).resolve().parents[2]
# Everything that decides how a game is played: the engine, the runner, its settings and its launcher.
SOURCES = (
    "configs/ai-chess-vps.json",
    "engines/llm-chess-engine/llm_chess_uci.py",
    "engines/llm-chess-engine/provider_wire_audit.py",
    "engines/llm-chess-engine/subscription_providers.py",
    "run-ai-chess-forever.sh",
    "tools/ai_chess_forever.py",
    "tools/ai_chess_memory.py",
    "tools/disk_guard.py",
    "tools/play_llm_series.py",
    "tools/play_llm_swiss.py",
)


def sources_sha256(root: Path = ROOT) -> str:
    """One hash over the harness sources. Line endings are normalised: a Windows checkout matches the server."""
    digest = hashlib.sha256()
    for rel in SOURCES:
        data = (root / rel).read_bytes().replace(b"\r\n", b"\n")
        digest.update(rel.encode("utf-8") + b"\0" + hashlib.sha256(data).digest())
    return digest.hexdigest()


def engine_version(id_name: str) -> int | None:
    """The version from an engine's UCI `id name llm-chess-engine v<N>` line (None for other engines)."""
    words = id_name.split()
    if "llm-chess-engine" in words and words[-1].startswith("v") and words[-1][1:].isdigit():
        return int(words[-1][1:])
    return None
