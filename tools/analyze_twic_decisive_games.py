"""Analyze every decisive TWIC game with first-principles heuristics."""
from __future__ import annotations

import argparse
import io
import json
import re
import urllib.request
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import chess
import chess.pgn

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from manual_twic_wisdom import (
    DEFAULT_OUT,
    PIECE_VALUES,
    RESULT_WIN_RE,
    SignalLedger,
    count_developed,
    download_issue,
    game_phase,
    ledger_to_dict,
    merge_ledgers,
    parse_pgn_date,
    read_pgn_texts,
    scan_game_signals,
    side_castled,
    winner_side,
)

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
    "material_swing": "A forcing sequence gained material; the line worked because normal replies conceded.",
}

THEME_PRIORITY = [
    "back_rank",
    "seventh_rank",
    "king_attack",
    "pawn_break",
    "queen_trade_ahead",
    "passed_pawn",
    "king_activation",
    "development_edge",
    "castled_first",
    "material_swing",
]


@dataclass
class GameAnalysis:
    twic_issue: int
    event: str
    date: str | None
    white: str
    black: str
    white_elo: int | None
    black_elo: int | None
    result: str
    winner: str
    plies: int
    phase: str
    themes: list[str] = field(default_factory=list)
    lesson: str = ""
    signals: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "twic_issue": self.twic_issue,
            "event": self.event,
            "date": self.date,
            "white": self.white,
            "black": self.black,
            "white_elo": self.white_elo,
            "black_elo": self.black_elo,
            "result": self.result,
            "winner": self.winner,
            "plies": self.plies,
            "phase": self.phase,
            "themes": self.themes,
            "lesson": self.lesson,
            "signals": self.signals,
        }


