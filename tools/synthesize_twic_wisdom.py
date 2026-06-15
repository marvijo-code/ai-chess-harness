"""Render general winning wisdom (no individual games) from TWIC batch progress."""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "out" / "twic-manual-wisdom"
PROGRESS_PATH = OUT_DIR / "batch-progress.json"
STATE_PATH = OUT_DIR / "state.json"
DOC_PATH = ROOT / "docs" / "manual-wisdom-master-games.md"
SOURCES_MD = ROOT / "docs" / "manual-wisdom-sources.md"
SOURCES_JSON = ROOT / "docs" / "manual-wisdom-sources.json"
LEGACY_LEDGER = ROOT / "docs" / "manual-wisdom-twic-ledger.md"
TWIC_PORTAL = "https://theweekinchess.com/twic"
TWIC_ZIP_URL = "https://theweekinchess.com/zips/twic{issue}g.zip"

THEME_LESSONS: dict[str, str] = {
    "king_attack": "Checks force defensive concessions; conversion follows when the king loses shelter.",
    "king_activation": "In simplified positions, the winning king centralizes before collecting.",
    "pawn_break": "Pawn breaks apply concession pressure when they open lines faster than defenders redeploy.",
    "passed_pawn": "Passed pawns convert when counterplay is denied first.",
    "queen_trade_ahead": "Trading queens while ahead lowers risk — convert to a technical endgame.",
    "development_edge": "Restriction first: lead in development forces the opponent into passive defense.",
    "castled_first": "Safe king shelter lets you apply concession pressure on the other wing.",
    "seventh_rank": "Seventh-rank infiltration wins when the defender is overloaded.",
    "back_rank": "Back-rank pressure converts when the enemy king lacks escape squares.",
    "material_swing": "A forcing sequence gained material because normal replies conceded.",
}

THEME_ORDER = [
    "pawn_break",
    "material_swing",
    "passed_pawn",
    "king_attack",
    "seventh_rank",
    "development_edge",
    "queen_trade_ahead",
    "king_activation",
    "back_rank",
    "castled_first",
]

PROMOTED = [
    {
        "rank": 1,
        "principle": "Normal moves become concessions",
        "command": "What does the opponent lose by defending normally?",
        "weight": "bedrock",
        "themes": ["king_attack", "material_swing"],
        "weight_note": "Underlying lens — implicated in almost every forcing win",
    },
    {
        "rank": 3,
        "principle": "Deny counterplay before collecting",
        "command": "List checks, breaks, trades before grabbing material",
        "weight": "bedrock",
        "themes": ["king_attack", "passed_pawn"],
        "weight_note": "King pressure + passers co-occur in ~half of wins",
    },
    {
        "rank": 2,
        "principle": "One weakness is not enough",
        "command": "Fix one, then create or switch to a second target",
        "weight": "beam",
        "themes": ["seventh_rank", "development_edge"],
        "weight_note": "Infiltration and restriction show up in ~3–4 wins in ten",
    },
    {
        "rank": 5,
        "principle": "Restriction before tactics",
        "command": "Narrow piece scope; the tactic is the receipt, not the plan",
        "weight": "beam",
        "themes": ["development_edge", "seventh_rank"],
        "weight_note": "Development edge tags ~28% of decisive wins",
    },
    {
        "rank": 4,
        "principle": "Conversion = lower risk, not just more material",
        "command": "Trade when opponent counterplay dies faster than yours grows",
        "weight": "timber",
        "themes": ["queen_trade_ahead"],
        "weight_note": "Queen trades while ahead — ~1 win in 5 when already winning",
    },
    {
        "rank": 6,
        "principle": "Technique is part of the line",
        "command": "After forcing play, name the endgame or mating net",
        "weight": "timber",
        "themes": ["queen_trade_ahead", "king_activation"],
        "weight_note": "Name the receipt before you cash — discipline, not a tactic label",
    },
    {
        "rank": 7,
        "principle": "Activate the king before collecting",
        "command": "In simplified positions, king moves are part of the winning line",
        "weight": "key",
        "themes": ["king_activation"],
        "weight_note": "Endgame-only — ~18% of wins, mandatory once simplified",
    },
]

