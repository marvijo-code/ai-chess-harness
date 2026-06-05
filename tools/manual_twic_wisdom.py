"""Download TWIC archives and extract decisive-game signals for manual wisdom research."""
from __future__ import annotations

import argparse
import io
import json
import re
import urllib.request
import zipfile
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path

import chess
import chess.pgn

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "out" / "twic-manual-wisdom"
TWIC_BASE = "https://theweekinchess.com/zips/twic{n}g.zip"
RESULT_WIN_RE = re.compile(r"^(1-0|0-1)$")
DATE_HEADER_RE = re.compile(r"^(\d{4})\.(\d{2})\.(\d{2})$")
PIECE_VALUES = {
    chess.PAWN: 100,
    chess.KNIGHT: 320,
    chess.BISHOP: 330,
    chess.ROOK: 500,
    chess.QUEEN: 900,
    chess.KING: 0,
}


@dataclass
class IssueMeta:
    issue: int
    zip_name: str
    game_count: int = 0
    decisive_count: int = 0
    draw_count: int = 0
    earliest_date: str | None = None
    latest_date: str | None = None
    downloaded: bool = False
    parse_error: str | None = None


@dataclass
class SignalLedger:
    games_parsed: int = 0
    decisive_games: int = 0
    winner_castled_first: int = 0
    winner_castled_total: int = 0
    loser_castled_total: int = 0
    winner_developed_8plus: int = 0
    loser_developed_8plus: int = 0
    winner_king_attack_flags: int = 0
    queen_trade_winner_ahead: int = 0
    pawn_break_success: int = 0
    open_file_to_king: int = 0
    passed_pawn_conversion: int = 0
    phase_counts: Counter = field(default_factory=Counter)
    termination_counts: Counter = field(default_factory=Counter)
    event_samples: list[str] = field(default_factory=list)


def request_url(url: str) -> urllib.request.Request:
    return urllib.request.Request(url, headers={"User-Agent": "chess-harness-codex/1.0"})