def parse_elo(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def material_for(board: chess.Board, color: chess.Color) -> int:
    return sum(len(board.pieces(pt, color)) * PIECE_VALUES[pt] for pt in chess.PIECE_TYPES)


def king_centralization_score(board: chess.Board, color: chess.Color) -> int:
    king_sq = board.king(color)
    if king_sq is None:
        return 0
    file_idx = chess.square_file(king_sq)
    rank = chess.square_rank(king_sq)
    if color == chess.WHITE:
        return min(file_idx, 7 - file_idx) + min(rank, 7 - rank)
    return min(file_idx, 7 - file_idx) + min(7 - rank, rank)


def analyze_decisive_game(game: chess.pgn.Game, issue: int) -> GameAnalysis | None:
    winner = winner_side(game)
    if winner is None:
        return None
    loser = not winner
    white = game.headers.get("White", "?")
    black = game.headers.get("Black", "?")
    result = game.headers.get("Result", "*")
    winner_name = white if winner == chess.WHITE else black
    game_date = game.headers.get("Date") or game.headers.get("EventDate")

    board = game.board()
    ply = 0
    winner_castled = loser_castled = False
    max_winner_dev = max_loser_dev = 0
    check_count = 0
    queen_trade_ahead = False
    pawn_break = False
    material_swing = False
    max_swing = 0
    prev_winner_mat = 0
    seventh_rank = False
    back_rank = False
    passed_pawn = False
    winner_king_moves_endgame = 0
    winner_king_start_cent = king_centralization_score(board, winner)
    queens_removed_ply: int | None = None

    for move in game.mainline_moves():
        ply += 1
        mover = board.turn
        piece = board.piece_at(move.from_square)
        was_king_move = piece == chess.Piece(chess.KING, mover)
        board.push(move)
        if ply <= 40:
            if side_castled(board, winner):
                winner_castled = True
            if side_castled(board, loser):
                loser_castled = True
            max_winner_dev = max(max_winner_dev, count_developed(board, winner))
            max_loser_dev = max(max_loser_dev, count_developed(board, loser))

        if board.is_check() and board.turn == loser:
            check_count += 1

        w_mat = material_for(board, chess.WHITE)
        b_mat = material_for(board, chess.BLACK)
        winner_mat = w_mat - b_mat if winner == chess.WHITE else b_mat - w_mat
        swing = winner_mat - prev_winner_mat
        if abs(swing) >= 300 and ply <= 50:
            material_swing = True
            max_swing = max(max_swing, abs(swing))
        prev_winner_mat = winner_mat

        if (
            len(board.pieces(chess.QUEEN, chess.WHITE)) + len(board.pieces(chess.QUEEN, chess.BLACK)) < 2
            and winner_mat >= 200
            and ply <= 45
        ):
            queen_trade_ahead = True

        if piece and piece.piece_type == chess.PAWN and mover == winner:
            from_rank = chess.square_rank(move.from_square)
            to_rank = chess.square_rank(move.to_square)
            if abs(to_rank - from_rank) == 2 or board.is_capture(move):
                pawn_break = True

        for sq in board.pieces(chess.ROOK, winner):
            rank = chess.square_rank(sq)
            if winner == chess.WHITE and rank == 6:
                seventh_rank = True
            if winner == chess.BLACK and rank == 1:
                seventh_rank = True

        loser_king = board.king(loser)
        if loser_king is not None:
            lr = chess.square_rank(loser_king)
            if (loser == chess.WHITE and lr == 0) or (loser == chess.BLACK and lr == 7):
                attackers = board.attackers(winner, loser_king)
                if len(attackers) >= 2:
                    back_rank = True

        if queens_off(board) and queens_removed_ply is None:
            queens_removed_ply = ply
            winner_king_start_cent = king_centralization_score(board, winner)

        if was_king_move and mover == winner and queens_removed_ply is not None and ply - queens_removed_ply <= 30:
            winner_king_moves_endgame += 1

    final = board
    winner_king_end_cent = king_centralization_score(final, winner)
    winner_mat_final = material_for(final, winner) - material_for(final, loser)

    for sq in chess.SQUARES:
        piece = final.piece_at(sq)
        if piece and piece.piece_type == chess.PAWN and piece.color == winner:
            file_idx = chess.square_file(sq)
            rank = chess.square_rank(sq)
            blocked = False
            step = 1 if winner == chess.WHITE else -1
            for r in range(rank + step, 8 if winner == chess.WHITE else -1, step):
                if final.piece_at(chess.square(file_idx, r)):
                    blocked = True
                    break
            if not blocked and winner_mat_final >= 0:
                passed_pawn = True
                break

    king_activation = (
        queens_off(final)
        and winner_king_moves_endgame >= 2
        and winner_king_end_cent > winner_king_start_cent
    )

    themes: list[str] = []
    if back_rank:
        themes.append("back_rank")
    if seventh_rank:
        themes.append("seventh_rank")
    if check_count >= 3:
        themes.append("king_attack")
    if pawn_break:
        themes.append("pawn_break")
    if queen_trade_ahead:
        themes.append("queen_trade_ahead")
    if passed_pawn:
        themes.append("passed_pawn")
    if king_activation:
        themes.append("king_activation")
    if max_winner_dev >= max_loser_dev + 1 and max_winner_dev >= 3:
        themes.append("development_edge")
    if winner_castled and not loser_castled:
        themes.append("castled_first")
    if material_swing:
        themes.append("material_swing")

    themes.sort(key=lambda t: THEME_PRIORITY.index(t) if t in THEME_PRIORITY else 99)
    primary = themes[0] if themes else "general_technique"
    lesson = THEME_LESSONS.get(primary, "Winning lines make normal defense concede something permanent.")

    return GameAnalysis(
        twic_issue=issue,
        event=game.headers.get("Event", ""),
        date=game_date,
        white=white,
        black=black,
        white_elo=parse_elo(game.headers.get("WhiteElo")),
        black_elo=parse_elo(game.headers.get("BlackElo")),
        result=result,
        winner=winner_name,
        plies=ply,
        phase=game_phase(final),
        themes=themes,
        lesson=lesson,
        signals={
            "checks": check_count,
            "winner_castled": winner_castled,
            "loser_castled": loser_castled,
            "development_edge": max_winner_dev - max_loser_dev,
            "queen_trade_ahead": queen_trade_ahead,
            "passed_pawn": passed_pawn,
            "king_activation": king_activation,
            "material_swing": material_swing,
        },
    )


def queens_off(board: chess.Board) -> bool:
    return len(board.pieces(chess.QUEEN, chess.WHITE)) + len(board.pieces(chess.QUEEN, chess.BLACK)) == 0


def analyze_issue(
    issue: int,
    downloads_dir: Path,
    jsonl_path: Path,
    theme_counter: Counter,
    lesson_counter: Counter,
    ledger: SignalLedger,
) -> tuple[int, int, int]:
    zip_path = download_issue(issue, downloads_dir)
    if zip_path is None:
        return 0, 0, 0
    games_total = decisive = 0
    with jsonl_path.open("a", encoding="utf-8") as out:
        for text in read_pgn_texts(zip_path):
            stream = io.StringIO(text)
            while True:
                game = chess.pgn.read_game(stream)
                if game is None:
                    break
                games_total += 1
                ledger.games_parsed += 1
                result = game.headers.get("Result", "*")
                if result == "1/2-1/2":
                    continue
                if not RESULT_WIN_RE.match(result or ""):
                    continue
                decisive += 1
                try:
                    scan_game_signals(game, ledger)
                    analysis = analyze_decisive_game(game, issue)
                    if analysis is None:
                        continue
                    out.write(json.dumps(analysis.to_dict(), ensure_ascii=False) + "\n")
                    for theme in analysis.themes:
                        theme_counter[theme] += 1
                    lesson_counter[analysis.lesson] += 1
                except Exception:
                    continue
    return games_total, decisive, decisive


def run_analyze_all(
    start_issue: int,
    until_date: date,
    out_dir: Path,
    resume: bool = True,
) -> dict:
    downloads_dir = out_dir / "downloads"
    jsonl_path = out_dir / "decisive-analysis.jsonl"
    summary_path = out_dir / "analysis-summary.json"
    progress_path = out_dir / "analysis-progress.json"

    progress = {"completed_issues": [], "theme_counts": {}, "lesson_counts": {}, "decisive_games": 0}
    if resume and progress_path.exists():
        progress = json.loads(progress_path.read_text(encoding="utf-8"))

    completed = set(progress.get("completed_issues", []))
    theme_counter = Counter(progress.get("theme_counts", {}))
    lesson_counter = Counter(progress.get("lesson_counts", {}))
    ledger = SignalLedger()

    if state_path := out_dir / "state.json":
        if state_path.exists() and not completed:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            issues_from_state = [i["issue"] for i in state.get("issues", [])]
        else:
            issues_from_state = []
    else:
        issues_from_state = []

    issue_range: list[int] = []
    if issues_from_state:
        issue_range = sorted(issues_from_state, reverse=True)
    else:
        stop = False
        for issue in range(start_issue, 1400, -1):
            if issue in completed:
                continue
            issue_range.append(issue)

    for issue in issue_range:
        if issue in completed:
            continue
        zip_path = download_issue(issue, downloads_dir)
        if zip_path is None:
            continue
        meta_dates: list[date] = []
        with zipfile.ZipFile(zip_path) as zf:
            for name in zf.namelist():
                if not name.lower().endswith(".pgn"):
                    continue
                stream = io.StringIO(zf.read(name).decode("utf-8", "replace"))
                while True:
                    g = chess.pgn.read_game(stream)
                    if g is None:
                        break
                    d = parse_pgn_date(g.headers.get("Date")) or parse_pgn_date(g.headers.get("EventDate"))
                    if d:
                        meta_dates.append(d)

        _, decisive, _ = analyze_issue(issue, downloads_dir, jsonl_path, theme_counter, lesson_counter, ledger)
        completed.add(issue)
        progress["completed_issues"] = sorted(completed, reverse=True)
        progress["theme_counts"] = dict(theme_counter)
        progress["lesson_counts"] = dict(lesson_counter.most_common(50))
        progress["decisive_games"] = progress.get("decisive_games", 0) + decisive
        progress["latest_issue"] = issue
        progress_path.write_text(json.dumps(progress, indent=2), encoding="utf-8")

        latest = max(meta_dates).isoformat() if meta_dates else None
        print(json.dumps({"issue": issue, "decisive": decisive, "total_decisive": progress["decisive_games"], "latest_date": latest}))

        if latest and latest < until_date.isoformat():
            break

    total_decisive = progress.get("decisive_games", 0)
    summary = {
        "until_date": until_date.isoformat(),
        "issues_analyzed": len(completed),
        "decisive_games": total_decisive,
        "theme_counts": dict(theme_counter.most_common()),
        "top_lessons": [{"lesson": k, "count": v} for k, v in lesson_counter.most_common(15)],
        "ledger": ledger_to_dict(ledger),
        "jsonl": str(jsonl_path),
    }
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-issue", type=int, default=1647)
    parser.add_argument("--until-date", default="2025-01-01")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--no-resume", action="store_true")
    args = parser.parse_args()
    summary = run_analyze_all(
        args.start_issue,
        date.fromisoformat(args.until_date),
        args.out_dir,
        resume=not args.no_resume,
    )
    print(json.dumps({"done": True, **{k: summary[k] for k in ("issues_analyzed", "decisive_games")}}, indent=2))


if __name__ == "__main__":
    main()