WEIGHT_TIERS = [
    (
        "bedrock",
        "Bedrock",
        "Ask every move — skip these and the rest is guessing",
        "●●●",
    ),
    (
        "beam",
        "Beam",
        "Load-bearing in ~3–5 wins in ten — ask before tactics",
        "●●○",
    ),
    (
        "timber",
        "Timber",
        "Decisive when ahead — about one win in four",
        "●○○",
    ),
    (
        "key",
        "Key",
        "Light until simplified — then non-negotiable",
        "🔑",
    ),
]

NOISE_THEMES = {"pawn_break", "material_swing"}

CANDIDATES = [
    (
        "Second-rank infiltration with dual passers",
        "Occupy the second rank while distant passers advance; deny king counterplay before pushing.",
    ),
    (
        "King-file alignment after a central wedge",
        "A central pawn wedge wins when the enemy king shares a file with your heavy pieces.",
    ),
    (
        "Seventh rank then pawn roller",
        "Bind on the seventh rank, then roll a passer — checks force concessions, not the goal.",
    ),
]


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def pct(n: int, d: int) -> str:
    return f"{100.0 * n / d:.1f}%" if d else "0%"


def aggregate_themes_from_git() -> Counter[str]:
    import subprocess

    try:
        text = subprocess.check_output(
            ["git", "show", "HEAD:docs/manual-wisdom-twic-ledger.md"],
            cwd=ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return Counter()
    return aggregate_themes_from_text(text)


def aggregate_themes_from_text(text: str) -> Counter[str]:
    themes: Counter[str] = Counter()
    for match in re.finditer(r"`([a-z_]+)`[^0-9]+([0-9,]+) \(", text):
        themes[match.group(1)] += int(match.group(2).replace(",", ""))
    return themes


def aggregate_themes_from_ledger(path: Path) -> Counter[str]:
    if not path.exists():
        return Counter()
    return aggregate_themes_from_text(path.read_text(encoding="utf-8"))


def load_progress() -> dict:
    if not PROGRESS_PATH.exists():
        return {}
    return json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))


def load_issue_catalog() -> dict[int, dict]:
    if not STATE_PATH.exists():
        return {}
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {row["issue"]: row for row in state.get("issues", [])}


def load_state_meta(completed: list[int] | None = None) -> tuple[int, int, str, str]:
    catalog = load_issue_catalog()
    issues = completed or sorted(catalog.keys(), reverse=True)
    if not issues:
        return 1647, 1573, "?", "2025-01-01"
    meta_rows = [catalog[i] for i in issues if i in catalog]
    if not meta_rows:
        return max(issues), min(issues), "?", "2025-01-01"
    earliest = min((row["earliest_date"] for row in meta_rows if row.get("earliest_date")), default="?")
    latest = max((row["latest_date"] for row in meta_rows if row.get("latest_date")), default="?")
    span = f"{earliest} → {latest}"
    until = "2025-01-01"
    if STATE_PATH.exists():
        until = json.loads(STATE_PATH.read_text(encoding="utf-8")).get("until_date", until)
    return max(issues), min(issues), span, until


def twic_zip_name(issue: int) -> str:
    return f"twic{issue}g.zip"


def twic_zip_url(issue: int) -> str:
    return TWIC_ZIP_URL.format(issue=issue)


def build_source_rows(progress: dict) -> list[dict]:
    catalog = load_issue_catalog()
    rows: list[dict] = []
    for issue in sorted(progress.get("completed_issues", []), reverse=True):
        meta = catalog.get(issue, {})
        zip_name = meta.get("zip_name") or twic_zip_name(issue)
        rows.append(
            {
                "issue": issue,
                "zip": zip_name,
                "url": twic_zip_url(issue),
                "earliest_date": meta.get("earliest_date"),
                "latest_date": meta.get("latest_date"),
                "game_count": meta.get("game_count"),
                "decisive_count": meta.get("decisive_count"),
            }
        )
    return rows