def parse_pgn_date(value: str | None) -> date | None:
    if not value:
        return None
    match = DATE_HEADER_RE.match(value.strip())
    if not match:
        return None
    year, month, day = map(int, match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def download_issue(issue: int, downloads_dir: Path) -> Path | None:
    downloads_dir.mkdir(parents=True, exist_ok=True)
    zip_path = downloads_dir / f"twic{issue}g.zip"
    if zip_path.exists() and zip_path.stat().st_size > 1000:
        return zip_path
    url = TWIC_BASE.format(n=issue)
    try:
        data = urllib.request.urlopen(request_url(url), timeout=120).read()
    except Exception as exc:  # noqa: BLE001
        return None
    if len(data) < 1000:
        return None
    zip_path.write_bytes(data)
    return zip_path


def read_pgn_texts(zip_path: Path) -> list[str]:
    texts: list[str] = []
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            if name.lower().endswith(".pgn"):
                texts.append(zf.read(name).decode("utf-8", "replace"))
    return texts


def count_developed(board: chess.Board, color: chess.Color) -> int:
    start_rank = chess.BB_RANK_1 if color == chess.WHITE else chess.BB_RANK_8
    developed = 0
    for piece_type in (chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN):
        pieces = board.pieces(piece_type, color)
        developed += len(pieces & ~start_rank)
    return developed


def side_castled(board: chess.Board, color: chess.Color) -> bool:
    king_square = board.king(color)
    if king_square is None:
        return False
    file_idx = chess.square_file(king_square)
    rank_idx = chess.square_rank(king_square)
    if color == chess.WHITE:
        return rank_idx == 0 and file_idx in (1, 2, 6, 7)
    return rank_idx == 7 and file_idx in (1, 2, 6, 7)


def winner_side(game: chess.pgn.Game) -> chess.Color | None:
    result = game.headers.get("Result", "*")
    if result == "1-0":
        return chess.WHITE
    if result == "0-1":
        return chess.BLACK
    return None


def game_phase(board: chess.Board) -> str:
    queens = len(board.pieces(chess.QUEEN, chess.WHITE)) + len(board.pieces(chess.QUEEN, chess.BLACK))
    minors = (
        len(board.pieces(chess.KNIGHT, chess.WHITE))
        + len(board.pieces(chess.KNIGHT, chess.BLACK))
        + len(board.pieces(chess.BISHOP, chess.WHITE))
        + len(board.pieces(chess.BISHOP, chess.BLACK))
    )
    if queens >= 2:
        return "opening_middlegame"
    if queens == 1 and minors >= 4:
        return "late_middlegame"
    if queens == 0:
        return "queenless_middlegame"
    return "endgame"


def scan_game_signals(game: chess.pgn.Game, ledger: SignalLedger) -> None:
    winner = winner_side(game)
    if winner is None:
        return
    loser = not winner
    ledger.decisive_games += 1
    term = game.headers.get("Termination", "unknown")
    ledger.termination_counts[term.split()[0] if term else "unknown"] += 1

    board = game.board()
    ply = 0
    winner_castled = False
    loser_castled = False
    max_winner_dev = 0
    max_loser_dev = 0
    winner_attack = False
    queen_trade_ahead = False
    pawn_break = False
    open_king_file = False
    passed_pawn_win = False
    winner_material = 0

    for move in game.mainline_moves():
        ply += 1
        board.push(move)
        if ply <= 40:
            if side_castled(board, winner):
                winner_castled = True
            if side_castled(board, loser):
                loser_castled = True
            max_winner_dev = max(max_winner_dev, count_developed(board, winner))
            max_loser_dev = max(max_loser_dev, count_developed(board, loser))
        if board.is_check():
            winner_attack = True
        white_mat = sum(len(board.pieces(pt, chess.WHITE)) * PIECE_VALUES[pt] for pt in chess.PIECE_TYPES)
        black_mat = sum(len(board.pieces(pt, chess.BLACK)) * PIECE_VALUES[pt] for pt in chess.PIECE_TYPES)
        winner_material = white_mat - black_mat if winner == chess.WHITE else black_mat - white_mat
        if (
            len(board.pieces(chess.QUEEN, chess.WHITE)) + len(board.pieces(chess.QUEEN, chess.BLACK)) < 2
            and winner_material >= 300
            and ply <= 40
        ):
            queen_trade_ahead = True

    final_board = board
    ledger.phase_counts[game_phase(final_board)] += 1
    if winner_castled:
        ledger.winner_castled_total += 1
    if loser_castled:
        ledger.loser_castled_total += 1
    if winner_castled and not loser_castled:
        ledger.winner_castled_first += 1
    if max_winner_dev >= 3 and max_winner_dev >= max_loser_dev + 1:
        ledger.winner_developed_8plus += 1
    if max_loser_dev >= 3 and max_loser_dev > max_winner_dev:
        ledger.loser_developed_8plus += 1
    if winner_attack:
        ledger.winner_king_attack_flags += 1
    if queen_trade_ahead:
        ledger.queen_trade_winner_ahead += 1

    # Passed pawn for winner in final position
    for sq in chess.SQUARES:
        piece = final_board.piece_at(sq)
        if piece and piece.piece_type == chess.PAWN and piece.color == winner:
            file_idx = chess.square_file(sq)
            rank = chess.square_rank(sq)
            ahead = True
            for r in range(rank + (1 if winner == chess.WHITE else -1), 8 if winner == chess.WHITE else -1, 1 if winner == chess.WHITE else -1):
                if final_board.piece_at(chess.square(file_idx, r)):
                    ahead = False
                    break
            if ahead and winner_material > 0:
                passed_pawn_win = True
                break
    if passed_pawn_win:
        ledger.passed_pawn_conversion += 1

    event = game.headers.get("Event", "")
    if event and len(ledger.event_samples) < 40 and event not in ledger.event_samples:
        ledger.event_samples.append(event)


def process_issue(issue: int, downloads_dir: Path) -> tuple[IssueMeta, SignalLedger]:
    meta = IssueMeta(issue=issue, zip_name=f"twic{issue}g.zip")
    ledger = SignalLedger()
    zip_path = download_issue(issue, downloads_dir)
    if zip_path is None:
        meta.parse_error = "download_failed"
        return meta, ledger
    meta.downloaded = True
    dates: list[date] = []
    try:
        for text in read_pgn_texts(zip_path):
            stream = io.StringIO(text)
            while True:
                game = chess.pgn.read_game(stream)
                if game is None:
                    break
                meta.game_count += 1
                ledger.games_parsed += 1
                result = game.headers.get("Result", "*")
                if result == "1/2-1/2":
                    meta.draw_count += 1
                    continue
                game_date = parse_pgn_date(game.headers.get("Date")) or parse_pgn_date(game.headers.get("EventDate"))
                if game_date:
                    dates.append(game_date)
                if RESULT_WIN_RE.match(result or ""):
                    meta.decisive_count += 1
                    try:
                        scan_game_signals(game, ledger)
                    except Exception:  # noqa: BLE001
                        continue
    except Exception as exc:  # noqa: BLE001
        meta.parse_error = str(exc)
    if dates:
        meta.earliest_date = min(dates).isoformat()
        meta.latest_date = max(dates).isoformat()
    return meta, ledger


def merge_ledgers(target: SignalLedger, source: SignalLedger) -> None:
    target.games_parsed += source.games_parsed
    target.decisive_games += source.decisive_games
    target.winner_castled_first += source.winner_castled_first
    target.winner_castled_total += source.winner_castled_total
    target.loser_castled_total += source.loser_castled_total
    target.winner_developed_8plus += source.winner_developed_8plus
    target.loser_developed_8plus += source.loser_developed_8plus
    target.winner_king_attack_flags += source.winner_king_attack_flags
    target.queen_trade_winner_ahead += source.queen_trade_winner_ahead
    target.pawn_break_success += source.pawn_break_success
    target.open_file_to_king += source.open_file_to_king
    target.passed_pawn_conversion += source.passed_pawn_conversion
    target.phase_counts.update(source.phase_counts)
    target.termination_counts.update(source.termination_counts)
    for sample in source.event_samples:
        if sample not in target.event_samples and len(target.event_samples) < 80:
            target.event_samples.append(sample)


def find_start_issue(target: date, downloads_dir: Path, hi: int = 1647, lo: int = 1500) -> int:
    """Binary search approximate issue whose latest game date is on/after target."""
    best = hi
    while lo <= hi:
        mid = (lo + hi) // 2
        meta, _ = process_issue(mid, downloads_dir)
        if meta.latest_date and meta.latest_date >= target.isoformat():
            best = mid
            hi = mid - 1
        else:
            lo = mid + 1
    return best


def ledger_to_dict(ledger: SignalLedger) -> dict:
    return {
        **{k: v for k, v in asdict(ledger).items() if k not in ("phase_counts", "termination_counts", "event_samples")},
        "phase_counts": dict(ledger.phase_counts),
        "termination_counts": dict(ledger.termination_counts),
        "event_samples": ledger.event_samples,
    }


def rebuild_ledger(issues: list[dict], downloads_dir: Path) -> SignalLedger:
    total = SignalLedger()
    for item in sorted(issues, key=lambda row: row["issue"]):
        _, ledger = process_issue(item["issue"], downloads_dir)
        merge_ledgers(total, ledger)
    return total


def run_batch(start_issue: int, until_date: date, out_dir: Path, rebuild: bool = False) -> dict:
    downloads_dir = out_dir / "downloads"
    state_path = out_dir / "state.json"
    state = {"issues": [], "ledger": {}, "until_date": until_date.isoformat()}
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))

    processed = {item["issue"] for item in state.get("issues", [])}
    if rebuild and state.get("issues"):
        total_ledger = rebuild_ledger(state["issues"], downloads_dir)
        state["ledger"] = ledger_to_dict(total_ledger)
        state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    else:
        total_ledger = SignalLedger()
        if state.get("ledger"):
            total_ledger = SignalLedger(
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

    if processed:
        start_issue = min(start_issue, min(processed) - 1)

    stop = False
    for issue in range(start_issue, 1400, -1):
        if issue in processed:
            continue
        meta, ledger = process_issue(issue, downloads_dir)
        state["issues"].append(asdict(meta))
        merge_ledgers(total_ledger, ledger)
        processed.add(issue)
        if meta.latest_date and meta.latest_date < until_date.isoformat():
            stop = True
        state["ledger"] = ledger_to_dict(total_ledger)
        state["latest_issue"] = issue
        state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        print(json.dumps({"issue": issue, "meta": asdict(meta), "running_totals": state["ledger"]}))
        if stop:
            break
    return state


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-issue", type=int, default=1647)
    parser.add_argument("--until-date", default="2025-01-01")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--probe", action="store_true", help="Probe one issue only")
    parser.add_argument("--rebuild-ledger", action="store_true", help="Recompute signals for processed issues")
    args = parser.parse_args()
    until = date.fromisoformat(args.until_date)
    if args.probe:
        meta, ledger = process_issue(args.start_issue, args.out_dir / "downloads")
        payload = {
            "meta": asdict(meta),
            "ledger": {
                **{k: v for k, v in asdict(ledger).items() if k not in ("phase_counts", "termination_counts", "event_samples")},
                "phase_counts": dict(ledger.phase_counts),
                "termination_counts": dict(ledger.termination_counts),
                "event_samples": ledger.event_samples,
            },
        }
        print(json.dumps(payload, indent=2))
        return
    state = run_batch(args.start_issue, until, args.out_dir, rebuild=args.rebuild_ledger)
    print(json.dumps({"done": True, "issues": len(state["issues"]), "ledger": state["ledger"]}, indent=2))


if __name__ == "__main__":
    main()
