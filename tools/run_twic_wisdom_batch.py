"""Process all decisive TWIC games, aggregate wisdom, commit/push per issue."""
from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

import chess.pgn

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "out" / "twic-manual-wisdom"
DOCS_MASTER = ROOT / "docs" / "manual-wisdom-master-games.md"
DOCS_CHECKLIST = ROOT / "docs" / "manual-wisdom-checklist.md"
EXTEND_FLOOR = 1401

sys.path.insert(0, str(Path(__file__).resolve().parent))
from analyze_twic_decisive_games import GameAnalysis, analyze_decisive_game
from manual_twic_wisdom import (
    DEFAULT_OUT,
    RESULT_WIN_RE,
    SignalLedger,
    asdict,
    download_issue,
    ledger_to_dict,
    merge_ledgers,
    parse_pgn_date,
    process_issue,
    read_pgn_texts,
    scan_game_signals,
)
from synthesize_twic_wisdom import write_wisdom_artifacts
from twic_batch_status import write_status

COMMIT_PATHS = [
    "docs/manual-wisdom-master-games.md",
    "docs/manual-wisdom-sources.md",
    "docs/manual-wisdom-sources.json",
    "docs/manual-wisdom-batch-status.md",
    "docs/manual-wisdom-checklist.md",
    "docs/manual-wisdom-prompt.md",
    "tools/manual_twic_wisdom.py",
    "tools/analyze_twic_decisive_games.py",
    "tools/twic_game_board.py",
    "tools/run_twic_wisdom_batch.py",
    "tools/synthesize_twic_wisdom.py",
    "tools/twic_batch_status.py",
    "tools/render_manual_wisdom_doc.py",
    ".gitignore",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_state(out_dir: Path) -> dict:
    state_path = out_dir / "state.json"
    if not state_path.exists():
        raise SystemExit(f"Missing {state_path}; run manual_twic_wisdom.py first.")
    return json.loads(state_path.read_text(encoding="utf-8"))


def save_state(out_dir: Path, state: dict) -> None:
    (out_dir / "state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")


def load_issues(out_dir: Path) -> list[dict]:
    return sorted(load_state(out_dir).get("issues", []), key=lambda row: row["issue"], reverse=True)


def scan_issue_into_state(out_dir: Path, issue: int) -> dict:
    state = load_state(out_dir)
    known = {row["issue"] for row in state.get("issues", [])}
    if issue in known:
        return next(row for row in state["issues"] if row["issue"] == issue)

    write_status(phase="scan", current_issue=issue, note=f"Downloading and parsing twic{issue}g.zip")
    meta, ledger = process_issue(issue, out_dir / "downloads")
    state.setdefault("issues", []).append(asdict(meta))
    total = SignalLedger()
    if state.get("ledger"):
        total = SignalLedger(
            games_parsed=state["ledger"].get("games_parsed", 0),
            decisive_games=state["ledger"].get("decisive_games", 0),
            winner_castled_first=state["ledger"].get("winner_castled_first", 0),
            winner_castled_total=state["ledger"].get("winner_castled_total", 0),
            loser_castled_total=state["ledger"].get("loser_castled_total", 0),
            winner_developed_8plus=state["ledger"].get("winner_developed_8plus", 0),
            loser_developed_8plus=state["ledger"].get("loser_developed_8plus", 0),
            winner_king_attack_flags=state["ledger"].get("winner_king_attack_flags", 0),
            queen_trade_winner_ahead=state["ledger"].get("queen_trade_winner_ahead", 0),
            pawn_break_success=state["ledger"].get("pawn_break_success", 0),
            open_file_to_king=state["ledger"].get("open_file_to_king", 0),
            passed_pawn_conversion=state["ledger"].get("passed_pawn_conversion", 0),
            phase_counts=Counter(state["ledger"].get("phase_counts", {})),
            termination_counts=Counter(state["ledger"].get("termination_counts", {})),
            event_samples=state["ledger"].get("event_samples", []),
        )
    merge_ledgers(total, ledger)
    state["ledger"] = ledger_to_dict(total)
    state["latest_issue"] = issue
    save_state(out_dir, state)
    return asdict(meta)


def analyze_issue_full(issue: int, downloads_dir: Path) -> tuple[list[GameAnalysis], SignalLedger, list[date]]:
    zip_path = download_issue(issue, downloads_dir)
    if zip_path is None:
        return [], SignalLedger(), []
    analyses: list[GameAnalysis] = []
    ledger = SignalLedger()
    dates: list[date] = []
    for text in read_pgn_texts(zip_path):
        stream = io.StringIO(text)
        while True:
            game = chess.pgn.read_game(stream)
            if game is None:
                break
            result = game.headers.get("Result", "*")
            if result == "1/2-1/2":
                continue
            d = parse_pgn_date(game.headers.get("Date")) or parse_pgn_date(game.headers.get("EventDate"))
            if d:
                dates.append(d)
            if not RESULT_WIN_RE.match(result or ""):
                continue
            try:
                scan_game_signals(game, ledger)
                row = analyze_decisive_game(game, issue)
                if row:
                    analyses.append(row)
            except Exception:
                continue
    return analyses, ledger, dates


def theme_counts_from_analyses(analyses: list[GameAnalysis]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for row in analyses:
        for theme in row.themes:
            counts[theme] += 1
    return counts


def write_wisdom_doc(progress: dict) -> None:
    theme_totals = Counter(progress.get("theme_totals", {}))
    write_wisdom_artifacts(progress, theme_totals)


def git_commit_push(message: str, paths: list[str]) -> bool:
    existing = [p for p in paths if (ROOT / p).exists()]
    if not existing:
        return False
    subprocess.run(["git", "add", *existing], cwd=ROOT, check=True)
    staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT)
    if staged.returncode == 0:
        return False
    subprocess.run(["git", "commit", "-m", message], cwd=ROOT, check=True)
    subprocess.run(["git", "push"], cwd=ROOT, check=True)
    return True


def build_work_queue(out_dir: Path, extend: bool, completed: set[int]) -> list[int]:
    state = load_state(out_dir)
    scanned = {row["issue"] for row in state.get("issues", [])}
    pending = sorted(scanned - completed, reverse=True)
    if not extend:
        return pending

    floor = min(scanned) if scanned else 1647
    if floor <= EXTEND_FLOOR:
        return pending

    extension = list(range(floor - 1, EXTEND_FLOOR - 1, -1))
    seen: set[int] = set()
    queue: list[int] = []
    for issue in pending + extension:
        if issue not in seen:
            seen.add(issue)
            queue.append(issue)
    return queue


def run_batch(
    out_dir: Path,
    until_date: date,
    commit_every: int = 1,
    push: bool = True,
    extend: bool = False,
    stop_at_cutoff: bool = True,
) -> dict:
    progress_path = out_dir / "batch-progress.json"
    progress = {"completed_issues": [], "decisive_games": 0, "wisdom_updates": 0, "theme_totals": {}}
    if progress_path.exists():
        progress = json.loads(progress_path.read_text(encoding="utf-8"))
        progress.setdefault("theme_totals", {})

    completed = set(progress.get("completed_issues", []))
    theme_totals: Counter[str] = Counter(progress.get("theme_totals", {}))
    pending_commit = 0
    work = build_work_queue(out_dir, extend, completed)

    write_status(
        phase="starting",
        note=f"Work queue: {len(work)} issues ({'extend to ' + str(EXTEND_FLOOR) if extend else 'analyze pending only'})",
    )

    for issue in work:
        meta = scan_issue_into_state(out_dir, issue) if extend else next(
            (row for row in load_issues(out_dir) if row["issue"] == issue), {"issue": issue}
        )
        if issue in completed:
            continue

        write_status(
            phase="analyze",
            current_issue=issue,
            note=f"Tagging {meta.get('decisive_count', '?')} decisive games for wisdom corpus",
        )
        analyses, ledger, _ = analyze_issue_full(issue, out_dir / "downloads")
        issue_themes = theme_counts_from_analyses(analyses)
        theme_totals.update(issue_themes)

        completed.add(issue)
        progress["completed_issues"] = sorted(completed, reverse=True)
        progress["decisive_games"] = progress.get("decisive_games", 0) + len(analyses)
        progress["wisdom_updates"] = progress.get("wisdom_updates", 0) + 1
        progress["theme_totals"] = dict(theme_totals)
        progress["latest_issue"] = issue
        progress["updated_at"] = utc_now()
        progress_path.write_text(json.dumps(progress, indent=2), encoding="utf-8")

        write_wisdom_doc(progress)
        write_status(
            phase="analyze",
            current_issue=issue,
            note=f"Done TWIC {issue}: +{len(analyses):,} decisive ({progress['decisive_games']:,} total)",
        )

        (out_dir / "analysis-summary.json").write_text(
            json.dumps(
                {
                    "issue": issue,
                    "decisive": len(analyses),
                    "total_decisive": progress["decisive_games"],
                    "ledger": ledger_to_dict(ledger),
                    "theme_totals": progress["theme_totals"],
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        pending_commit += 1
        print(
            json.dumps({"issue": issue, "decisive": len(analyses), "total": progress["decisive_games"]}),
            flush=True,
        )

        if push and pending_commit >= commit_every:
            msg = f"docs: TWIC {issue} wisdom corpus ({len(analyses):,} decisive games)"
            if git_commit_push(msg, COMMIT_PATHS):
                pending_commit = 0

        if (
            stop_at_cutoff
            and meta.get("latest_date")
            and meta["latest_date"] < until_date.isoformat()
        ):
            break

    write_status(phase="done", note=f"Batch pass complete — {len(completed)} issues analyzed")

    if push and pending_commit > 0:
        git_commit_push(
            f"docs: TWIC wisdom batch progress ({len(completed)} issues)",
            COMMIT_PATHS,
        )

    return progress


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--until-date", default="2025-01-01", help="State scan stop + optional batch stop")
    parser.add_argument("--extend", action="store_true", help="Scan older TWIC issues into state.json first")
    parser.add_argument("--no-stop-at-cutoff", action="store_true", help="Process all issues in state (for extending past the original cutoff)")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--commit-every", type=int, default=1, help="Commit after N TWIC issues")
    parser.add_argument("--no-push", action="store_true")
    args = parser.parse_args()
    progress = run_batch(
        args.out_dir,
        date.fromisoformat(args.until_date),
        commit_every=args.commit_every,
        push=not args.no_push,
        extend=args.extend,
        stop_at_cutoff=not args.no_stop_at_cutoff,
    )
    print(json.dumps({"done": True, **progress}, indent=2))


if __name__ == "__main__":
    main()