def build_sources_payload(progress: dict) -> dict:
    rows = build_source_rows(progress)
    completed = progress.get("completed_issues", [])
    newest = max(completed) if completed else None
    oldest = min(completed) if completed else None
    _, _, date_span, _ = load_state_meta(completed)
    return {
        "updated_at": progress.get("updated_at", utc_now()),
        "provider": "The Week in Chess",
        "provider_url": TWIC_PORTAL,
        "latest_zip": twic_zip_name(newest) if newest else None,
        "issue_count": len(rows),
        "issue_range": [newest, oldest] if newest and oldest else [],
        "decisive_games": progress.get("decisive_games", 0),
        "game_date_span": date_span,
        "issues": rows,
    }


def render_sources_md(progress: dict) -> str:
    payload = build_sources_payload(progress)
    rows = payload["issues"]
    lines = [
        "# TWIC sources — processed archives",
        "",
        f"Updated: {payload['updated_at']}",
        "",
        f"Provider: [{payload['provider']}]({payload['provider_url']})",
        "",
        "Every row below is a **processed** TWIC issue — downloaded, parsed, and included in the wisdom corpus.",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Latest zip | **{payload['latest_zip'] or '?'}** |",
        f"| Issues processed | **{payload['issue_count']}** |",
        f"| Issue range | {payload['issue_range'][0] if payload['issue_range'] else '?'} → {payload['issue_range'][-1] if payload['issue_range'] else '?'} |",
        f"| Decisive games | **{payload['decisive_games']:,}** |",
        f"| Game date span | {payload['game_date_span']} |",
        "",
        "## Processed issues (newest first)",
        "",
        "| TWIC | Archive | Date span | Decisive | URL |",
        "|---:|---|---|---:|---|",
    ]
    for row in rows:
        span = "?"
        if row.get("earliest_date") and row.get("latest_date"):
            span = f"{row['earliest_date']} → {row['latest_date']}"
        decisive = f"{row['decisive_count']:,}" if row.get("decisive_count") is not None else "?"
        lines.append(
            f"| {row['issue']} | `{row['zip']}` | {span} | {decisive} | [download]({row['url']}) |"
        )
    lines.extend(
        [
            "",
            "Machine-readable mirror: [manual-wisdom-sources.json](manual-wisdom-sources.json)",
            "",
            "Re-render: `python tools/synthesize_twic_wisdom.py`",
            "",
        ]
    )
    return "\n".join(lines)


def render_sources_summary(progress: dict) -> list[str]:
    payload = build_sources_payload(progress)
    latest = payload["latest_zip"] or "?"
    return [
        "## Sources",
        "",
        f"Latest archive: **{latest}** · [{payload['provider']}]({payload['provider_url']})",
        "",
        f"**{payload['issue_count']} TWIC issues processed** "
        f"({payload['issue_range'][0] if payload['issue_range'] else '?'} → "
        f"{payload['issue_range'][-1] if payload['issue_range'] else '?'}) — "
        f"**{payload['decisive_games']:,}** decisive games · dates {payload['game_date_span']}.",
        "",
        "Full per-issue list: [manual-wisdom-sources.md](manual-wisdom-sources.md) · "
        "[manual-wisdom-sources.json](manual-wisdom-sources.json)",
        "",
        "---",
        "",
    ]


def reinforcement_rate(themes: list[str], theme_totals: Counter[str], decisive: int) -> float | None:
    if decisive <= 0 or not themes:
        return None
    usable = [theme_totals.get(t, 0) for t in themes if t not in NOISE_THEMES]
    if not usable:
        return None
    return sum(usable) / len(usable) / decisive


def weight_label(weight_id: str) -> str:
    for tier_id, label, _, dots in WEIGHT_TIERS:
        if tier_id == weight_id:
            return f"{label} {dots}"
    return weight_id


