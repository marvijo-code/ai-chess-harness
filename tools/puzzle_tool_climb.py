"""A model builds its own verification tools from nothing until it solves every hard Lichess puzzle.

    python tools/puzzle_tool_climb.py [--model z-ai/glm-5.3-flash] [--budget-usd 5] [--max-rounds 30]
                                      [--out out/puzzles/climb-<time>]

Round 0 is the baseline: the puzzle gauntlet with NO tools at all (not even the harness's shared
ones). With --import-tools <memory repo>/agents the model instead starts from every tournament
player's tools (each copied as <player>-<tool>.py into a read-only imported folder that, like the
harness's shared tools, does not count against its own 8) plus the shared harness tools. After every failed round the SAME model is shown the puzzle it missed (the position, its move,
what its tools printed, and - as Lichess shows after a miss - the solution line and the puzzle's
themes) and writes, rewrites or deletes its own tools. Then the gauntlet starts again from puzzle 1,
fail-fast, so a round passes only when all puzzles pass with the current tools.

Tools follow the tournament contract (tools/player_tools.py: python-chess only, no engine, no
network, no files, 8 s per run, at most 8 files of 8 KB), and on top of that a tool may not name a
position: FEN text, puzzle ids and solution move sequences are rejected, so a tool has to be a
general check, not an answer key. The harness never picks a move.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

import chess

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "engines" / "llm-chess-engine"))

import lichess_puzzles  # noqa: E402
import player_tools  # noqa: E402
import puzzle_gauntlet as pg  # noqa: E402

FEN_LITERAL = re.compile(r"[pnbrqkPNBRQK1-8]{1,8}(?:/[pnbrqkPNBRQK1-8]{1,8}){3,7}")
MAX_AUTHOR_RETRIES = 2
MEMORY_MAX_BYTES = 4000
RATING_RANGE = "2856-2998"
AUTHOR_SYSTEM = ("You are a chess player who writes small python-chess programs that check positions for you. "
                 "Reply with one JSON object only.")

# A worked example in the ai-chess reflection style: a real, runnable skeleton the player extends.
# It is deliberately shallow (depth 3, material only) - the player has to make it deep enough to see
# the quiet move it missed.
WORKED_EXAMPLE = """```python
import json, sys, time, chess
VAL = {chess.PAWN: 100, chess.KNIGHT: 320, chess.BISHOP: 330, chess.ROOK: 500, chess.QUEEN: 900, chess.KING: 0}
payload = json.load(sys.stdin)
board = chess.Board(payload["fen"])
for uci in payload.get("moves") or []:
    board.push(chess.Move.from_uci(uci))
start = time.time()

def ev(b):
    s = sum(VAL[p.piece_type] * (1 if p.color == b.turn else -1) for p in b.piece_map().values())
    return s

def search(b, depth, alpha, beta):
    if time.time() - start > 5.0:
        raise TimeoutError
    if b.is_checkmate():
        return -99999 - depth
    if depth == 0:
        return ev(b)
    best = -10**9
    for move in sorted(b.legal_moves, key=lambda m: b.is_capture(m), reverse=True):
        b.push(move)
        score = -search(b, depth - 1, -beta, -alpha)
        b.pop()
        best = max(best, score)
        alpha = max(alpha, score)
        if alpha >= beta:
            break
    return best

rows = []
for move in board.legal_moves:
    board.push(move)
    try:
        score = -search(board, 3, -10**9, 10**9)
    except TimeoutError:
        board.pop()
        break
    board.pop()
    rows.append((score, move.uci()))
rows.sort(reverse=True)
for score, uci in rows[:5]:
    print("PLAY: %s  eval %d" % (uci, score))
```"""


def load_memory(out_dir: Path) -> str:
    path = out_dir / "MEMORY.md"
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


def save_memory(out_dir: Path, text: str) -> None:
    (out_dir / "MEMORY.md").write_text(text, encoding="utf-8")


def tool_verdict(tools_dir: Path, board: chess.Board, history: list[str], expected: str,
                 include_shared: bool = False, extra_dirs=()) -> tuple[bool, str]:
    """Run the player's tools on one position. (do the tools RECOMMEND `expected`, text for the prompt).

    A tool recommends a move only as the first move of its first "PLAY:" line. A mention anywhere else
    (a "WEAK" line, a pv, a list of replies) is not a find: the player would not be told to play it.
    The verdict is a hit only when at least one tool recommends the winning move and no tool recommends
    a different one, because in play the player sees every tool and a split vote does not point anywhere.
    """
    block = player_tools.run_tools(tools_dir, board, history, include_shared=include_shared, extra_dirs=extra_dirs)
    if not block.any_ran:
        return False, "(you have no tools yet)"
    right, wrong, notes = 0, 0, []
    for result in block.results:
        if not result.ok:
            notes.append(f"[{result.name}] could not run: {result.error}")
            continue
        text = result.output
        shown = text[:player_tools.MAX_OUTPUT_CHARS]    # the player only sees this much in play
        plays = [m for line in shown.splitlines() if line.strip().upper().startswith("PLAY:")
                 for m in re.findall(r"\b([a-h][1-8][a-h][1-8][qrbn]?)\b", line)[:1]]
        if plays and plays[0] == expected:
            right += 1
            notes.append(f"[{result.name}] RECOMMENDS THE WINNING MOVE {expected} (its first PLAY line).")
        elif plays:
            wrong += 1
            notes.append(f"[{result.name}] recommends {plays[0]} - that is NOT the winning move {expected}.")
        elif re.search(r"\b" + re.escape(expected) + r"\b", shown):
            notes.append(f"[{result.name}] only MENTIONS {expected}; it does not recommend it (no PLAY line "
                         "starts with it), so this does not count as found.")
        else:
            notes.append(f"[{result.name}] does not name the winning move.")
        notes.append(f"[{result.name}] output:\n{text}")
    hit = right > 0 and wrong == 0
    if right and wrong:
        notes.insert(0, f"NOT FOUND: your tools disagree ({right} recommend {expected}, {wrong} recommend another "
                        "move). In play you would see both. Make them agree.")
    return hit, "\n".join(notes)


def puzzle_selftest(tools_dir: Path, puzzle: dict, include_shared: bool = False, extra_dirs=()) -> tuple[int, str]:
    """Run the tools on every solver position of the puzzle. (positions whose move was found, text)."""
    board = chess.Board(puzzle["fen"])
    board.push_uci(puzzle["moves"][0])
    history = [puzzle["moves"][0]]
    found, lines = 0, []
    for index in range(1, len(puzzle["moves"]), 2):
        expected = puzzle["moves"][index]
        hit, text = tool_verdict(tools_dir, board, history, expected, include_shared, extra_dirs)
        found += 1 if hit else 0
        side = "White" if board.turn == chess.WHITE else "Black"
        lines.append(f"Position {index} ({side} to move). The winning move is {expected}.")
        lines.append(text)
        board.push_uci(expected)
        history.append(expected)
        if index + 1 < len(puzzle["moves"]):
            board.push_uci(puzzle["moves"][index + 1])
            history.append(puzzle["moves"][index + 1])
    return found, "\n".join(lines)


def answer_key_problem(content: str, puzzles: list[dict]) -> str | None:
    """Why this tool source looks like an answer key rather than a general check (None = fine)."""
    if FEN_LITERAL.search(content):
        return "it contains a position (FEN text); tools must be general checks that work on any position"
    for puzzle in puzzles:
        if puzzle["id"] in content:
            return f"it names puzzle {puzzle['id']}"
        solution = puzzle["moves"][1:]
        for index in range(0, len(solution) - 1):
            pair = solution[index:index + 2]
            if re.search(re.escape(pair[0]) + r"\W{1,6}" + re.escape(pair[1]), content):
                return "it contains a stored move sequence; tools must compute moves from the position"
    return None


def import_tools(agents_dir: Path, dest: Path, puzzles: list[dict], log=print) -> list[dict]:
    """Copy every player's tools (agents/<player>/tools/*.py) into `dest` as <player>-<tool>.py.

    Each one passes the same checks as a new tool (contract, compile, start-position run, answer-key ban);
    one that fails is left out and reported."""
    dest.mkdir(parents=True, exist_ok=True)
    report = []
    for path in sorted(Path(agents_dir).glob("*/tools/*.py")):
        player = path.parent.parent.name
        name = f"{player}-{path.stem}"[:48].rstrip("-")
        content = path.read_text(encoding="utf-8", errors="replace")
        ok, why = player_tools.validate_tool_edit(f"tools/{name}.py", content)
        if ok:
            why = answer_key_problem(content, puzzles) or ""
            ok = not why
        if ok:
            (dest / f"{name}.py").write_text(content, encoding="utf-8")
        report.append({"from": f"{player}/{path.name}", "as": f"{name}.py", "ok": ok, "why": why})
        log(f"import {player}/{path.name} -> {name}.py: {'ok' if ok else 'left out: ' + why}")
    return report


def tool_summary(path: Path) -> str:
    """The tool's first docstring or comment line, for the author prompt (full sources would be too long)."""
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        text = line.strip().strip('"').strip("'").lstrip("#").strip()
        if text and not text.startswith(("import ", "from ")):
            return text[:140]
    return ""


