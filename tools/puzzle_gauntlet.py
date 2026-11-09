"""Play the hardest-Lichess-puzzle set through the real LLM chess engine, stopping at the first miss.

    python tools/puzzle_gauntlet.py [--tools-dir DIR] [--model z-ai/glm-5.3-flash] [--budget-usd 2]
                                    [--start 1] [--limit 100] [--no-fail-fast] [--out out/puzzles/run]

The engine is the same process a tournament game uses (engines/llm-chess-engine, provider
openrouter-chat): the player's own tools run before every move and the tool gate refuses a flagged
move once. The harness never picks a move. A puzzle is solved when every solver move equals the
Lichess solution (a checkmating move is also accepted, as on Lichess); a wrong legal move, a
forfeit (bestmove 0000) or an engine error fails it. With fail-fast (the default) the run stops at
the first failed puzzle, like the integration tests.

Spend is read from OpenRouter itself (/key usage before and after), so every call counts; the run
stops before a puzzle once the budget is used up.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

import chess

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "engines" / "llm-chess-engine"))

import lichess_puzzles  # noqa: E402

DEFAULT_MODEL = "z-ai/glm-5.3-flash"
DEFAULT_PROVIDER = "openrouter-chat"
# OpenRouter list price is 0.075 / 0.25 USD per million; the cap keeps a pricier provider out.
MAX_PRICE = {"prompt": 0.2, "completion": 0.7}
MOVE_CLOCK_MS = 15 * 60 * 1000
PUZZLE_HEADER = (
    "Chess puzzle (Lichess puzzle database, rated {rating}). The position comes from a real game. "
    "You play {side}. There is exactly ONE move that wins (or saves the game); every other move fails the "
    "puzzle. After your move the opponent replies with its best defence and you may have to find the next "
    "only move too. Look for forcing moves first: checks, captures and threats, and calculate the "
    "opponent's best reply to each before you choose."
)


@dataclass
class PuzzleResult:
    number: int
    id: str
    rating: int
    passed: bool
    failed_ply: int | None = None
    expected: str = ""
    played: str = ""
    reason: str = ""
    seconds: float = 0.0
    moves: list[dict] = field(default_factory=list)


def player_config(model: str = DEFAULT_MODEL, provider: str = DEFAULT_PROVIDER, effort: str = "high",
                  tools_dir: str | Path | None = None, shared_tools: bool = False) -> dict:
    player = {"name": f"{model} puzzles", "provider": provider, "model": model, "effort": effort, "image": False,
              "maxOutputTokens": 131072, "sharedTools": shared_tools}
    if provider == "openrouter-chat":
        player["maxPrice"] = MAX_PRICE
    if tools_dir:
        player["toolsDir"] = str(tools_dir)
    return player


def engine_cfg(attempt_timeout: int = 900) -> dict:
    # moveCap off: the model thinks until it answers, inside the 15-minute move clock (a 60 s cap cut GLM off
    # mid-calculation on the first live run, 2026-11-09).
    return {"maxAttempts": 3, "attemptTimeoutSeconds": attempt_timeout, "showLegalMoves": True,
            "conversation": False, "limitWait": False, "moveCap": False, "keepReasoning": False}


def open_engine(player: dict, cfg: dict):
    import play_llm_series as pls

    return pls.LlmEngine(player, cfg)


def grade(board: chess.Board, played: str, expected: str) -> tuple[bool, str]:
    """(correct, reason). Lichess accepts the solution move or any move that mates at once."""
    if played == expected:
        return True, ""
    if played in {"", "0000", "(none)"}:
        return False, "no move (forfeit)"
    try:
        move = chess.Move.from_uci(played)
    except ValueError:
        return False, f"unreadable move {played!r}"
    if move not in board.legal_moves:
        return False, f"illegal move {played}"
    board.push(move)
    mate = board.is_checkmate()
    board.pop()
    return (True, "") if mate else (False, f"played {played}, the only winning move is {expected}")


def solve(engine, puzzle: dict, context_dir: Path, log=print) -> PuzzleResult:
    """Play one puzzle through a UCI engine with send()/wait_for()/new_game()."""
    import play_llm_series as pls

    start = time.monotonic()
    board = chess.Board(puzzle["fen"])
    moves = list(puzzle["moves"])
    played_line = [moves[0]]
    board.push_uci(moves[0])
    side = "White" if board.turn == chess.WHITE else "Black"
    result = PuzzleResult(puzzle.get("number", 0), puzzle["id"], puzzle["rating"], passed=False)
    engine.new_game()
    context_dir.mkdir(parents=True, exist_ok=True)
    ctx = context_dir / f"{puzzle['id']}.json"
    ctx.write_text(json.dumps({"memory": "", "header": PUZZLE_HEADER.format(rating=puzzle["rating"], side=side)}),
                   encoding="utf-8")
    engine.send(f"setoption name GameContextFile value {ctx}")
    for index in range(1, len(moves), 2):
        expected = moves[index]
        engine.send(f"position fen {puzzle['fen']} moves {' '.join(played_line)}")
        engine.send(f"go wtime {MOVE_CLOCK_MS} btime {MOVE_CLOCK_MS} winc 0 binc 0")
        try:
            lines = engine.wait_for("bestmove", engine.move_budget_seconds())
        except Exception as exc:  # engine died or timed out: a failed puzzle, never a guessed move
            result.failed_ply, result.expected, result.reason = board.ply(), expected, f"engine error: {exc}"
            break
        played = lines[-1].split()[1] if len(lines[-1].split()) > 1 else ""
        comment, tries, illegal = pls.parse_info(lines)
        ok, why = grade(board, played, expected)
        result.moves.append({"ply": board.ply(), "expected": expected, "played": played, "ok": ok, "tries": tries,
                             "illegal": illegal, "note": pls.parse_note(lines)[:300], "usage": pls.parse_usage(lines)})
        log(f"  puzzle {result.number} {puzzle['id']} ply {board.ply()}: played {played or '-'} "
            f"expected {expected} -> {'ok' if ok else 'WRONG'} (tries {tries})")
        if not ok:
            result.failed_ply, result.expected, result.played, result.reason = board.ply(), expected, played, why
            break
        board.push_uci(played)
        played_line.append(played)
        if board.is_checkmate():
            result.passed = True
            break
        if index + 1 < len(moves):
            board.push_uci(moves[index + 1])
            played_line.append(moves[index + 1])
    else:
        result.passed = True
    result.seconds = round(time.monotonic() - start, 1)
    return result


class Spend:
    """USD used on the OpenRouter key since this object was made (from /key, so every call counts)."""

    API = "https://openrouter.ai/api/v1/key"

    def __init__(self, fetch=None) -> None:
        self.fetch = fetch or self._fetch
        self.start = self.fetch()

    @staticmethod
    def _fetch() -> float:
        import subscription_providers as sp

        key = os.environ.get("OPENROUTER_API_KEY") or sp._user_env("OPENROUTER_API_KEY")
        if not key:
            raise RuntimeError("OPENROUTER_API_KEY is not set")
        request = urllib.request.Request(Spend.API, headers={"Authorization": f"Bearer {key}",
                                                             "User-Agent": "Mozilla/5.0 chess-harness"})
        with urllib.request.urlopen(request, timeout=20) as response:
            data = (json.loads(response.read().decode("utf-8")) or {}).get("data") or {}
        return float(data.get("usage") or 0.0)

    def used(self) -> float:
        return max(0.0, self.fetch() - self.start)


def run(puzzles: list[dict], engine, out_dir: Path, *, fail_fast: bool = True, budget_usd: float | None = None,
        spend: Spend | None = None, log=print) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[PuzzleResult] = []
    stopped = ""
    for puzzle in puzzles:
        if budget_usd is not None and spend is not None and spend.used() >= budget_usd:
            stopped = f"budget of {budget_usd} USD used up"
            break
        result = solve(engine, puzzle, out_dir / "context", log=log)
        results.append(result)
        write_report(out_dir, results, spend, stopped)
        if not result.passed and fail_fast:
            stopped = f"first failure: puzzle {result.number} ({result.id})"
            break
    return write_report(out_dir, results, spend, stopped)


def write_report(out_dir: Path, results: list[PuzzleResult], spend: Spend | None, stopped: str) -> dict:
    report = {"passed": sum(r.passed for r in results), "played": len(results),
              "first_failure": next((asdict(r) for r in results if not r.passed), None),
              "stopped": stopped, "usd": round(spend.used(), 4) if spend else None,
              "results": [asdict(r) for r in results]}
    (out_dir / "results.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tools-dir", type=Path)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--provider", default=DEFAULT_PROVIDER)
    parser.add_argument("--effort", default="high")
    parser.add_argument("--start", type=int, default=1)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--budget-usd", type=float, default=2.0)
    parser.add_argument("--no-fail-fast", action="store_true")
    parser.add_argument("--fixture", type=Path, default=lichess_puzzles.FIXTURE)
    parser.add_argument("--out", type=Path, default=ROOT / "out" / "puzzles" / time.strftime("gauntlet-%Y%m%d-%H%M%S"))
    args = parser.parse_args(argv)
    puzzles = lichess_puzzles.load(args.fixture)[args.start - 1: args.start - 1 + args.limit]
    engine = open_engine(player_config(args.model, args.provider, args.effort, args.tools_dir), engine_cfg())
    try:
        spend = Spend() if args.provider == "openrouter-chat" else None
        report = run(puzzles, engine, args.out, fail_fast=not args.no_fail_fast, budget_usd=args.budget_usd,
                     spend=spend, log=lambda m: print(m, flush=True))
    finally:
        engine.close()
    print(json.dumps({k: report[k] for k in ("passed", "played", "stopped", "usd")}), flush=True)
    return 0 if report["passed"] == len(puzzles) else 1


if __name__ == "__main__":
    sys.exit(main())