def render_weight_stack(theme_totals: Counter[str], decisive: int) -> list[str]:
    tier_order = ["bedrock", "beam", "timber", "key"]
    by_tier: dict[str, list[dict]] = {tier: [] for tier in tier_order}
    for row in PROMOTED:
        by_tier[row["weight"]].append(row)

    lines = [
        "",
        "## Remember the weights (the stack)",
        "",
        "Principles are **ordered by weight**, not by rank number. Heavier layers are questions you ask "
        "**before** lighter ones — not moves you play instead of them.",
        "",
        "**Pocket mnemonic — C-B-T-K:**",
        "",
        "| Letter | Layer | Means |",
        "|---|---|---|",
        "| **C** | Concession + Counterplay | What does normal cost? Is their counterplay dead before I grab? |",
        "| **B** | Bind + second target | Restrict scope; if they fixed one weakness, open another |",
        "| **T** | Trade down risk + Technique | Trade when their counterplay dies faster; name the endgame first |",
        "| **K** | King walk | In simplified positions, centralize the king before pushing |",
        "",
        "**Over the board:** run **C → B → T → K** top to bottom. If a heavier layer says *not yet*, "
        "a lighter principle does not override it.",
        "",
        "### The stack (heavy at the bottom — build on these)",
        "",
    ]

    for tier_id, label, blurb, dots in WEIGHT_TIERS:
        rows = sorted(by_tier[tier_id], key=lambda r: r["rank"])
        if not rows:
            continue
        lines.append(f"#### {label} {dots} — {blurb}")
        lines.append("")
        for row in rows:
            rate = reinforcement_rate(row["themes"], theme_totals, decisive)
            if rate is None:
                corpus = row["weight_note"]
            else:
                corpus = f"Corpus ~{rate * 100:.0f}% · {row['weight_note']}"
            lines.append(f"- **#{row['rank']} {row['principle']}** — {row['command']}  ")
            lines.append(f"  *{corpus}*")
        lines.append("")

    lines.extend(
        [
            "**One sentence to keep:** *Bedrock asks what normal costs; beam binds; timber names the receipt; "
            "the key turns only in the ending.*",
            "",
            "---",
            "",
        ]
    )
    return lines