def tool_sources(tools_dir: Path) -> dict[str, str]:
    return {f"tools/{p.name}": p.read_text(encoding="utf-8") for p in sorted(tools_dir.glob("*.py"))}


def failure_story(puzzle: dict, failed: dict, tools_dir: Path, shared_tools: bool = False,
                  extra_dirs: list[Path] | None = None) -> str:
    """The missed puzzle as the model should see it: position, its move, its tools, the solution."""
    board = chess.Board(puzzle["fen"])
    board.push_uci(puzzle["moves"][0])
    solution_line, miss = [], None
    for uci in puzzle["moves"][1:]:
        if board.ply() == failed["failed_ply"]:
            miss = board.copy()
        move = chess.Move.from_uci(uci)
        solution_line.append(board.san(move))
        board.push(move)
    if miss is None:
        raise ValueError(f"puzzle {puzzle['id']} has no ply {failed['failed_ply']}")
    block = player_tools.run_tools(tools_dir, miss, [], include_shared=shared_tools, extra_dirs=extra_dirs or ())
    tools_text = player_tools.render_block(block, miss, []) or "(you had no tools)"
    side = "White" if miss.turn == chess.WHITE else "Black"
    played = failed.get("played") or "no move"
    try:
        played_san = miss.san(chess.Move.from_uci(played))
    except Exception:
        played_san = played
    expected_san = miss.san(chess.Move.from_uci(failed["expected"]))
    return "\n".join([
        f"Puzzle {puzzle['number']} (Lichess rating {puzzle['rating']}, themes: {', '.join(puzzle['themes'])}).",
        f"Position where you went wrong ({side} to move): {miss.fen()}",
        miss.unicode(borders=False, empty_square="."),
        f"You played: {played_san} ({played}). Why it failed: {failed['reason']}.",
        f"The only winning move was {expected_san} ({failed['expected']}). Full solution from the start of the "
        f"puzzle: {' '.join(solution_line)}",
        "What your tools printed on that position:",
        tools_text,
    ])


