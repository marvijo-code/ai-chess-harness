import argparse
import io
import json
import re
import time
from collections import Counter, defaultdict
from pathlib import Path

import chess.pgn


ROOT = Path(__file__).resolve().parents[1]
LEARNER = "Codex-chess-learner"
DEFAULT_MEMORY = ROOT / "engines" / "codex-chess-learner" / "MEMORY.md"
DEFAULT_KB = ROOT / "engines" / "codex-chess-learner" / "knowledgebase" / "live-match-lessons.md"
DEFAULT_JSON = ROOT / "engines" / "codex-chess-learner" / "knowledgebase" / "live-match-lessons.json"
ENGINE_LOG_DIR = ROOT / "out" / "codex-chess-logs"

STARTED_RE = re.compile(r"Started game\s+(?P<game>\d+)\s+of\s+(?P<total>\d+)")
FINISHED_RE = re.compile(
    r"Finished game\s+(?P<game>\d+)\s+\((?P<white>.+?)\s+vs\s+(?P<black>.+?)\):\s+"
    r"(?P<result>1-0|0-1|1/2-1/2|\*)\s+\{(?P<reason>[^}]*)\}"
)
MEMORY_START = "<!-- learner-autolearn:start -->"
MEMORY_END = "<!-- learner-autolearn:end -->"
THINKING_MARKERS = (
    "thread started:",
    "decision prompt:",
    "decision comment:",
    "illegal Codex move",
    "invalid Codex response",
    "invalid model",
    "codex turn error",
    "Codex app-server turn failed",
    "bestmove ",
)


def read_stdout_reasons(path: Path | None) -> tuple[dict[int, dict], int | None]:
    if path is None or not path.exists():
        return {}, None
    text = path.read_text(encoding="utf-8", errors="replace")
    total = None
    for match in STARTED_RE.finditer(text):
        total = int(match.group("total"))
    reasons = {}
    for match in FINISHED_RE.finditer(text):
        data = match.groupdict()
        game_no = int(data["game"])
        reasons[game_no] = {
            "white": data["white"],
            "black": data["black"],
            "result": data["result"],
            "reason": data["reason"],
        }
    return reasons, total


def read_games(path: Path) -> list[dict]:
    if not path.exists():
        return []
    games = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        while True:
            game = chess.pgn.read_game(handle)
            if game is None:
                break
            game_no = len(games) + 1
            board = game.board()
            san = []
            uci = []
            for move in game.mainline_moves():
                try:
                    san.append(board.san(move))
                    uci.append(move.uci())
                    board.push(move)
                except Exception:
                    break
            games.append(
                {
                    "game": game_no,
                    "white": game.headers.get("White", "White"),
                    "black": game.headers.get("Black", "Black"),
                    "result": game.headers.get("Result", "*"),
                    "termination": game.headers.get("Termination", ""),
                    "plies": len(uci),
                    "san": san,
                    "uci": uci,
                }
            )
    return games


def learner_points(game: dict) -> float | None:
    result = game["result"]
    if result == "1/2-1/2":
        return 0.5
    if result == "1-0":
        return 1.0 if game["white"] == LEARNER else 0.0 if game["black"] == LEARNER else None
    if result == "0-1":
        return 1.0 if game["black"] == LEARNER else 0.0 if game["white"] == LEARNER else None
    return None


def reason_class(reason: str) -> str:
    lower = reason.lower()
    if "illegal move" in lower:
        return "illegal move"
    if "repetition" in lower:
        return "threefold repetition"
    if "mate" in lower:
        return "mate"
    if "time" in lower:
        return "time"
    if "draw" in lower:
        return "draw"
    return lower or "unknown"


def side_making_illegal(reason: str) -> str | None:
    lower = reason.lower()
    if "white makes an illegal move" in lower:
        return "white"
    if "black makes an illegal move" in lower:
        return "black"
    return None


def format_line(san: list[str], max_plies: int = 12) -> str:
    tokens = []
    for index, move in enumerate(san[:max_plies]):
        if index % 2 == 0:
            tokens.append(f"{index // 2 + 1}. {move}")
        else:
            tokens[-1] += f" {move}"
    return " ".join(tokens)


