"""Free-disk check shared by the tournament runners and the viewer.

A full disk once stopped every state save of a running tournament (2026-10-12, Tournament #37: `No space
left on device` 155 times, two games lost). The runners ask this module before a tournament and before every
round; the viewer asks it for the "disk low" chip.

Levels: "ok", "low" (under `warn_mb`, default 3 GB: a warning), "critical" (under `refuse_mb`, default
500 MB: no new tournament starts), "unknown" (the disk could not be measured; never blocks anything).
"""

from __future__ import annotations

import errno
import shutil
import time
from pathlib import Path

DISK_WARN_MB = 3072
DISK_REFUSE_MB = 500
MB = 1024 * 1024

# Replaceable in tests: (path) -> an object with .total and .free in bytes.
USAGE = shutil.disk_usage


def _existing(path: Path) -> Path:
    """The path itself or its nearest folder that exists (a live folder may not be made yet)."""
    path = Path(path)
    for candidate in (path, *path.parents):
        if candidate.exists():
            return candidate
    return Path(".")


def disk_status(path: Path | str, warn_mb: float | None = None, refuse_mb: float | None = None, usage=None) -> dict:
    """{"level", "free_mb", "total_mb", "warn_mb", "refuse_mb", "path", "checked_epoch_ms"} for the disk that
    holds `path`. A limit of 0 switches that level off. Never raises."""
    warn = DISK_WARN_MB if warn_mb is None else float(warn_mb)
    refuse = DISK_REFUSE_MB if refuse_mb is None else float(refuse_mb)
    out = {"level": "unknown", "free_mb": None, "total_mb": None, "warn_mb": warn, "refuse_mb": refuse,
           "path": str(path), "checked_epoch_ms": int(time.time() * 1000)}
    try:
        seen = (usage or USAGE)(_existing(Path(path)))
        free, total = float(seen.free) / MB, float(seen.total) / MB
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"[:200]
        return out
    out.update(free_mb=round(free, 1), total_mb=round(total, 1))
    if refuse > 0 and free < refuse:
        out["level"] = "critical"
    elif warn > 0 and free < warn:
        out["level"] = "low"
    else:
        out["level"] = "ok"
    return out


def size_text(mb: float | None) -> str:
    """'410 MB' under 1 GB, '1.9 GB' from there."""
    if mb is None:
        return "an unknown amount"
    return f"{mb / 1024:.1f} GB" if mb >= 1024 else f"{mb:.0f} MB"


def public_status(status: dict) -> dict:
    """What a page may show: the level, free space rounded to 100 MB (so a pushed state does not change on
    every read) and the two limits. No path."""
    free = status.get("free_mb")
    return {"level": status.get("level", "unknown"),
            "free_mb": None if free is None else (round(free / 100) * 100 if free >= 1000 else round(free / 10) * 10),
            "warn_mb": status.get("warn_mb"), "refuse_mb": status.get("refuse_mb")}


def is_disk_full(error: BaseException) -> bool:
    return isinstance(error, OSError) and error.errno in (errno.ENOSPC, errno.EDQUOT)


def describe_error(error: BaseException) -> str:
    """One short phrase for a log line: a full disk is named as such."""
    if is_disk_full(error):
        return "the disk is full (No space left on device)"
    return f"{type(error).__name__}: {error}"[:200]
