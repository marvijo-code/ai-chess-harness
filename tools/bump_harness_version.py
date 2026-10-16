"""Bump the harness version after a change to its sources (engines/llm-chess-engine/harness_version.py).

    python tools/bump_harness_version.py           # +1 and a new fingerprint
    python tools/bump_harness_version.py --check   # exit 1 when the sources changed without a bump
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engines" / "llm-chess-engine"))
import harness_version as hv  # noqa: E402

FILE = Path(hv.__file__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    current = hv.sources_sha256()
    if args.check:
        if current == hv.SOURCES_SHA256:
            print(f"harness v{hv.HARNESS_VERSION}: sources unchanged")
            return 0
        print(f"harness v{hv.HARNESS_VERSION}: sources changed; run python tools/bump_harness_version.py")
        return 1
    if current == hv.SOURCES_SHA256:
        print(f"harness v{hv.HARNESS_VERSION}: sources unchanged, nothing to bump")
        return 0
    first = hv.SOURCES_SHA256 == "pending"
    version = hv.HARNESS_VERSION if first else hv.HARNESS_VERSION + 1
    text = FILE.read_text(encoding="utf-8")
    text = re.sub(r"^HARNESS_VERSION = \d+$", f"HARNESS_VERSION = {version}", text, count=1, flags=re.M)
    text = re.sub(r'^SOURCES_SHA256 = "[^"]*"$', f'SOURCES_SHA256 = "{current}"', text, count=1, flags=re.M)
    FILE.write_text(text, encoding="utf-8", newline="")
    print(f"harness v{version}: fingerprint {current[:12]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