def merge_reasons(games: list[dict], reasons: dict[int, dict]) -> list[dict]:
    merged = []
    for game in games:
        copy = dict(game)
        reason = reasons.get(game["game"], {})
        copy["reason"] = reason.get("reason") or game.get("termination") or ""
        copy["reason_class"] = reason_class(copy["reason"])
        merged.append(copy)
    return merged


def summarize(games: list[dict], total: int | None, pgn: Path, stdout: Path | None) -> dict:
    completed = [game for game in games if game["result"] in {"1-0", "0-1", "1/2-1/2"}]
    points = [learner_points(game) for game in completed]
    points = [point for point in points if point is not None]
    reason_counts = Counter(game["reason_class"] for game in completed)
    illegal_losses = []
    illegal_wins = []
    zero_ply_engine_failures = []
    mate_losses = []
    mate_wins = []
    time_losses = []
    time_wins = []
    repetition_draws = []
    signatures = defaultdict(lambda: {"games": 0, "score": 0.0, "draws": 0, "losses": 0, "wins": 0})

    for game in completed:
        point = learner_points(game)
        if point is None:
            continue
        if game["plies"] > 0:
            signature = format_line(game["san"], max_plies=10)
            entry = signatures[signature]
            entry["games"] += 1
            entry["score"] += point
            if point == 0.5:
                entry["draws"] += 1
            elif point == 1.0:
                entry["wins"] += 1
            else:
                entry["losses"] += 1

        illegal_side = side_making_illegal(game["reason"])
        if illegal_side:
            if game["plies"] == 0:
                zero_ply_engine_failures.append(game)
                continue
            learner_side = "white" if game["white"] == LEARNER else "black" if game["black"] == LEARNER else ""
            if illegal_side == learner_side:
                illegal_losses.append(game)
            else:
                illegal_wins.append(game)
        if game["reason_class"] == "mate":
            if point == 1.0:
                mate_wins.append(game)
            elif point == 0.0:
                mate_losses.append(game)
        if game["reason_class"] == "time":
            if point == 1.0:
                time_wins.append(game)
            elif point == 0.0:
                time_losses.append(game)
        if game["reason_class"] == "threefold repetition":
            repetition_draws.append(game)

    repeated_lines = sorted(signatures.items(), key=lambda item: (-item[1]["games"], item[0]))[:8]
    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source_pgn": str(pgn),
        "source_stdout": str(stdout) if stdout else "",
        "completed_games": len(completed),
        "scheduled_games": total,
        "learner_points": sum(points),
        "learner_score_percent": round((sum(points) / len(points) * 100), 2) if points else 0.0,
        "reason_counts": dict(reason_counts),
        "illegal_losses": [brief_game(game) for game in illegal_losses[-12:]],
        "illegal_wins": [brief_game(game) for game in illegal_wins[-12:]],
        "zero_ply_engine_failures": [brief_game(game) for game in zero_ply_engine_failures[-12:]],
        "mate_losses": [brief_game(game) for game in mate_losses[-12:]],
        "mate_wins": [brief_game(game) for game in mate_wins[-12:]],
        "time_losses": [brief_game(game) for game in time_losses[-12:]],
        "time_wins": [brief_game(game) for game in time_wins[-12:]],
        "repetition_draws": len(repetition_draws),
        "repeated_opening_lines": [
            {
                "line": line,
                "games": data["games"],
                "learner_score": data["score"],
                "wins": data["wins"],
                "draws": data["draws"],
                "losses": data["losses"],
            }
            for line, data in repeated_lines
        ],
    }


def brief_game(game: dict) -> dict:
    return {
        "game": game["game"],
        "white": game["white"],
        "black": game["black"],
        "result": game["result"],
        "plies": game["plies"],
        "reason": game["reason"],
        "opening": format_line(game["san"], max_plies=10),
    }


