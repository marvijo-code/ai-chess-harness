"""Ask every tournament model the round 2 trap position, N times each, and say who loses the queen.

Position (after 37.Qg4+): Black to move, in check. The knight on f5 shields the
queen on d7. Ng7 loses the queen to Qxd7; Kh8 is safe.

Each model sees exactly the production prompt: legal moves, computed facts and the
shared tool's output. No engine, no hints.

    python tools/queen_trap_bench.py --runs 3
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
import time
from pathlib import Path

import chess

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "engines" / "llm-chess-engine"))

import subscription_providers as sp  # noqa: E402

FEN = "r4rk1/pbpq1p1p/1p3p2/1P1p1n2/3P2QP/2B1P1P1/P4PB1/R4RK1 b - - 1 19"

# Routes we may spend without a new approval: the two local subscriptions and the
# OpenCode Go plan. Metered OpenRouter and the Gemini CLI log-in are left out.
MODELS = [
    ("Sonnet 5.5", "claude", "claude-sonnet-5-5"),
    ("Haiku 5.5", "claude", "claude-haiku-5-5"),
    ("DeepSeek V4.1 Flash", "opencode-go", "deepseek-v4.1-flash"),
    ("GLM 5.3 Flash", "opencode-go", "glm-5.3-flash"),
]


def _loader():
    spec = importlib.util.spec_from_file_location("lpc", ROOT / "tools" / "player-tools" / "loose-piece-check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


LPC = _loader()


def queen_loss(board_after: chess.Board) -> int:
    """Pawns of Black material White can win right now (queen focused)."""
    worst = 0
    for square in chess.SQUARES:
        piece = board_after.piece_at(square)
        if piece is None or piece.color != chess.BLACK:
            continue
        if board_after.attackers(chess.WHITE, square):
            worst = max(worst, LPC.exchange_gain(board_after, square, chess.WHITE))
    return worst


def judge(move_uci: str) -> tuple[str, str]:
    board = chess.Board(FEN)
    if not move_uci or move_uci == "0000":
        return "no reply", "provider failed or forfeited"
    try:
        move = chess.Move.from_uci(move_uci)
    except ValueError:
        return "bad reply", "not a move"
    if move not in board.legal_moves:
        return "bad reply", "illegal"
    san = board.san(move)
    before_queen = board.pieces(chess.QUEEN, chess.BLACK)
    board.push(move)
    after_queen = board.pieces(chess.QUEEN, chess.BLACK)
    loss = queen_loss(board)
    if before_queen and not after_queen:
        return "LOST QUEEN", san
    if loss >= 5:
        return "LOST QUEEN", f"{san} (material {loss})"
    if loss >= 2:
        return "lost material", f"{san} (-{loss})"
    return "safe", san


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--only", help="substring of the model name to run")
    parser.add_argument("--effort", default="high")
    parser.add_argument("--history-uci", help="comma separated moves that lead to the trap, for in-context runs")
    parser.add_argument("--clock-ms", type=int, help="clock for the side to move (Black) at that point")
    parser.add_argument("--provider", help="ad hoc route, e.g. openrouter-chat")
    parser.add_argument("--model-id", help="ad hoc model id, e.g. openai/gpt-6-luna")
    args = parser.parse_args()

    models = list(MODELS)
    if args.provider and args.model_id:
        label = f"{args.model_id} ({args.provider})"
        models = [(label, args.provider, args.model_id)]
    rows = []
    for name, provider, model in models:
        if args.only and args.only.lower() not in name.lower():
            continue
        for run in range(1, args.runs + 1):
            client = sp.SubscriptionChessClient(provider, lambda _m: None)
            client.tools_dir = None          # shared tools only, as in a tournament game
            client.effort = args.effort
            client.model = model
            started = time.monotonic()
            history = [u for u in (args.history_uci or "").split(",") if u]
            clock = args.clock_ms or 600000
            try:
                move, comment = client.choose_move(
                    chess.Board(FEN), {"wtime": 600000, "btime": clock, "winc": 10000, "binc": 10000}, history)
            except Exception as exc:         # noqa: BLE001 - report, never crash the sweep
                rows.append((name, run, "ERROR", f"{type(exc).__name__}: {exc}"[:80], 0, 0.0))
                continue
            verdict, detail = judge(move)
            if verdict == "no reply":
                why = (comment or "no comment").strip().splitlines()[0][:70]
                detail = why
            rows.append((name, run, verdict, detail, client.last_report.get("think_ms") or 0,
                         time.monotonic() - started))
            print(f"{name} run {run}: {verdict} - {detail} "
                  f"({time.monotonic() - started:.0f}s)", flush=True)

    print("\n=== SUMMARY ===")
    print(f"{'model':22s} {'runs':5s} {'lost queen':10s} {'lost material':13s} {'safe':5s}")
    for name, _p, _m in models:
        mine = [r for r in rows if r[0] == name]
        if not mine:
            continue
        q = sum(1 for r in mine if r[2] == "LOST QUEEN")
        mat = sum(1 for r in mine if r[2] == "lost material")
        safe = sum(1 for r in mine if r[2] == "safe")
        dead = sum(1 for r in mine if r[2] in {"no reply", "bad reply", "ERROR"})
        tag = f"  ({dead} unavailable/bad)" if dead else ""
        print(f"{name:22s} {len(mine):<5d} {q:<10d} {mat:<13d} {safe:<5d}{tag}")
    print("\nMoves played:")
    for name, run, verdict, detail, think, _wall in rows:
        print(f"  {name:22s} run {run}: {verdict:13s} {detail}  ({think/1000:.0f}s think)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