def render(theme_totals: Counter[str], progress: dict) -> str:
    decisive = progress.get("decisive_games", 0)
    completed = progress.get("completed_issues", [])
    issue_count = len(completed)
    newest, oldest, date_span, until = load_state_meta(completed)
    updated = progress.get("updated_at", utc_now())
    latest_zip = twic_zip_name(newest) if completed else "?"

    lines = [
        "# chess-wisdom — winning principles from decisive master games",
        "",
        f"Updated: {updated}",
        "",
        f"Latest source: **{latest_zip}** · [manual-wisdom-sources.md](manual-wisdom-sources.md)",
        "",
        "Source prompt: [manual-wisdom-prompt.md](manual-wisdom-prompt.md)  ",
        "Checklist: [manual-wisdom-checklist.md](manual-wisdom-checklist.md)",
        "",
        "This document holds **portable wisdom only** — principles and corpus patterns that influence wins. "
        "It does not list individual games, players, or move sequences.",
        "",
        "---",
        "",
        "## Method",
        "",
        "| Layer | Role |",
        "|---|---|",
        "| **A — Board-verified study** | python-chess + FEN at phase boundaries; concession-pressure and conversion-delta questions |",
        "| **B — Corpus heuristics** | `tools/run_twic_wisdom_batch.py` tags themes across all decisive games; validates patterns, not move recipes |",
        "",
        "Layer A rule: step the board before structural claims — PGN text alone is not enough.",
        "",
        "---",
        "",
        "## Coverage",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| TWIC issues | **{issue_count}** ({newest} → {oldest}) |",
        f"| Latest archive | **{latest_zip}** |",
        f"| Decisive games analyzed | **{decisive:,}** |",
        f"| Game date span | {date_span} |",
        f"| Source list | [manual-wisdom-sources.md](manual-wisdom-sources.md) |",
        f"| Target cutoff | {until} |",
        f"| Batch runner | `python tools/run_twic_wisdom_batch.py` |",
        f"| Re-render wisdom | `python tools/synthesize_twic_wisdom.py` |",
        "",
        "---",
        "",
    ]
    lines.extend(render_sources_summary(progress))
    lines.extend(
        [
            "## Formula",
            "",
            "**Win = concession pressure × conversion delta × counterplay denial**",
            "",
            "- **Concession pressure:** normal replies cost shelter, tempo, coordination, or pawn structure.",
            "- **Conversion delta:** each transformation makes the advantage safer and more permanent.",
            "- **Counterplay denial:** checks, breaks, and trades are answered before material is collected.",
            "",
            "---",
            "",
            "## Principles (promoted)",
            "",
            "Rank is historical; **weight** is what matters at the board. See [the stack](#remember-the-weights-the-stack).",
            "",
            "| # | Weight | Principle | Command |",
            "|---|---|---|---|",
        ]
    )
    for row in sorted(PROMOTED, key=lambda r: r["rank"]):
        lines.append(
            f"| {row['rank']} | {weight_label(row['weight'])} | {row['principle']} | {row['command']} |"
        )

    lines.extend(render_weight_stack(theme_totals, decisive))

    lines.extend(
        [
            "",
            "## Candidate principles (watch for repeats)",
            "",
            "| Principle | Command |",
            "|---|---|",
        ]
    )
    for principle, command in CANDIDATES:
        lines.append(f"| {principle} | {command} |")

    lines.extend(
        [
            "",
            "Promote a candidate into the ranked table only after it survives two separate event types.",
            "",
            "---",
            "",
            f"## Corpus evidence ({decisive:,} decisive games)",
            "",
            "Theme tags are **non-exclusive** heuristics (a game may carry several). "
            "Rates show how often each winning pattern appeared in the corpus — not a single cause label.",
            "",
            "| Theme | Tagged games | Rate | Wisdom |",
            "|---|---:|---:|---|",
        ]
    )
    ordered = sorted(
        theme_totals.items(),
        key=lambda kv: (-kv[1], THEME_ORDER.index(kv[0]) if kv[0] in THEME_ORDER else 99),
    )
    for theme, count in ordered:
        if theme not in THEME_LESSONS:
            continue
        lines.append(
            f"| `{theme}` | {count:,} | {pct(count, decisive)} | {THEME_LESSONS[theme]} |"
        )

    lines.extend(
        [
            "",
            "**Reading the table:** `pawn_break` and `material_swing` rates are high because the heuristics "
            "fire on almost any forcing win — treat them as background noise. Focus on combinations that "
            "reinforce the promoted principles (king activation, queen trades while ahead, seventh-rank entry).",
            "",
            "---",
            "",
            "## Study questions (Layer A)",
            "",
            "1. **Concession pressure:** What did the loser lose by playing normal moves?",
            "2. **Conversion-delta:** How did each winner move make the advantage safer or more permanent?",
            "3. **Counterplay:** What opponent checks/captures/breaks were denied before material was taken?",
            "4. **Horizon:** Was there a quiet setup move depth-limited engines might undervalue?",
            "5. **Portable lesson:** One sentence a human could apply without memorizing a line?",
            "",
            "Board helper: `python tools/twic_game_board.py <pgn> --phases`",
            "",
        ]
    )
    return "\n".join(lines)


def write_wisdom_artifacts(progress: dict, theme_totals: Counter[str]) -> None:
    DOC_PATH.write_text(render(theme_totals, progress), encoding="utf-8")
    SOURCES_MD.write_text(render_sources_md(progress), encoding="utf-8")
    SOURCES_JSON.write_text(json.dumps(build_sources_payload(progress), indent=2), encoding="utf-8")


def main() -> None:
    progress = load_progress()
    theme_totals: Counter[str] = Counter(progress.get("theme_totals", {}))
    if not theme_totals and LEGACY_LEDGER.exists():
        theme_totals = aggregate_themes_from_ledger(LEGACY_LEDGER)
    if not theme_totals:
        theme_totals = aggregate_themes_from_git()
        progress["theme_totals"] = dict(theme_totals)
        PROGRESS_PATH.parent.mkdir(parents=True, exist_ok=True)
        PROGRESS_PATH.write_text(json.dumps(progress, indent=2), encoding="utf-8")

    write_wisdom_artifacts(progress, theme_totals)
    print(
        json.dumps(
            {
                "written": str(DOC_PATH),
                "sources_md": str(SOURCES_MD),
                "sources_json": str(SOURCES_JSON),
                "issues": len(progress.get("completed_issues", [])),
                "themes": len(theme_totals),
                "decisive": progress.get("decisive_games", 0),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