def render_markdown(summary: dict) -> str:
    lines = [
        "# Live Match Lessons",
        "",
        f"Generated: {summary['generated_at']}",
        f"Completed games: {summary['completed_games']} / {summary.get('scheduled_games') or '?'}",
        f"Learner score: {summary['learner_points']} ({summary['learner_score_percent']}%)",
        "",
        "## Result Shape",
    ]
    for reason, count in sorted(summary["reason_counts"].items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"- {reason}: {count}")
    lines += [
        "",
        "## Durable Move Rules",
        "- Copy `uci` exactly from `legal_moves`; never output `0000` while legal moves exist.",
        "- The main failure mode is repeated positions: avoid moves listed as repetition risks unless a draw is the only safe result.",
        "- A 15-ply threefold loop is not learning; choose a legal capture, check, pawn break, or development move that changes the position when available.",
        "- Manage the clock while still choosing a move intentionally; there is no fallback or client-picked move.",
        "- In winning endings, convert material with forcing moves and pawn promotion plans; do not shuffle the king until the clock collapses.",
    ]

    if summary["repeated_opening_lines"]:
        lines += ["", "## Repeated Lines To Avoid"]
        for item in summary["repeated_opening_lines"]:
            lines.append(
                f"- {item['games']} games, score {item['learner_score']}: {item['line'] or '[empty line]'}"
            )

    if summary.get("zero_ply_engine_failures"):
        lines += [
            "",
            "## Engine Availability Failures",
            "- Zero-ply illegal losses are engine/app-server failures, not chess-position lessons. Do not learn an empty opening line from them.",
        ]
        for game in summary["zero_ply_engine_failures"]:
            lines.append(f"- Game {game['game']} as {learner_side(game)}: {game['reason']}")

    if summary["illegal_losses"]:
        lines += [
            "",
            "## Learner Illegal-Move Losses",
            "- A `0000` illegal move after real plies usually means the model timed out or returned invalid JSON three consecutive times. Treat it as a clock/format failure, not a chess tactic.",
        ]
        for game in summary["illegal_losses"]:
            lines.append(f"- Game {game['game']} as {learner_side(game)}: {game['reason']} after {game['plies']} plies; {game['opening']}")

    if summary["mate_losses"]:
        lines += ["", "## Learner Mate Losses"]
        for game in summary["mate_losses"]:
            lines.append(f"- Game {game['game']} as {learner_side(game)}: {game['reason']} after {game['plies']} plies; {game['opening']}")
    if summary.get("time_losses"):
        lines += [
            "",
            "## Learner Time Losses",
            "- Time losses are move-selection failures. Improve time management, but still choose a move intentionally from the position.",
            "- Prefer a forcing capture, check, passed-pawn push, king move toward passed pawns, or simple recapture when that is the best evaluated plan.",
        ]
        for game in summary["time_losses"]:
            lines.append(f"- Game {game['game']} as {learner_side(game)}: {game['reason']} after {game['plies']} plies; {game['opening']}")
    lines.append("")
    return "\n".join(lines)


def learner_side(game: dict) -> str:
    if game["white"] == LEARNER:
        return "White"
    if game["black"] == LEARNER:
        return "Black"
    return "unknown"


def parse_log_timestamp(line: str) -> tuple[str, str]:
    match = re.match(r"\[(?P<ts>[^\]]+)\]\s*(?P<text>.*)", line)
    if not match:
        return "", line
    return match.group("ts"), match.group("text")


def bot_from_thread_line(line: str) -> tuple[str, str]:
    lower = line.lower()
    if str((ROOT / "engines" / "codex-chess-learner")).lower() in lower:
        return "learner", LEARNER
    if "context=" in lower:
        return "baseline", "Codex-chess"
    return "unknown", "Unknown"


def side_from_line(line: str, current_side: str = "") -> str:
    match = re.search(r"\bside=(white|black)\b", line, flags=re.I)
    if match:
        return match.group(1).lower()
    match = re.search(r"\bside_to_move[\"']?\s*[:=]\s*[\"']?(white|black)\b", line, flags=re.I)
    if match:
        return match.group(1).lower()
    return current_side


def classify_log_line(line: str) -> str:
    lower = line.lower()
    if "thread started" in lower:
        return "setup"
    if "decision prompt" in lower:
        return "prompt"
    if "decision comment" in lower:
        return "comment"
    if (
        "illegal codex move" in lower
        or "invalid codex response" in lower
        or "invalid model" in lower
        or "codex turn error" in lower
        or "codex app-server turn failed" in lower
        or "usagelimitexceeded" in lower
    ):
        return "repair"
    if "bestmove" in lower:
        return "move"
    return "log"


