"""Playbook-chess trainer (PRD 168).

Turns Playbook-chess gate-game failures plus The Week In Chess corpus evidence
into bounded, human-readable weight updates in ``engines/playbook-chess/playbook.md``.

Every change is appended to the playbook Training log with:
- the diagnosed failure classes from the gate PGNs (blunder swing, failed
  conversion, repetition drift, king collapse, slow outplay),
- the TWIC theme statistics supporting the adjustment (from
  ``out/twic-manual-wisdom/batch-progress.json``),
- optionally a fresh measurement sampled directly from the newest local TWIC
  PGN zip (``--fresh-sample``).

The playbook must never receive exact FEN-to-move rules, opening lines, or
external-engine PVs (PRD 166); this trainer only nudges named numeric weights
inside safety bounds.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import chess
import chess.pgn

ROOT = Path(__file__).resolve().parents[1]
PLAYBOOK_PATH = ROOT / "engines" / "playbook-chess" / "playbook.md"
MATCHES_DIR = ROOT / "out" / "playbook-matches"
STATE_PATH = ROOT / "out" / "playbook-climb" / "trainer-state.json"
TWIC_PROGRESS = ROOT / "out" / "twic-manual-wisdom" / "batch-progress.json"
TWIC_DOWNLOADS = ROOT / "out" / "twic-manual-wisdom" / "downloads"

ENGINE_PGN_NAME = "Playbook-chess"
EVAL_RE = re.compile(r"eval ([+-]?\d+)cp")
WEIGHT_LINE_RE = re.compile(r"^(\s*-\s*)([a-z][a-z0-9_.]*)(\s*=\s*)(-?\d+(?:\.\d+)?)(.*)$")

# key -> (min, max, step per training round)
BOUNDS: dict[str, tuple[float, float, float]] = {
    "search.min_depth": (3, 6, 1),
    "search.base_movetime_ms": (4000, 30000, 2000),
    "search.draw_contempt": (10, 80, 10),
    "mobility.per_square": (1, 6, 1),
    "pieces.rook_open_file": (10, 32, 2),
    "pieces.rook_seventh": (10, 36, 2),
    "development.undeveloped_minor_penalty": (6, 25, 2),
    "development.uncastled_penalty": (8, 32, 3),
    "king.shield_pawn": (6, 24, 2),
    "king.open_file_penalty": (10, 40, 3),
    "king.ring_attack_penalty": (6, 24, 2),
    "pawns.passed_base": (10, 32, 2),
    "pawns.passed_per_rank": (8, 24, 2),
    "conversion.simplify_bonus": (2, 10, 1),
    "conversion.king_activity": (6, 24, 2),
}

# failure class -> (weights to bump, supporting TWIC themes)
CLASS_ADJUSTMENTS: dict[str, tuple[list[str], list[str]]] = {
    "blunder_swing": (
        ["search.base_movetime_ms", "search.min_depth"],
        ["material_swing"],
    ),
    "opening_blunder": (
        ["development.undeveloped_minor_penalty", "development.uncastled_penalty"],
        ["development_edge"],
    ),
    "king_collapse": (
        ["king.ring_attack_penalty", "king.shield_pawn", "king.open_file_penalty"],
        ["king_attack"],
    ),
    "failed_conversion": (
        ["conversion.simplify_bonus", "conversion.king_activity", "pawns.passed_per_rank", "search.draw_contempt"],
        ["passed_pawn", "queen_trade_ahead", "king_activation"],
    ),
    "repetition_draw": (
        ["search.draw_contempt"],
        ["material_swing"],
    ),
    "slow_outplay": (
        ["mobility.per_square", "pieces.rook_open_file", "search.base_movetime_ms"],
        ["development_edge", "seventh_rank"],
    ),
}


@dataclass
class GameDiagnosis:
    pgn_name: str
    result: str
    playbook_white: bool
    outcome: str  # win | draw | loss
    classes: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def playbook_evals(game: chess.pgn.Game, playbook_white: bool) -> list[tuple[int, int]]:
    """(fullmove_number, eval_cp) for each Playbook-chess move comment."""
    rows: list[tuple[int, int]] = []
    board = game.board()
    for node in game.mainline():
        move_by_playbook = (board.turn == chess.WHITE) == playbook_white
        if move_by_playbook and node.comment:
            match = EVAL_RE.search(node.comment)
            if match:
                rows.append((board.fullmove_number, int(match.group(1))))
        board.push(node.move)
    return rows


def diagnose_game(game: chess.pgn.Game) -> GameDiagnosis | None:
    white = game.headers.get("White", "")
    black = game.headers.get("Black", "")
    if ENGINE_PGN_NAME not in (white, black):
        return None
    playbook_white = white == ENGINE_PGN_NAME
    result = game.headers.get("Result", "*")
    if result == "*":
        return None
    if result == "1/2-1/2":
        outcome = "draw"
    elif (result == "1-0") == playbook_white:
        outcome = "win"
    else:
        outcome = "loss"
    diag = GameDiagnosis(
        pgn_name="",
        result=result,
        playbook_white=playbook_white,
        outcome=outcome,
    )
    if outcome == "win":
        return diag

    evals = playbook_evals(game, playbook_white)
    termination = game.headers.get("Termination", "").upper()

    # Blunder swings: big drop between consecutive own-move evals.
    biggest_drop = 0
    for (m1, e1), (m2, e2) in zip(evals, evals[1:]):
        drop = e1 - e2
        if drop >= 250:
            diag.classes.append("blunder_swing")
            if m2 <= 12:
                diag.classes.append("opening_blunder")
            diag.notes.append(f"eval fell {e1:+d} -> {e2:+d} around move {m2}")
            biggest_drop = max(biggest_drop, drop)
            break

    # Failed conversion: was clearly winning for a stretch but did not win.
    run = 0
    for _, ev in evals:
        run = run + 1 if ev >= 300 else 0
        if run >= 6:
            diag.classes.append("failed_conversion")
            diag.notes.append("held >= +300cp for 6+ own moves without winning")
            break

    if outcome == "draw":
        if "REPETITION" in termination or "FIVEFOLD" in termination or "THREEFOLD" in termination:
            diag.classes.append("repetition_draw")
            diag.notes.append(f"draw by {termination.lower()}")
        elif evals and evals[-1][1] >= 150:
            diag.classes.append("repetition_draw")
            diag.notes.append("drew while final self-eval was still positive")

    if outcome == "loss":
        if "CHECKMATE" in termination or "MATE" in termination:
            recent = [ev for _, ev in evals[-8:]]
            if recent and max(recent) >= -150:
                diag.classes.append("king_collapse")
                diag.notes.append("mated shortly after a roughly level self-eval")
        if biggest_drop == 0 and "blunder_swing" not in diag.classes:
            diag.classes.append("slow_outplay")
            diag.notes.append("gradual decline with no single 250cp swing")

    if not diag.classes:
        diag.classes.append("slow_outplay")
        diag.notes.append("non-win without a sharper diagnosis")
    return diag


def load_twic_rates() -> tuple[dict[str, tuple[int, float]], str]:
    """theme -> (count, rate%), plus a human citation of the corpus range."""
    try:
        progress = json.loads(TWIC_PROGRESS.read_text(encoding="utf-8"))
    except OSError:
        return {}, "TWIC corpus artifacts unavailable"
    total = int(progress.get("decisive_games") or 0)
    issues = progress.get("completed_issues") or []
    citation = f"TWIC issues {min(issues)}-{max(issues)}, {total:,} decisive games" if issues else "TWIC corpus"
    rates: dict[str, tuple[int, float]] = {}
    for theme, count in (progress.get("theme_totals") or {}).items():
        rates[theme] = (int(count), 100.0 * count / total if total else 0.0)
    return rates, citation


def fresh_twic_sample(theme_needed: str, sample_games: int) -> str | None:
    """Measure a class-relevant statistic directly from the newest TWIC PGN zip."""
    if sample_games <= 0:
        return None
    zips = sorted(TWIC_DOWNLOADS.glob("twic*g.zip"))
    if not zips:
        return None
    newest = zips[-1]
    checked = 0
    hits = 0
    try:
        with zipfile.ZipFile(newest) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith(".pgn")]
            if not names:
                return None
            with zf.open(names[0]) as raw:
                stream = io.TextIOWrapper(raw, encoding="latin-1", errors="replace")
                while checked < sample_games:
                    game = chess.pgn.read_game(stream)
                    if game is None:
                        break
                    result = game.headers.get("Result", "*")
                    if result not in ("1-0", "0-1"):
                        continue
                    board = game.board()
                    winner_white = result == "1-0"
                    winner_checks_late = 0
                    plies = []
                    for node in game.mainline():
                        plies.append((board.turn, node.move, board.is_capture(node.move)))
                        board.push(node.move)
                    if not plies:
                        continue
                    checked += 1
                    if theme_needed == "king_attack":
                        # Winner delivering check in the final 6 plies = attack finish.
                        tail_board = game.board()
                        gave_check = False
                        total = len(plies)
                        replay = game.board()
                        for i, node in enumerate(game.mainline()):
                            mover_white = replay.turn == chess.WHITE
                            replay.push(node.move)
                            if i >= total - 6 and mover_white == winner_white and replay.is_check():
                                gave_check = True
                        if gave_check:
                            hits += 1
                    elif theme_needed in ("passed_pawn", "king_activation", "queen_trade_ahead"):
                        # Winner has a passed pawn on the final board = conversion asset.
                        final = board
                        winner_color = chess.WHITE if winner_white else chess.BLACK
                        enemy_pawns = final.pieces_mask(chess.PAWN, not winner_color)
                        found = False
                        for sq in final.pieces(chess.PAWN, winner_color):
                            mask = 0
                            f, r = chess.square_file(sq), chess.square_rank(sq)
                            for df in (-1, 0, 1):
                                nf = f + df
                                if not 0 <= nf <= 7:
                                    continue
                                ranks = range(r + 1, 8) if winner_color == chess.WHITE else range(0, r)
                                for nr in ranks:
                                    mask |= chess.BB_SQUARES[chess.square(nf, nr)]
                            if not (enemy_pawns & mask):
                                found = True
                                break
                        if found:
                            hits += 1
                    else:
                        # Generic decisive-material proxy: winner ended ahead on material.
                        vals = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9}
                        diff = 0
                        for pt, v in vals.items():
                            diff += v * (len(board.pieces(pt, chess.WHITE)) - len(board.pieces(pt, chess.BLACK)))
                        if (diff > 0) == winner_white and diff != 0:
                            hits += 1
    except (OSError, zipfile.BadZipFile):
        return None
    if checked == 0:
        return None
    return f"fresh sample ({newest.name}, {checked} decisive games): {theme_needed} proxy {100.0 * hits / checked:.1f}%"


def apply_adjustments(text: str, adjustments: dict[str, float]) -> tuple[str, dict[str, tuple[float, float]]]:
    """Rewrite `- key = value` lines in place, preserving rationale text."""
    applied: dict[str, tuple[float, float]] = {}
    lines = text.splitlines()
    for i, line in enumerate(lines):
        match = WEIGHT_LINE_RE.match(line)
        if not match:
            continue
        key = match.group(2)
        if key not in adjustments:
            continue
        old = float(match.group(4))
        new = adjustments[key]
        if new == old:
            continue
        new_repr = str(int(new)) if float(new).is_integer() else f"{new:g}"
        lines[i] = f"{match.group(1)}{key}{match.group(3)}{new_repr}{match.group(5)}"
        applied[key] = (old, new)
    return "\n".join(lines) + ("\n" if text.endswith("\n") else ""), applied


def current_weights(text: str) -> dict[str, float]:
    weights: dict[str, float] = {}
    for line in text.splitlines():
        match = WEIGHT_LINE_RE.match(line)
        if match:
            try:
                weights[match.group(2)] = float(match.group(4))
            except ValueError:
                continue
    return weights


def train_round(
    pgn_paths: list[Path],
    playbook_path: Path = PLAYBOOK_PATH,
    fresh_sample: int = 0,
    dry_run: bool = False,
    twic_progress_path: Path | None = None,
) -> dict:
    """Diagnose the given gate PGNs and apply one bounded playbook update round."""
    global TWIC_PROGRESS
    if twic_progress_path is not None:
        TWIC_PROGRESS = twic_progress_path

    diagnoses: list[GameDiagnosis] = []
    for path in pgn_paths:
        try:
            with path.open(encoding="utf-8", errors="replace") as handle:
                game = chess.pgn.read_game(handle)
        except OSError:
            continue
        if game is None:
            continue
        diag = diagnose_game(game)
        if diag is None:
            continue
        diag.pgn_name = path.name
        diagnoses.append(diag)

    failures = [d for d in diagnoses if d.outcome != "win"]
    summary = {
        "games_scanned": len(diagnoses),
        "failures": len(failures),
        "classes": {},
        "adjustments": {},
        "log_entry": "",
        "changed": False,
    }
    class_counts: dict[str, int] = {}
    for diag in failures:
        for cls in set(diag.classes):
            class_counts[cls] = class_counts.get(cls, 0) + 1
    summary["classes"] = class_counts
    if not class_counts:
        return summary

    text = playbook_path.read_text(encoding="utf-8")
    weights = current_weights(text)
    targets: dict[str, float] = {}
    themes_needed: list[str] = []
    for cls in class_counts:
        keys, themes = CLASS_ADJUSTMENTS.get(cls, ([], []))
        themes_needed.extend(themes)
        for key in keys:
            lo, hi, step = BOUNDS.get(key, (None, None, None))
            if lo is None:
                continue
            cur = weights.get(key)
            if cur is None:
                continue
            # One bounded step per key per round, no matter how many failure
            # classes implicate the same weight (PRD 168).
            targets[key] = min(hi, max(lo, cur + step))

    targets = {k: v for k, v in targets.items() if weights.get(k) != v}
    new_text, applied = apply_adjustments(text, targets)

    version = int(weights.get("meta.version", 1))
    if applied:
        new_text, version_applied = apply_adjustments(new_text, {"meta.version": version + 1})
        applied.update(version_applied)

    rates, citation = load_twic_rates()
    evidence_bits: list[str] = []
    for theme in dict.fromkeys(themes_needed):
        if theme in rates:
            count, rate = rates[theme]
            evidence_bits.append(f"{theme} {count:,} games ({rate:.1f}%)")
    fresh_note = None
    if fresh_sample and themes_needed:
        fresh_note = fresh_twic_sample(themes_needed[0], fresh_sample)
        if fresh_note:
            evidence_bits.append(fresh_note)

    stamp = time.strftime("%Y-%m-%d %H:%M")
    game_bits = ", ".join(f"`{d.pgn_name}` ({d.result}, {d.outcome})" for d in failures)
    class_bits = ", ".join(f"{cls} x{n}" for cls, n in sorted(class_counts.items()))
    note_bits = "; ".join(note for d in failures for note in d.notes[:2])
    adj_bits = (
        "; ".join(
            f"{key} {old:g} -> {new:g}" for key, (old, new) in sorted(applied.items()) if key != "meta.version"
        )
        or "no weight headroom left inside safety bounds"
    )
    entry = (
        f"\n### {stamp} — Gate training round\n\n"
        f"Games: {game_bits}.\n\n"
        f"Diagnosis: {class_bits}. {note_bits}.\n\n"
        f"Adjustments: {adj_bits}.\n\n"
        f"Evidence: {citation}; " + "; ".join(evidence_bits) + ".\n"
    )
    summary["adjustments"] = {k: v for k, v in applied.items() if k != "meta.version"}
    summary["log_entry"] = entry
    summary["changed"] = bool(applied)

    if not dry_run:
        playbook_path.write_text(new_text + entry, encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matches-dir", type=Path, default=MATCHES_DIR)
    parser.add_argument("--playbook", type=Path, default=PLAYBOOK_PATH)
    parser.add_argument("--fresh-sample", type=int, default=0, help="sample N fresh games from the newest TWIC zip")
    parser.add_argument("--limit", type=int, default=12, help="newest N gate PGNs to scan")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    pgns = sorted(args.matches_dir.glob("*.pgn"), key=lambda p: p.stat().st_mtime)[-args.limit :]
    summary = train_round(pgns, args.playbook, fresh_sample=args.fresh_sample, dry_run=args.dry_run)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
