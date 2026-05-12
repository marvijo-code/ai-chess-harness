import argparse
import json
import re
from collections import Counter
from pathlib import Path

import chess.pgn


FINISHED_RE = re.compile(
    r"Finished game\s+(?P<game>\d+)\s+\((?P<white>.+?)\s+vs\s+(?P<black>.+?)\):\s+"
    r"(?P<result>1-0|0-1|1/2-1/2|\*)\s+\{(?P<reason>[^}]*)\}"
)
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def clean_console_text(text: str) -> str:
    text = ANSI_RE.sub("", text)
    text = text.replace("\r", "\n")
    return "\n".join(line.rstrip() for line in text.splitlines())


def parse_stdout_reasons(path: Path) -> dict[int, dict]:
    if not path or not path.exists():
        return {}
    text = clean_console_text(path.read_text(encoding="utf-8", errors="replace"))
    reasons = {}
    for match in FINISHED_RE.finditer(text):
        game_no = int(match.group("game"))
        reasons[game_no] = {
            "game": game_no,
            "white": match.group("white"),
            "black": match.group("black"),
            "result": match.group("result"),
            "reason": match.group("reason"),
        }
    return reasons


def parse_pgn(path: Path) -> list[dict]:
    games = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        game_no = 1
        while True:
            game = chess.pgn.read_game(handle)
            if game is None:
                break
            games.append(
                {
                    "game": game_no,
                    "white": game.headers.get("White", ""),
                    "black": game.headers.get("Black", ""),
                    "result": game.headers.get("Result", "*"),
                    "termination": game.headers.get("Termination", ""),
                    "plies": sum(1 for _ in game.mainline_moves()),
                }
            )
            game_no += 1
    return games


def classify(reason: str, termination: str) -> str:
    text = f"{reason} {termination}".lower()
    if "3-fold" in text or "threefold" in text:
        return "threefold repetition"
    if "mate" in text:
        return "mate"
    if "illegal" in text:
        return "illegal move"
    if "time" in text or "flag" in text:
        return "time"
    if "resign" in text:
        return "resignation"
    if "stalemate" in text:
        return "stalemate"
    if "adjud" in text:
        return "adjudication"
    return reason or termination or "unknown"


def build_summary(pgn_path: Path, stdout_path: Path | None) -> list[dict]:
    stdout_reasons = parse_stdout_reasons(stdout_path) if stdout_path else {}
    rows = []
    for game in parse_pgn(pgn_path):
        reason_info = stdout_reasons.get(game["game"], {})
        reason = reason_info.get("reason", "")
        row = dict(game)
        row["reason"] = reason or game["termination"] or "unknown"
        row["reason_class"] = classify(reason, game["termination"])
        rows.append(row)
    return rows


def print_markdown(rows: list[dict]) -> None:
    counts = Counter(row["reason_class"] for row in rows)
    print(f"Completed games: {len(rows)}")
    print("Reasons:")
    for reason, count in counts.most_common():
        print(f"- {reason}: {count}")
    print()
    print("| Game | White | Black | Result | Plies | Reason | PGN termination |")
    print("| ---: | --- | --- | --- | ---: | --- | --- |")
    for row in rows:
        print(
            f"| {row['game']} | {row['white']} | {row['black']} | {row['result']} | "
            f"{row['plies']} | {row['reason']} | {row['termination']} |"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize FastChess game-ending reasons from PGN and stdout.")
    parser.add_argument("--pgn", type=Path, required=True)
    parser.add_argument("--stdout", type=Path, default=None, help="Redirected FastChess stdout containing Finished game lines.")
    parser.add_argument("--json", type=Path, default=None, help="Optional JSON output path.")
    args = parser.parse_args()

    rows = build_summary(args.pgn, args.stdout)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print_markdown(rows)


if __name__ == "__main__":
    main()
