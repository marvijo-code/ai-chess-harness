"""Focused Playbook-chess tests (PRD 165-171, Validation 77)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import chess
import chess.pgn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engines" / "playbook-chess"))
sys.path.insert(0, str(ROOT / "tools"))

import playbook_chess_uci as pb  # noqa: E402
import playbook_trainer as trainer  # noqa: E402
import run_playbook_climb as climb  # noqa: E402


def test_parse_playbook_weights_and_malformed_lines():
    text = """
# heading prose
- material.pawn = 100
- king.ring_attack_penalty = 14 — TWIC: king attacks decide 50.0% of games.
- not a weight line at all
- broken.line = not_a_number
- search.draw_contempt=45
plain prose line = 7
"""
    weights = pb.parse_playbook_text(text)
    assert weights["material.pawn"] == 100
    assert weights["king.ring_attack_penalty"] == 14
    assert weights["search.draw_contempt"] == 45
    # malformed/unknown lines fall back to defaults without raising
    assert weights["material.queen"] == pb.DEFAULT_PLAYBOOK["material.queen"]


def test_missing_playbook_uses_defaults(tmp_path):
    book = pb.Playbook(tmp_path / "missing.md")
    assert book["material.rook"] == pb.DEFAULT_PLAYBOOK["material.rook"]


def test_queen_captures_undefended_piece_regression():
    # wisdom-chess pruned "queen takes undefended bishop" as an unreasonable
    # capture and lost to Stockfish depth 1; Playbook-chess must take it.
    board = chess.Board("4k3/8/8/1b6/8/8/8/1Q2K3 w - - 0 1")
    move = pb.pick_move(board, None, 4)
    assert move.uci() == "b1b5"


def test_mate_in_one_found():
    board = chess.Board("6k1/5ppp/8/8/8/8/5PPP/R5K1 w - - 0 1")
    move = pb.pick_move(board, None, 3)
    assert move.uci() == "a1a8"


def test_draw_contempt_sign():
    # White up a rook: a draw must score negative for the side that is ahead.
    board = chess.Board("4k3/8/8/8/8/8/8/R3K3 w - - 0 1")
    assert pb.draw_score(board) < 0
    board_black_turn = chess.Board("4k3/8/8/8/8/8/8/R3K3 b - - 0 1")
    assert pb.draw_score(board_black_turn) >= 0


def test_static_exchange_free_and_defended():
    free = chess.Board("4k3/8/8/3p4/4P3/8/8/4K3 w - - 0 1")
    assert pb.static_exchange(free, chess.Move.from_uci("e4d5")) > 0
    defended = chess.Board("4k3/8/2p5/3p4/4P3/8/8/3QK3 w - - 0 1")
    # QxP on d5 with pawn c6 defending: losing exchange for the queen.
    assert pb.static_exchange(defended, chess.Move.from_uci("d1d5")) < 0


def _write_gate_pgn(path: Path, result: str, termination: str, evals: list[int]) -> None:
    game = chess.pgn.Game()
    game.headers["White"] = "Playbook-chess"
    game.headers["Black"] = "Stockfish depth 1"
    game.headers["Result"] = result
    game.headers["Termination"] = termination
    board = chess.Board()
    node = game
    idx = 0
    for move in board.legal_moves:
        break
    moves = ["e2e4", "e7e5", "g1f3", "b8c6", "f1c4", "g8f6", "d2d3", "f8c5",
             "b1c3", "d7d6", "c1e3", "c8e6", "d1d2", "d8d7", "e1g1", "e8g8"]
    for uci in moves:
        move = chess.Move.from_uci(uci)
        if move not in board.legal_moves:
            break
        node = node.add_variation(move)
        if board.turn == chess.WHITE and idx < len(evals):
            node.comment = f"Depth 6, eval {evals[idx]:+d}cp, nodes 100 (playbook v1). Test."
            idx += 1
        board.push(move)
    path.write_text(str(game) + "\n\n", encoding="utf-8")


def _seed_playbook(path: Path) -> None:
    path.write_text(
        "# Test playbook\n\n"
        "- meta.version = 1\n"
        "- search.draw_contempt = 30 — rationale text preserved.\n"
        "- conversion.simplify_bonus = 4\n"
        "- conversion.king_activity = 12\n"
        "- pawns.passed_per_rank = 14\n"
        "- search.base_movetime_ms = 8000\n"
        "- search.min_depth = 4\n"
        "\n## Training log\n",
        encoding="utf-8",
    )


def _fake_progress(path: Path) -> Path:
    progress = {
        "decisive_games": 1000,
        "completed_issues": [1600, 1601],
        "theme_totals": {
            "passed_pawn": 520,
            "queen_trade_ahead": 185,
            "king_activation": 180,
            "material_swing": 974,
        },
    }
    path.write_text(json.dumps(progress), encoding="utf-8")
    return path


def test_trainer_bounded_update_with_twic_citation(tmp_path):
    playbook = tmp_path / "playbook.md"
    _seed_playbook(playbook)
    pgn = tmp_path / "game1.pgn"
    # Drew from a winning position: failed_conversion + repetition_draw.
    _write_gate_pgn(pgn, "1/2-1/2", "THREEFOLD REPETITION", [350, 360, 370, 380, 390, 400, 410, 420])
    progress = _fake_progress(tmp_path / "progress.json")

    summary = trainer.train_round([pgn], playbook, fresh_sample=0, twic_progress_path=progress)
    assert summary["changed"]
    assert "failed_conversion" in summary["classes"]
    assert "repetition_draw" in summary["classes"]
    # Bounded steps only.
    for key, (old, new) in summary["adjustments"].items():
        lo, hi, step = trainer.BOUNDS[key]
        assert lo <= new <= hi
        assert abs(new - old) <= step + 1e-9

    text = playbook.read_text(encoding="utf-8")
    assert "- meta.version = 2" in text
    assert "rationale text preserved" in text  # in-place edits keep prose
    assert "TWIC issues 1600-1601" in text
    assert "passed_pawn" in text
    # draw_contempt got the failed_conversion + repetition_draw bump (single bounded step).
    assert "- search.draw_contempt = 40" in text


def test_trainer_ignores_wins(tmp_path):
    playbook = tmp_path / "playbook.md"
    _seed_playbook(playbook)
    pgn = tmp_path / "game2.pgn"
    _write_gate_pgn(pgn, "1-0", "CHECKMATE", [100, 200, 300])
    progress = _fake_progress(tmp_path / "progress.json")
    summary = trainer.train_round([pgn], playbook, fresh_sample=0, twic_progress_path=progress)
    assert not summary["changed"]
    assert summary["failures"] == 0
    assert "- search.draw_contempt = 30" in playbook.read_text(encoding="utf-8")


def test_trainer_respects_bounds_cap(tmp_path):
    playbook = tmp_path / "playbook.md"
    playbook.write_text(
        "- meta.version = 3\n- search.draw_contempt = 80\n\n## Training log\n",
        encoding="utf-8",
    )
    pgn = tmp_path / "game3.pgn"
    _write_gate_pgn(pgn, "1/2-1/2", "THREEFOLD REPETITION", [200, 200, 200])
    progress = _fake_progress(tmp_path / "progress.json")
    summary = trainer.train_round([pgn], playbook, fresh_sample=0, twic_progress_path=progress)
    # Already at the cap: no draw_contempt change is possible.
    assert "search.draw_contempt" not in summary["adjustments"]
    assert "- search.draw_contempt = 80" in playbook.read_text(encoding="utf-8")


def test_climb_state_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(climb, "STATE_DIR", tmp_path)
    monkeypatch.setattr(climb, "STATE_PATH", tmp_path / "climb-state.json")
    state = climb.default_state()
    state["current_depth"] = 3
    state["max_passed_depth"] = 2
    climb.save_state(state)
    loaded = climb.load_state()
    assert loaded["current_depth"] == 3
    assert loaded["max_passed_depth"] == 2


def test_checkpoint_allowlist_guard():
    assert climb.staged_paths_allowed(
        ["engines/playbook-chess/playbook.md", "tools/playbook_trainer.py"]
    )
    assert not climb.staged_paths_allowed(["engines/playbook-chess/playbook.md", "tools/live_pgn_viewer.py"])
    assert not climb.staged_paths_allowed(["engines/codex-chess-zero/zero_research.py"])
    assert not climb.staged_paths_allowed(["chess-harness.config.json"])
    # PRD files carry unrelated in-flight hunks and stay out of climb checkpoints.
    assert not climb.staged_paths_allowed(["PRD.md"])


def test_gate_won_rules():
    assert climb.gate_won("1-0", "CHECKMATE", True)
    assert climb.gate_won("0-1", "CHECKMATE", False)
    assert not climb.gate_won("1-0", "CHECKMATE", False)
    assert not climb.gate_won("1/2-1/2", "THREEFOLD REPETITION", True)
    # Adjudicated material edge never passes a gate (PRD 169).
    assert not climb.gate_won("1-0", "max plies 240 material adjudication (+350cp white)", True)