def author_prompt(story: str, history: list[str], sources: dict[str, str], round_no: int, passed: int,
                  total: int, memory: str = "", self_test: str = "", rejected: list[str] | None = None,
                  borrowed: list[str] | None = None, iteration: int = 1, iterations: int = 4) -> str:
    parts = [
        f"You are solving {total} of the hardest Lichess puzzles (rated {RATING_RANGE}), each with exactly one "
        "winning move at every step. They run in order and the run stops at the first miss. "
        f"Round {round_no}: you solved {passed} in a row, then missed the next one.",
        "",
        "Before every move, the harness runs YOUR verification tools on the position and shows you what they "
        "print. " + ("You also start with the borrowed tools listed below (written by other players; read-only, they "
                     "do not count against your own limit; write a tool with the same name to replace one). "
                     if borrowed else
                     "You start from nothing: there are no tools except the ones you write. ") +
        "No engine, no tablebase, no network: your tools are the only help you get.",
        "",
        "HOW TO FIX THIS (the same loop the ai-chess tournament players use):",
        "1. Read the missed position, the full solution, and what your tools printed on it.",
        "2. Work out WHY you missed it: which candidate move you did not consider, which opponent reply you "
        "did not see. Then decide what general check would have surfaced the winning move.",
        "3. Write, rewrite or delete tools. A heuristic that only applies one rule keeps missing moves: a "
        "QUIET move (no check, no capture) that wins several moves later is found by SEARCHING, not by a rule. "
        "A tool that runs iterative-deepening alpha-beta over legal moves with its own evaluation, quiescence "
        "and mate detection is allowed and is usually the only thing that finds those moves. Deepen it until "
        "it is right.",
        "4. The harness then runs your tools on every position of the missed puzzle, and on every puzzle you "
        "already solved in this run, immediately and shows you the output, "
        f"without spending a puzzle round. You get up to {iterations} tries in this round: if a tool does not "
        "RECOMMEND the winning move (first move of its first PLAY: line; a mention elsewhere does not count, and "
        "no other tool may PLAY a different move), rewrite it and try again. Only when it does does the full run restart.",
        "5. Keep your notebook (MEMORY.md) current: the general lesson for each kind of move you miss, and what "
        "your evaluation must contain. It is shown to you in every round. Merge and sharpen old lessons; do not "
        "just append.",
        "",
        "Tool rules: " + player_tools.TOOL_CONTRACT,
        f"Each run of a tool has {player_tools.RUN_TIMEOUT_SECONDS:.0f} seconds; a slower tool is killed and shows "
        f"nothing. At most {player_tools.MAX_TOOL_FILES} tools, {player_tools.TOOL_MAX_BYTES} bytes each - an edit "
        f"over {player_tools.TOOL_MAX_BYTES} bytes is rejected, so keep it compact. "
        f"Only the first {player_tools.MAX_OUTPUT_CHARS} characters of each tool's output are shown, so print "
        "the most important finding first (your PLAY lines first). A tool must run on the starting position and "
        "print something, or it is rejected. A tool may NOT contain positions (FEN text), puzzle ids or stored "
        "move sequences: it has to compute from the position it is given.",
        "",
        "WORKED EXAMPLE (a shallow search tool: copy its shape, then make the search deep enough and the "
        "evaluation good enough to find quiet winning moves):",
        WORKED_EXAMPLE,
        "",
        "THE PUZZLE YOU MISSED",
        story,
        "",
        "EARLIER MISSES (most recent first)",
        "\n".join(history[-6:]) or "(none)",
        "",
        "YOUR NOTEBOOK (MEMORY.md)",
        memory.strip() or "(empty - start it)",
        "",
        "SELF-TEST OF YOUR TOOLS ON THE POSITION YOU MISSED" if self_test else "",
        self_test,
        "",
    ]
    if borrowed:
        parts += ["BORROWED TOOLS (already running before every move; name: what it says it does)", *borrowed, ""]
    parts += ["YOUR CURRENT TOOLS"]
    parts += [f"--- {path} ---\n{text}" for path, text in sources.items()] or ["(none yet)"]
    parts += [
        "",
        f"This is try {iteration} of {iterations} in this round." if iterations > 1 else "",
        "Reply with ONLY this JSON object:",
        '{"summary": "<what you missed and what the new or changed tool checks>", '
        f'"memory": "<the whole new MEMORY.md, at most {MEMORY_MAX_BYTES} bytes>", "edits": ['
        '{"path": "tools/<name>.py", "content": "<the full python file>"}, {"path": "tools/<name>.py", "delete": true}]}',
        "An edit replaces the whole file. Leave out tools you keep unchanged. Leave out \"memory\" to keep it as is.",
    ]
    if rejected:
        parts += ["", "YOUR LAST REPLY WAS NOT FULLY ACCEPTED:", *rejected,
                  "Reply again with the whole JSON object; fix the rejected tools."]
    return "\n".join(line for line in parts if line != "")


