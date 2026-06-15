"""Human-readable live status for the TWIC wisdom batch."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "out" / "twic-manual-wisdom"
STATE_PATH = OUT_DIR / "state.json"
PROGRESS_PATH = OUT_DIR / "batch-progress.json"
STATUS_JSON = OUT_DIR / "batch-status.json"
STATUS_MD = ROOT / "docs" / "manual-wisdom-batch-status.md"
EXTEND_FLOOR = 1401


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def compute_status(
    phase: str = "idle",
    current_issue: int | None = None,
    note: str = "",
) -> dict:
    state = load_json(STATE_PATH)
    progress = load_json(PROGRESS_PATH)
    state_issues = state.get("issues", [])
    scanned_nums = sorted((row["issue"] for row in state_issues), reverse=True)
    completed = sorted(progress.get("completed_issues", []), reverse=True)
    completed_set = set(completed)
    pending_analysis = sorted(set(scanned_nums) - completed_set, reverse=True)

    extend_from = max(scanned_nums) if scanned_nums else 1647
    extend_floor = EXTEND_FLOOR
    extend_total = max(0, extend_from - extend_floor)
    extend_scanned = sum(1 for n in scanned_nums if n <= extend_from and n >= extend_floor)
    extend_remaining_scan = max(0, (min(scanned_nums) if scanned_nums else extend_from) - extend_floor)

    status = {
        "updated_at": utc_now(),
        "phase": phase,
        "current_issue": current_issue,
        "note": note,
        "scanned_issues": len(scanned_nums),
        "scanned_newest": scanned_nums[0] if scanned_nums else None,
        "scanned_oldest": scanned_nums[-1] if scanned_nums else None,
        "analyzed_issues": len(completed),
        "analyzed_newest": completed[0] if completed else None,
        "analyzed_oldest": completed[-1] if completed else None,
        "pending_analysis": len(pending_analysis),
        "pending_next": pending_analysis[0] if pending_analysis else None,
        "decisive_games": progress.get("decisive_games", 0),
        "extend_floor": extend_floor,
        "extend_remaining_scan": extend_remaining_scan,
        "progress_updated_at": progress.get("updated_at"),
    }
    return status


def render_status_md(status: dict) -> str:
    phase = status.get("phase", "idle")
    current = status.get("current_issue")
    lines = [
        "# TWIC wisdom batch — live status",
        "",
        f"Updated: {status.get('updated_at', '?')}",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Phase | **{phase}** |",
    ]
    if current is not None:
        lines.append(f"| Current TWIC | **{current}** |")
    lines.extend(
        [
            f"| Scanned in catalog | **{status.get('scanned_issues', 0)}** ({status.get('scanned_newest', '?')} → {status.get('scanned_oldest', '?')}) |",
            f"| Analyzed for wisdom | **{status.get('analyzed_issues', 0)}** ({status.get('analyzed_newest', '?')} → {status.get('analyzed_oldest', '?')}) |",
            f"| Pending analysis | **{status.get('pending_analysis', 0)}** |",
            f"| Decisive games in corpus | **{status.get('decisive_games', 0):,}** |",
            f"| Extend floor | TWIC **{status.get('extend_floor', '?')}** |",
            f"| Issues left to scan | **{status.get('extend_remaining_scan', '?')}** |",
            "",
        ]
    )
    if status.get("note"):
        lines.extend([status["note"], ""])
    if status.get("pending_next"):
        lines.append(f"Next queued for analysis: **TWIC {status['pending_next']}**")
        lines.append("")
    lines.extend(
        [
            "Files: `out/twic-manual-wisdom/batch-status.json` · "
            "[manual-wisdom-sources.md](manual-wisdom-sources.md)",
            "",
            "Refresh: `python tools/twic_batch_status.py`",
            "",
        ]
    )
    return "\n".join(lines)


def write_status(
    phase: str = "idle",
    current_issue: int | None = None,
    note: str = "",
) -> dict:
    status = compute_status(phase=phase, current_issue=current_issue, note=note)
    STATUS_JSON.parent.mkdir(parents=True, exist_ok=True)
    STATUS_JSON.write_text(json.dumps(status, indent=2), encoding="utf-8")
    STATUS_MD.write_text(render_status_md(status), encoding="utf-8")
    line = (
        f"[{status['updated_at']}] {phase}"
        + (f" TWIC {current_issue}" if current_issue is not None else "")
        + f" | scanned {status['scanned_issues']} | analyzed {status['analyzed_issues']}"
        + f" | pending {status['pending_analysis']} | decisive {status['decisive_games']:,}"
    )
    print(line, flush=True)
    return status


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Show or refresh TWIC batch status")
    parser.add_argument("--phase", default="idle")
    parser.add_argument("--issue", type=int, default=None)
    parser.add_argument("--note", default="")
    args = parser.parse_args()
    status = write_status(phase=args.phase, current_issue=args.issue, note=args.note)
    print(json.dumps(status, indent=2))


if __name__ == "__main__":
    main()