def collect_engine_thinking(log_dir: Path, since_epoch: float | None = None) -> list[dict]:
    if not log_dir.exists():
        return []
    logs = []
    for path in log_dir.glob("codex-chess-*.log"):
        try:
            stat = path.stat()
        except OSError:
            continue
        if since_epoch is not None and stat.st_mtime < since_epoch:
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue

        current_bot = "unknown"
        current_bot_label = "Unknown"
        current_side = ""
        entries = []
        for line in lines:
            if "thread started:" in line:
                current_bot, current_bot_label = bot_from_thread_line(line)
            current_side = side_from_line(line, current_side)
            if not any(marker in line for marker in THINKING_MARKERS):
                continue
            timestamp, text = parse_log_timestamp(line)
            entries.append(
                {
                    "timestamp": timestamp,
                    "kind": classify_log_line(text),
                    "bot": current_bot,
                    "bot_label": current_bot_label,
                    "side": current_side,
                    "text": text,
                    "file": path.name,
                }
            )
        if entries:
            logs.append({"file": path.name, "first_timestamp": entries[0]["timestamp"], "entries": entries})
    logs.sort(key=lambda item: (item["first_timestamp"], item["file"]))
    return logs


def build_thinking_archive(games: list[dict], pgn: Path, stdout: Path | None, log_dir: Path) -> dict:
    since_candidates = []
    for path in (pgn, stdout):
        if path and path.exists():
            try:
                since_candidates.append(path.stat().st_ctime - 120)
            except OSError:
                pass
    since_epoch = min(since_candidates) if since_candidates else None
    engine_logs = collect_engine_thinking(log_dir, since_epoch)
    game_items = [
        {
            "game": game["game"],
            "white": game["white"],
            "black": game["black"],
            "result": game["result"],
            "reason": game.get("reason", ""),
            "plies": game["plies"],
            "opening": format_line(game["san"], max_plies=10),
            "entries": [],
        }
        for game in games
    ]

    entries = [entry for log_item in engine_logs for entry in log_item["entries"]]
    entries.sort(key=lambda entry: (entry.get("timestamp", ""), entry.get("file", "")))
    game_index = -1
    pending: list[dict] = []
    for entry in entries:
        text = entry.get("text", "")
        if "decision prompt:" in text and "fen=rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1" in text:
            game_index += 1
            if game_index < len(game_items) and pending:
                game_items[game_index]["entries"].extend(pending)
            pending = []
        if game_index < 0:
            pending.append(entry)
        elif game_index < len(game_items):
            game_items[game_index]["entries"].append(entry)
    for game in game_items:
        game["entries"].sort(key=lambda entry: (entry.get("timestamp", ""), entry.get("file", "")))

    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source_pgn": str(pgn),
        "source_stdout": str(stdout) if stdout else "",
        "engine_log_dir": str(log_dir),
        "engine_log_files": len(engine_logs),
        "assignment": "FastChess restart=on produces one engine log per side; logs are assigned to games in chronological pairs.",
        "games": game_items,
    }


def render_thinking_markdown(archive: dict) -> str:
    lines = [
        "# Per-Game Bot Thinking",
        "",
        f"Generated: {archive['generated_at']}",
        f"Source PGN: {archive['source_pgn']}",
        f"Engine log files: {archive['engine_log_files']}",
        "",
    ]
    for game in archive["games"]:
        entries = game.get("entries", [])
        lines += [
            f"## Game {game['game']}: {game['white']} vs {game['black']} {game['result']}",
            f"- Reason: {game.get('reason') or 'in progress'}",
            f"- Plies: {game['plies']}",
            f"- Stored thinking entries: {len(entries)}",
        ]
        for entry in entries[:20]:
            side = f" {entry['side']}" if entry.get("side") else ""
            lines.append(f"- {entry.get('timestamp', '')} [{entry.get('bot_label', 'Unknown')}{side} {entry.get('kind', 'log')}] {entry.get('text', '')}")
        if len(entries) > 20:
            lines.append(f"- ... {len(entries) - 20} more entries in JSON archive")
        lines.append("")
    return "\n".join(lines)