def apply_edits(data: dict, tools_dir: Path, puzzles: list[dict]) -> tuple[list[str], list[str]]:
    """Validate and write the tool edits. (applied paths, rejection reasons)."""
    applied, rejected = [], []
    edits = data.get("edits") or []
    if not isinstance(edits, list):
        return [], ["edits is not a list"]
    for edit in edits:
        if not isinstance(edit, dict) or not isinstance(edit.get("path"), str):
            rejected.append("an edit without a path")
            continue
        path = edit["path"]
        if not player_tools.TOOL_PATH.match(path):
            rejected.append(f"{path[:60]!r} is not tools/<lowercase-name>.py")
            continue
        target = tools_dir / Path(path).name
        if edit.get("delete"):
            if target.exists():
                target.unlink()
                applied.append(f"deleted {path}")
            continue
        content = edit.get("content")
        ok, why = player_tools.validate_tool_edit(path, content)
        if ok:
            why = answer_key_problem(content, puzzles) or ""
            ok = not why
        if not ok:
            rejected.append(f"{path}: {why}")
            continue
        others = [p for p in tools_dir.glob("*.py") if p.name != target.name]
        if len(others) >= player_tools.MAX_TOOL_FILES:
            rejected.append(f"{path}: already {len(others)} tools; delete or merge one first")
            continue
        target.write_text(content, encoding="utf-8")
        applied.append(f"wrote {path} ({len(content.encode('utf-8'))} bytes)")
    return applied, rejected


