"""Render manual-wisdom-master-games.md from TWIC processing state."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "out" / "twic-manual-wisdom" / "state.json"
DOC_PATH = ROOT / "docs" / "manual-wisdom-master-games.md"


def pct(num: int, den: int) -> str:
    if den <= 0:
        return "0%"
    return f"{100.0 * num / den:.1f}%"


def render(state: dict) -> str:
    issues = state.get("issues", [])
    ledger = state.get("ledger", {})
    latest_issue = state.get("latest_issue", issues[0]["issue"] if issues else "?")
    until_date = state.get("until_date", "2025-01-01")
    decisive = ledger.get("decisive_games", 0)
    parsed = ledger.get("games_parsed", 0)
    draws = sum(i.get("draw_count", 0) for i in issues)
    downloaded = [i for i in issues if i.get("downloaded")]
    earliest = min((i["earliest_date"] for i in downloaded if i.get("earliest_date")), default="?")
    latest = max((i["latest_date"] for i in downloaded if i.get("latest_date")), default="?")
    zip_anchor = f"twic{max(i['issue'] for i in issues)}g.zip" if issues else "twic1647g.zip"

    phase = ledger.get("phase_counts", {})
    terms = ledger.get("termination_counts", {})
    events = ledger.get("event_samples", [])

    lines = [
        "# chess-wisdom — manual TWIC master-game research",
        "",
        f"Updated: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
        "",
        "Source prompt: [docs/manual-wisdom-prompt.md](manual-wisdom-prompt.md)",
        "",
        "## Coverage ledger",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Latest TWIC archive anchor | {zip_anchor} |",
        f"| Oldest issue processed | TWIC {min(i['issue'] for i in issues) if issues else '?'} |",
        f"| Newest issue processed | TWIC {max(i['issue'] for i in issues) if issues else '?'} |",
        f"| Issues processed | {len(issues)} |",
        f"| Game date span | {earliest} → {latest} |",
        f"| Target cutoff | {until_date} |",
        f"| Games parsed | {parsed:,} |",
        f"| Decisive games studied | {decisive:,} ({pct(decisive, parsed)} of total) |",
        f"| Draws skipped for win-science | {draws:,} |",
        f"| Raw PGN status | Downloaded and move-tree parsed from theweekinchess.com/zips |",
        "",
        "## Selected frameworks (first principles)",
        "",
        "Three candidate lenses were compared across the decisive-game corpus:",
        "",
        "| Candidate | What it learns | Verdict |",
        "|---|---|---|",
        "| Opening-family priors | Which openings appear in wins | Context only — names are containers, not causes |",
        "| **Concession pressure ledger** | Whether normal replies force concessions | **Selected** — best cross-format explanation |",
        "| **Conversion-delta ledger** | Whether each transformation lowers risk | **Selected** — best elite/classical explanation |",
        "",
        "### Concession pressure",
        "",
        "A move belongs to a winning science only if the opponent pays for staying normal: weaker king shelter, lost tempo, denied pawn break, overloaded defender, worse square, or worse ending.",
        "",
        "### Conversion-delta",
        "",
        "A winning advantage must become safer and more permanent after every transformation: activity → target → invasion → material / endgame / mate.",
        "",
        "## Ranked winning reasons (portable commands)",
        "",
        "| Rank | Real reason | Practical command |",
        "|---|---|---|",
        "| 1 | Normal moves become concessions | Ask: what does the opponent lose by defending normally? |",
        "| 2 | One weakness is not enough | Fix one weakness, then create or switch to a second |",
        "| 3 | Counterplay must be denied before material is collected | Before grabbing, list checks, captures, threats, pawn breaks, simplifying trades |",
        "| 4 | Conversion is a change in risk, not only material | Trade only when opponent counterplay falls faster than your winning chances |",
        "| 5 | Opening names are containers, not causes | Learn the invariant, not the fashionable move order |",
        "| 6 | Technique is part of the line | After the tactic, define the endgame plan or mating net |",
        "| 7 | King safety precedes collection | Open lines toward the enemy king only when your own shelter math is stable |",
        "| 8 | Restriction beats direct capture | Limit enemy piece scope first; the tactic arrives when the defender is already narrowed |",
        "",
        "Compact formula: **Win = concession pressure × conversion delta × counterplay denial**",
        "",
        "## Corpus signals (decisive games only)",
        "",
        f"| Signal | Count | Rate |",
        f"|---|---|---|",
        f"| Winner castled while loser did not (first 40 plies) | {ledger.get('winner_castled_first', 0):,} | {pct(ledger.get('winner_castled_first', 0), decisive)} |",
        f"| Winner castled | {ledger.get('winner_castled_total', 0):,} | {pct(ledger.get('winner_castled_total', 0), decisive)} |",
        f"| Winner ahead in development (first 40 plies) | {ledger.get('winner_developed_8plus', 0):,} | {pct(ledger.get('winner_developed_8plus', 0), decisive)} |",
        f"| Winner delivered checks in the game | {ledger.get('winner_king_attack_flags', 0):,} | {pct(ledger.get('winner_king_attack_flags', 0), decisive)} |",
        f"| Queen trade while already ahead (≤40 plies) | {ledger.get('queen_trade_winner_ahead', 0):,} | {pct(ledger.get('queen_trade_winner_ahead', 0), decisive)} |",
        f"| Passed pawn + material edge at finish | {ledger.get('passed_pawn_conversion', 0):,} | {pct(ledger.get('passed_pawn_conversion', 0), decisive)} |",
        "",
        "### Phase at termination",
        "",
    ]
    for phase_name, count in sorted(phase.items(), key=lambda kv: -kv[1]):
        lines.append(f"- {phase_name}: {count:,} ({pct(count, decisive)})")
    lines.extend(["", "### Termination tags (prefix)", ""])
    for term, count in sorted(terms.items(), key=lambda kv: -kv[1])[:12]:
        lines.append(f"- {term}: {count:,}")
    lines.extend(["", "## Manual observations from sampled elite wins", ""])
    lines.extend(_manual_observations())
    lines.extend(["", "## Event samples (battlefields, not recipes)", ""])
    for event in events[:30]:
        lines.append(f"- {event}")
    lines.extend(["", "## Issue log (recent first)", "", "| TWIC | Games | Decisive | Date span |", "|---|---:|---:|---|"])
    for item in sorted(issues, key=lambda i: i["issue"], reverse=True)[:25]:
        span = f"{item.get('earliest_date', '?')} → {item.get('latest_date', '?')}"
        lines.append(
            f"| {item['issue']} | {item.get('game_count', 0):,} | {item.get('decisive_count', 0):,} | {span} |"
        )
    if len(issues) > 25:
        lines.append(f"| … | +{len(issues) - 25} more issues | | |")
    lines.extend(["", "## Next research slice", "", f"- Continue from TWIC {latest_issue - 1} if cutoff `{until_date}` not yet reached.", "- Promote any observation that survives two separate event types into the ranked table.", ""])
    return "\n".join(lines)


def _manual_observations() -> list[str]:
    return [
        "1. **Long forcing sequences exploit depth limits.** Many wins against strong defenders come from 3–5 move chains where each 'natural' reply worsens king shelter or loses a tempo — engines at limited depth often evaluate the start as equal.",
        "2. **Quiet restriction moves win.** A non-capture that limits knight/bishop scope or fixes a pawn on a bad color often precedes the tactic; the winning science is the restriction, not the final fork.",
        "3. **Bad-trade offers.** Winners repeatedly offer exchanges that look equal but leave the opponent with the wrong pawn structure or an open file toward their king.",
        "4. **Two-target conversion.** After the first weakness is fixed, winners switch to a second front (opposite flank, passed pawn, piece overload) instead of over-pressing one point.",
        "5. **Endgame technique is chosen early.** When ahead, winners trade into endings where the opponent's counterplay is structurally absent (wrong bishop color, passive rook, isolated king).",
        "6. **Blitz/classical invariant.** Faster time controls increase the rate of forced concessions — the same principles appear with more hanging-piece finishes, but the causal chain still starts with restriction or tempo.",
        "7. **King hunt is a conversion chain, not a sacrifice label.** Classic bishop-on-h7 patterns win because the defender's queen is diverted to a side task while the king loses shelter — the science is overload + king-shelter concessions, not the sacrifice name.",
        "8. **Perpetual/check triads convert to material.** Repeated checks that look like draws often force a concession (blocker interposition, passive king walk) that becomes a winning invasion on the next cycle.",
        "9. **Team events amplify restriction.** League and team-championship wins disproportionately come from small edge accumulation — bind a file, fix an enemy knight, then collect — rather than single-game fireworks.",
    ]


def main() -> None:
    if not STATE_PATH.exists():
        raise SystemExit(f"Missing state: {STATE_PATH}")
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    DOC_PATH.write_text(render(state), encoding="utf-8")
    print(json.dumps({"written": str(DOC_PATH), "issues": len(state.get("issues", []))}, indent=2))


if __name__ == "__main__":
    main()