def default_thinking_json(pgn: Path) -> Path:
    return pgn.with_name(f"{pgn.stem}-thinking.json")


def default_thinking_md(pgn: Path) -> Path:
    return pgn.with_name(f"{pgn.stem}-thinking.md")


def update_memory(memory_path: Path, summary: dict) -> None:
    memory_path.parent.mkdir(parents=True, exist_ok=True)
    current = memory_path.read_text(encoding="utf-8", errors="replace") if memory_path.exists() else "# Codex-chess-learner Memory\n"
    block = "\n".join(
        [
            MEMORY_START,
            "## Autolearn Summary",
            f"- Last updated: {summary['generated_at']}",
            f"- Current match score: {summary['learner_points']} / {summary['completed_games']} ({summary['learner_score_percent']}%).",
            f"- Result reasons: {', '.join(f'{key}={value}' for key, value in sorted(summary['reason_counts'].items()))}.",
            "- Apply `knowledgebase/live-match-lessons.md` before choosing moves.",
            "- Avoid threefold repetition loops unless drawing is the only practical outcome.",
            "- Manage the clock while still choosing a move intentionally; there is no fallback or client-picked move.",
            "- Never return a move outside `legal_moves`; never return `0000` while legal moves exist.",
            MEMORY_END,
        ]
    )
    if MEMORY_START in current and MEMORY_END in current:
        updated = re.sub(
            re.escape(MEMORY_START) + r".*?" + re.escape(MEMORY_END),
            block,
            current,
            flags=re.S,
        )
    else:
        updated = current.rstrip() + "\n\n" + block + "\n"
    memory_path.write_text(updated, encoding="utf-8")


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + f".{time.strftime('%H%M%S')}.tmp")
    for attempt in range(8):
        try:
            temp.write_text(text, encoding="utf-8")
            temp.replace(path)
            return
        except PermissionError:
            time.sleep(0.25 * (attempt + 1))
    path.write_text(text, encoding="utf-8")
    if temp.exists():
        try:
            temp.unlink()
        except OSError:
            pass


def run_once(args: argparse.Namespace) -> dict:
    reasons, total = read_stdout_reasons(args.stdout)
    games = merge_reasons(read_games(args.pgn), reasons)
    summary = summarize(games, total, args.pgn, args.stdout)
    write_atomic(args.output, render_markdown(summary))
    write_atomic(args.json, json.dumps(summary, indent=2))
    thinking_json = args.thinking_json or default_thinking_json(args.pgn)
    thinking_md = args.thinking_md or default_thinking_md(args.pgn)
    thinking_archive = build_thinking_archive(games, args.pgn, args.stdout, args.engine_log_dir)
    write_atomic(thinking_json, json.dumps(thinking_archive, indent=2))
    write_atomic(thinking_md, render_thinking_markdown(thinking_archive))
    if not args.no_memory_update:
        update_memory(args.memory, summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Update learner knowledgebase from FastChess PGN and result stdout.")
    parser.add_argument("--pgn", type=Path, required=True)
    parser.add_argument("--stdout", type=Path)
    parser.add_argument("--output", type=Path, default=DEFAULT_KB)
    parser.add_argument("--json", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--memory", type=Path, default=DEFAULT_MEMORY)
    parser.add_argument("--thinking-json", type=Path)
    parser.add_argument("--thinking-md", type=Path)
    parser.add_argument("--engine-log-dir", type=Path, default=ENGINE_LOG_DIR)
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval", type=float, default=10.0)
    parser.add_argument("--no-memory-update", action="store_true")
    args = parser.parse_args()

    while True:
        summary = run_once(args)
        print(
            f"Updated learner knowledgebase: {summary['completed_games']} games, "
            f"score {summary['learner_points']} ({summary['learner_score_percent']}%)",
            flush=True,
        )
        if not args.watch:
            return
        scheduled = summary.get("scheduled_games")
        if scheduled and summary["completed_games"] >= scheduled:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