def make_author(model: str, provider: str, effort: str):
    import subscription_providers as sp

    if provider == "openrouter-chat":
        os.environ["LLM_MAX_PRICE"] = json.dumps(pg.MAX_PRICE)
    client = sp.SubscriptionChessClient(provider, lambda m: None)
    client.model = model
    client.effort = effort
    return client


def missed_position(puzzle: dict, failed_ply: int) -> tuple[chess.Board, list[str]]:
    """The position the player faced when it went wrong, and the moves that led there."""
    board = chess.Board(puzzle["fen"])
    board.push_uci(puzzle["moves"][0])
    history = [puzzle["moves"][0]]
    for index in range(1, len(puzzle["moves"]), 2):
        if board.ply() == failed_ply:
            return board.copy(), list(history)
        board.push_uci(puzzle["moves"][index])
        history.append(puzzle["moves"][index])
        if index + 1 < len(puzzle["moves"]):
            board.push_uci(puzzle["moves"][index + 1])
            history.append(puzzle["moves"][index + 1])
    raise ValueError(f"puzzle {puzzle['id']} has no ply {failed_ply}")


def retro(client, out_dir: Path, tools_dir: Path, story: str, history: list[str], puzzles: list[dict],
          puzzle: dict, failed: dict, round_no: int, passed: int, log=print, borrowed: list[str] | None = None,
          iterations: int = 4, include_shared: bool = False, extra_dirs=()) -> dict:
    """One self-retro: the player reviews the miss, writes tools, and the harness runs them on the spot.

    Same shape as the tournament reflection: a notebook (MEMORY.md) that persists, validated edits, and
    retries. The difference is the self-test: the tools are run on the missed position immediately, so a
    wrong tool is visible in seconds instead of after a full puzzle round.
    """
    from ai_chess_memory import parse_reflection

    record = {"applied": [], "rejected": [], "summary": "", "error": "", "memory_bytes": 0,
              "selftest_found": 0, "selftest_positions": 0, "iterations": 0}
    memory = load_memory(out_dir)
    rejected: list[str] | None = None
    self_test = ""
    expected = failed["expected"]
    positions = len(range(1, len(puzzle["moves"]), 2))
    miss_board, miss_history = missed_position(puzzle, failed["failed_ply"])
    hit = False
    for attempt in range(iterations):
        prompt = author_prompt(story, history, tool_sources(tools_dir), round_no, passed, len(puzzles),
                               memory=memory, self_test=self_test, rejected=rejected, borrowed=borrowed,
                               iteration=attempt + 1, iterations=iterations)
        try:
            text = client.ask_text(AUTHOR_SYSTEM, prompt, 1200)
        except Exception as exc:
            record["error"] = f"{type(exc).__name__}: {exc}"[:300]
            log(f"retro call failed: {record['error']}")
            break
        data, why = parse_reflection(text)
        if data is None:
            rejected = [why]
            record["rejected"].append(why)
            continue
        record["summary"] = " ".join(str(data.get("summary") or "").split())[:600]
        record["iterations"] = attempt + 1
        rejected = []
        new_memory = data.get("memory")
        if isinstance(new_memory, str) and new_memory.strip():
            size = len(new_memory.encode("utf-8"))
            if size <= MEMORY_MAX_BYTES:
                save_memory(out_dir, new_memory)
                memory = new_memory
                record["memory_bytes"] = size
            else:
                rejected.append(f"memory is {size} bytes, over the {MEMORY_MAX_BYTES}-byte cap; keep it shorter")
        applied, bad = apply_edits(data, tools_dir, puzzles)
        record["applied"] += applied
        record["rejected"] += bad
        rejected += bad
        hit, self_test = tool_verdict(tools_dir, miss_board, miss_history, expected, include_shared, extra_dirs)
        record["selftest_found"] = 1 if hit else 0
        record["selftest_positions"] = positions
        # Keep the whole exchange so a live page can show exactly what it was told and what it changed.
        (out_dir / f"round-{round_no:02d}").mkdir(parents=True, exist_ok=True)
        (out_dir / f"round-{round_no:02d}" / f"retro-{attempt + 1}.json").write_text(json.dumps({
            "round": round_no, "try": attempt + 1, "iterations": iterations, "puzzle": puzzle["id"],
            "expected": expected, "played": failed.get("played") or "", "summary": record["summary"],
            "prompt": prompt, "reply": text, "self_test": self_test, "hit": hit,
            "applied": applied, "rejected": bad, "memory": memory,
        }, indent=1), encoding="utf-8")
        log(f"retro try {attempt + 1}/{iterations}: applied {applied or 'nothing'}; "
            f"self-test: tools {'RECOMMEND' if hit else 'do not recommend'} the winning move {expected}")
        # Always test the whole line of the missed puzzle AND every puzzle already solved in this run, and
        # always show the result in the next prompt. Before 2026-11-09 this ran only after an accepted edit
        # that hit the missed move: after a rejected edit the next prompt showed the one missed position
        # alone, so the player kept a tool that still failed the next move of the same puzzle.
        found, full = puzzle_selftest(tools_dir, puzzle, include_shared, extra_dirs)
        record["selftest_found"] = found
        log(f"retro: full self-test on puzzle {puzzle['number']}: {found}/{positions} winning moves recommended")
        broken = []
        for earlier in puzzles[:passed]:
            if earlier["id"] == puzzle["id"]:
                continue
            total = len(range(1, len(earlier["moves"]), 2))
            got, text = puzzle_selftest(tools_dir, earlier, include_shared, extra_dirs)
            if got < total:
                broken.append(f"Puzzle {earlier['number']} (you solved it before): {got}/{total} winning moves "
                              f"recommended.\n{text}")
                log(f"retro: puzzle {earlier['number']} (solved before) is now {got}/{total}")
        record["selftest_regressions"] = len(broken)
        if found == positions and not broken:
            if not rejected:
                break
            self_test = "Your tools recommend every winning move of this puzzle line:\n" + full
            continue
        parts = []
        if found < positions:
            parts.append(f"NOT DONE: your tools recommend {found} of the {positions} winning moves of the puzzle "
                         "you missed. Every position of the line is below:\n" + full)
        if broken:
            parts.append("NOT DONE: your change BROKE a puzzle you had already solved. The run restarts at puzzle 1, "
                         "so you would fail there:\n" + "\n".join(broken))
        self_test = "\n\n".join(parts)
    return record


