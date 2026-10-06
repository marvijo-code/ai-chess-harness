"""Write the public AI chess page for marvijo.com/ai-chess from the local viewer's PAGE.

    python tools/aichess_export_page.py [--out frontend/public/ai-chess/index.html] [--api-base /api/aichess]

The hosted page is the same page as the local viewer (tools/llm_tournament_viewer.py) with two flags
set before its script: every request goes to <api-base>/api/... and hosted mode is on (read only, no
board tours, a freshness banner). It also gets a real title, a description, Open Graph tags and a
favicon. The output is deterministic: the same viewer code always gives the same bytes.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import llm_tournament_viewer as viewer  # noqa: E402

TITLE = "AI Chess Tournament - marvijo.com"
DESCRIPTION = ("Watch AI models play a chess tournament live: every board, the clocks, the standings, "
               "the bracket and each model's thinking, with spoken commentary.")
FAVICON = "/favicon.svg"
DEFAULT_API_BASE = "/api/aichess"
LOCAL_TITLE = "<title>AI Chess Swiss</title>"
SCRIPT_TAG = "\n<script>\n"


def build_page(api_base: str = DEFAULT_API_BASE, page: str | None = None) -> str:
    page = viewer.PAGE if page is None else page
    if page.count(LOCAL_TITLE) != 1 or SCRIPT_TAG not in page:
        raise ValueError("the viewer page changed shape: <title> or the main <script> was not found")
    api_base = api_base.rstrip("/")
    head = "\n".join([
        f"<title>{html.escape(TITLE)}</title>",
        f'<meta name="description" content="{html.escape(DESCRIPTION)}">',
        f'<meta property="og:title" content="{html.escape(TITLE)}">',
        f'<meta property="og:description" content="{html.escape(DESCRIPTION)}">',
        f'<link rel="icon" href="{html.escape(FAVICON)}" type="image/svg+xml">',
    ])
    flags = (f"\n<script>window.AICHESS_API_BASE={json.dumps(api_base)}; window.AICHESS_HOSTED=true;</script>")
    out = page.replace(LOCAL_TITLE, head, 1)
    at = out.index(SCRIPT_TAG)
    return out[:at] + flags + out[at:]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, help="file to write (default: stdout)")
    parser.add_argument("--api-base", default=DEFAULT_API_BASE, help="prefix for every /api/... request")
    args = parser.parse_args(argv)
    text = build_page(args.api_base)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        print(f"wrote {args.out} ({len(text.encode('utf-8'))} bytes)", file=sys.stderr)
    else:
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