def climb(puzzles: list[dict], out_dir: Path, *, model: str, provider: str, effort: str, budget_usd: float,
          max_rounds: int, engine_factory=None, author_factory=None, spend=None, log=print,
          imported_dir: Path | None = None, shared_tools: bool = False, retro_iterations: int = 4) -> dict:
    tools_dir = out_dir / "tools"
    tools_dir.mkdir(parents=True, exist_ok=True)
    extra = [imported_dir] if imported_dir is not None else []
    borrowed = [f"{p.stem}: {tool_summary(p)}" for d in extra for p in sorted(Path(d).glob("*.py"))]
    if shared_tools:
        borrowed += [f"{p.stem} (harness): {tool_summary(p)}" for p in sorted(player_tools.SHARED_DIR.glob("*.py"))]
    spend = spend if spend is not None else (pg.Spend() if provider == "openrouter-chat" else None)
    engine_factory = engine_factory or (lambda: pg.open_engine(
        pg.player_config(model, provider, effort, tools_dir, shared_tools=shared_tools, extra_dirs=extra),
        pg.engine_cfg()))
    author = (author_factory or make_author)(model, provider, effort)
    state = {"model": model, "provider": provider, "effort": effort, "puzzles": len(puzzles),
             "budget_usd": budget_usd, "rounds": [], "solved_all": False, "stopped": "",
             "borrowed_tools": len(borrowed), "shared_tools": shared_tools}
    write_state(out_dir, state)
    history: list[str] = []
    for round_no in range(max_rounds + 1):
        if spend is not None and spend.used() >= budget_usd:
            state["stopped"] = f"budget of {budget_usd} USD used up"
            break
        snapshot = out_dir / f"round-{round_no:02d}"
        if snapshot.exists():
            shutil.rmtree(snapshot)
        shutil.copytree(tools_dir, snapshot / "tools")
        log(f"round {round_no}: {len(list(tools_dir.glob('*.py')))} own tools, {len(borrowed)} borrowed; "
            "gauntlet from puzzle 1")
        engine = engine_factory()
        try:
            report = pg.run(puzzles, engine, snapshot, fail_fast=True, budget_usd=budget_usd, spend=spend, log=log)
        finally:
            engine.close()
        entry = {"round": round_no, "tools": sorted(p.name for p in tools_dir.glob("*.py")),
                 "passed": report["passed"], "played": report["played"], "stopped": report["stopped"],
                 "usd_total": report["usd"]}
        state["rounds"].append(entry)
        write_state(out_dir, state)
        log(f"round {round_no}: solved {report['passed']} in a row ({report['stopped'] or 'all done'}), "
            f"spent {report['usd']} USD so far")
        if report["passed"] == len(puzzles):
            state["solved_all"] = True
            break
        failed = report["first_failure"]
        if failed is None:      # stopped by the budget before a puzzle, not by a miss
            state["stopped"] = report["stopped"]
            break
        if round_no == max_rounds:
            state["stopped"] = f"{max_rounds} tool rounds used"
            break
        puzzle = next(p for p in puzzles if p["id"] == failed["id"])
        story = failure_story(puzzle, failed, tools_dir, shared_tools, extra)
        record = retro(author, out_dir, tools_dir, story, history, puzzles, puzzle, failed, round_no,
                       report["passed"], log=log, borrowed=borrowed, iterations=retro_iterations,
                       include_shared=shared_tools, extra_dirs=extra)
        entry["author"] = record
        history.insert(0, f"round {round_no}: puzzle {puzzle['number']} ({', '.join(puzzle['themes'][:4])}): played "
                          f"{failed.get('played') or '-'}, needed {failed['expected']}")
        write_state(out_dir, state)
    write_state(out_dir, state)
    return state


def write_state(out_dir: Path, state: dict) -> None:
    (out_dir / "climb.json").write_text(json.dumps(state, indent=1), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", default=pg.DEFAULT_MODEL)
    parser.add_argument("--provider", default=pg.DEFAULT_PROVIDER)
    parser.add_argument("--effort", default="high")
    parser.add_argument("--budget-usd", type=float, required=True)
    parser.add_argument("--max-rounds", type=int, default=30)
    parser.add_argument("--retro-iterations", type=int, default=4,
                        help="tool rewrites per round, each self-tested on the missed position for free")
    parser.add_argument("--fixture", type=Path, default=lichess_puzzles.FIXTURE)
    parser.add_argument("--import-tools", type=Path,
                        help="a memory repo agents/ folder: start from every player's tools plus the shared ones")
    parser.add_argument("--seed-own", type=Path, help="copy these .py tools into the model's own tools first")
    parser.add_argument("--out", type=Path, default=ROOT / "out" / "puzzles" / time.strftime("climb-%Y%m%d-%H%M%S"))
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)

    def log(message: str) -> None:
        print(time.strftime("%H:%M:%S ") + message, flush=True)

    puzzles = lichess_puzzles.load(args.fixture)
    imported = None
    if args.import_tools:
        imported = args.out / "imported"
        report = import_tools(args.import_tools, imported, puzzles, log=log)
        (args.out / "imported.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    if args.seed_own:
        (args.out / "tools").mkdir(parents=True, exist_ok=True)
        for path in sorted(args.seed_own.glob("*.py")):
            shutil.copy2(path, args.out / "tools" / path.name)
    state = climb(puzzles, args.out, model=args.model, provider=args.provider,
                  effort=args.effort, budget_usd=args.budget_usd, max_rounds=args.max_rounds, log=log,
                  imported_dir=imported, shared_tools=bool(args.import_tools),
                  retro_iterations=args.retro_iterations)
    print(json.dumps({"solved_all": state["solved_all"], "stopped": state["stopped"],
                      "rounds": [(r["round"], r["passed"]) for r in state["rounds"]]}), flush=True)
    return 0 if state["solved_all"] else 1


if __name__ == "__main__":
    sys.exit(main())
