import argparse
import io
import json
import re
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path

import chess
import chess.pgn


ROOT = Path(__file__).resolve().parents[1]
LEARNER = "Codex-chess-learner"
DEFAULT_MEMORY = ROOT / "engines" / "codex-chess-learner" / "MEMORY.md"
DEFAULT_KB = ROOT / "engines" / "codex-chess-learner" / "knowledgebase" / "live-match-lessons.md"
DEFAULT_JSON = ROOT / "engines" / "codex-chess-learner" / "knowledgebase" / "live-match-lessons.json"
DEFAULT_STRATEGY_KB = ROOT / "engines" / "codex-chess-learner" / "knowledgebase" / "strategy-lessons.md"
DEFAULT_STRATEGY_JSON = ROOT / "engines" / "codex-chess-learner" / "knowledgebase" / "strategy-lessons.json"
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
PIECE_VALUES = {
    chess.PAWN: 100,
    chess.KNIGHT: 300,
    chess.BISHOP: 300,
    chess.ROOK: 500,
    chess.QUEEN: 900,
    chess.KING: 0,
}
EVIDENCE_TYPES = {
    "hanging_checking_piece": {
        "description": "a checking move left the moved piece capturable by the enemy king",
    },
    "undefended_forcing_piece": {
        "description": "a forcing move left the moved piece immediately capturable",
    },
    "missed_king_capture": {
        "description": "a legal king capture was available but another move was chosen",
    },
    "material_swing": {
        "description": "the opponent reply caused a material balance drop",
    },
    "failed_conversion": {
        "description": "the learner had a material edge but did not win",
    },
    "pawn_promotion_failure": {
        "description": "the learner had a pawn one step from promotion but did not win",
    },
    "mate_loss": {
        "description": "the learner lost by mate",
    },
    "time_loss": {
        "description": "the learner lost on time",
    },
    "repetition_draw": {
        "description": "the learner drew by repetition",
    },
}


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
            initial_fen = board.fen()
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
                    "initial_fen": initial_fen,
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


def learner_color(game: dict) -> bool | None:
    if game["white"] == LEARNER:
        return chess.WHITE
    if game["black"] == LEARNER:
        return chess.BLACK
    return None


def color_label(color: bool) -> str:
    return "White" if color == chess.WHITE else "Black"


def piece_value(piece: chess.Piece | None) -> int:
    if piece is None:
        return 0
    return PIECE_VALUES.get(piece.piece_type, 0)


def material_balance(board: chess.Board, color: bool) -> int:
    total = 0
    for piece in board.piece_map().values():
        value = piece_value(piece)
        total += value if piece.color == color else -value
    return total


def is_pawn_one_step_from_promotion(board: chess.Board, color: bool) -> bool:
    target_rank = 6 if color == chess.WHITE else 1
    for square, piece in board.piece_map().items():
        if piece.color == color and piece.piece_type == chess.PAWN and chess.square_rank(square) == target_rank:
            return True
    return False


def king_capture_moves(board: chess.Board) -> list[chess.Move]:
    king_square = board.king(board.turn)
    if king_square is None:
        return []
    captures = []
    for move in board.legal_moves:
        if move.from_square == king_square and board.is_capture(move):
            captures.append(move)
    return captures


def legal_captures_to_square(board: chess.Board, square: chess.Square) -> list[chess.Move]:
    return [move for move in board.legal_moves if move.to_square == square and board.is_capture(move)]


def make_strategy_event(
    category: str,
    game: dict,
    side: bool,
    ply: int,
    move: str,
    san: str,
    fen_before: str,
    fen_after: str,
    detail: str,
) -> dict:
    return {
        "category": category,
        "game": game["game"],
        "side": color_label(side),
        "ply": ply,
        "move": move,
        "san": san,
        "fen_before": fen_before,
        "fen_after": fen_after,
        "detail": detail,
    }


def strategy_evidence_key(event: dict) -> str:
    return "|".join(
        str(event.get(part, ""))
        for part in ("source_pgn", "category", "game", "ply", "move", "fen_before", "detail")
    )


def load_previous_strategy_summary(path: Path | None) -> dict | None:
    if path is None or not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def comparable_summary(summary: dict, ignored_keys: set[str]) -> dict:
    return {key: value for key, value in summary.items() if key not in ignored_keys}


def preserve_generated_at_if_unchanged(summary: dict, previous: dict | None, ignored_keys: set[str]) -> dict:
    if not previous or not previous.get("generated_at"):
        return summary
    if comparable_summary(summary, ignored_keys) != comparable_summary(previous, ignored_keys):
        return summary
    stable = dict(summary)
    for key in ignored_keys:
        if key in previous:
            stable[key] = previous[key]
    return stable


def preserve_strategy_generated_at_if_no_new_evidence(summary: dict, previous: dict | None) -> dict:
    if not previous or not previous.get("generated_at") or summary.get("new_evidence_count"):
        return summary
    stable = dict(summary)
    for key in ("generated_at", "source_pgn", "source_stdout", "concepts", "concept_synthesis"):
        if key in previous:
            stable[key] = previous[key]
    return stable


def parse_json_object(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I).strip()
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if not match:
            raise
        return json.loads(match.group(0))


def concept_synthesis_prompt(summary: dict) -> str:
    existing = summary.get("concepts", [])[:12]
    evidence = summary.get("new_evidence", [])[:30]
    payload = {
        "task": (
            "Update a chess learner's own generalized concepts from self-play evidence. "
            "Do not memorize exact FENs, game numbers, or moves as answers. Infer concepts like a learner asking "
            "why a move became bad, which piece disappeared, how the position value changed, and what future move "
            "features should be valued more or less."
        ),
        "constraints": [
            "Return JSON only.",
            "Concepts must be general and reusable across positions.",
            "Use evidence_refs to cite observations, but do not create rules that only replay a cited move.",
            "Prefer value adjustments such as penalize loose moved pieces, reward preserving material, or reduce search time in low-clock states when evidence supports them.",
            "Do not use Stockfish, engine PVs, or outside chess databases.",
        ],
        "existing_concepts": existing,
        "new_self_play_evidence": evidence,
        "output_schema": {
            "concepts": [
                {
                    "name": "short model-discovered concept name",
                    "trigger": "position or move feature that activates the concept",
                    "value_adjustment": "how to increase or decrease move value when the trigger appears",
                    "why": "brief observable explanation from evidence",
                    "confidence": "number from 0 to 1",
                    "evidence_refs": ["category game/ply/move refs"],
                }
            ]
        },
    }
    return json.dumps(payload, indent=2)


def synthesize_strategy_concepts(summary: dict, model: str, effort: str, timeout: int) -> tuple[list[dict], dict]:
    if not summary.get("new_evidence"):
        return summary.get("concepts", []), summary.get("concept_synthesis") or {
            "status": "unchanged",
            "message": "no new self-play evidence",
        }

    query_script = Path.home() / ".codex" / "skills" / "codex-app-server-query" / "scripts" / "query_app_server.py"
    if not query_script.exists():
        return summary.get("concepts", []), {
            "status": "unavailable",
            "message": f"Codex app-server query script not found: {query_script}",
        }

    temp_path = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as handle:
            handle.write(concept_synthesis_prompt(summary))
            temp_path = Path(handle.name)
        command = [
            sys.executable,
            str(query_script),
            "--model",
            model,
            "--effort",
            effort,
            "--cwd",
            str(ROOT),
            "--timeout",
            str(timeout),
            "--prompt-file",
            str(temp_path),
        ]
        completed = subprocess.run(
            command,
            cwd=str(ROOT),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout + 30,
            check=False,
        )
        if completed.returncode != 0:
            return summary.get("concepts", []), {
                "status": "failed",
                "message": completed.stderr.strip() or completed.stdout.strip()[:500],
            }
        outer = json.loads(completed.stdout)
        inner = parse_json_object(str(outer.get("text") or ""))
        concepts = inner.get("concepts", [])
        if not isinstance(concepts, list):
            raise ValueError("concepts was not a list")
        normalized = []
        for concept in concepts[:12]:
            if isinstance(concept, dict) and concept.get("name"):
                normalized.append(concept)
        return normalized, {
            "status": "ok",
            "message": f"synthesized {len(normalized)} generalized concepts from {summary['new_evidence_count']} new observations",
            "model": model,
            "effort": effort,
        }
    except Exception as exc:
        return summary.get("concepts", []), {
            "status": "failed",
            "message": f"{type(exc).__name__}: {exc}",
        }
    finally:
        if temp_path:
            try:
                temp_path.unlink()
            except OSError:
                pass


def add_game_level_event(events: list[dict], category: str, game: dict, detail: str, fen: str = "") -> None:
    color = learner_color(game)
    if color is None:
        return
    events.append(
        {
            "category": category,
            "game": game["game"],
            "side": color_label(color),
            "ply": game["plies"],
            "move": "",
            "san": "",
            "fen_before": fen,
            "fen_after": fen,
            "detail": detail,
        }
    )


def extract_strategy_events(game: dict) -> list[dict]:
    color = learner_color(game)
    if color is None:
        return []

    events: list[dict] = []
    board = chess.Board(game.get("initial_fen") or chess.STARTING_FEN)
    max_learner_balance = material_balance(board, color)
    saw_pawn_near_promotion = is_pawn_one_step_from_promotion(board, color)
    pending_learner_move: dict | None = None

    for index, move_text in enumerate(game["uci"], start=1):
        try:
            move = chess.Move.from_uci(move_text)
        except ValueError:
            break
        if move not in board.legal_moves:
            break

        side_to_move = board.turn
        is_learner_move = side_to_move == color
        fen_before = board.fen()
        san = board.san(move)
        was_capture = board.is_capture(move)
        safe_king_captures = king_capture_moves(board) if is_learner_move else []
        board.push(move)
        fen_after = board.fen()
        balance_after = material_balance(board, color)
        max_learner_balance = max(max_learner_balance, balance_after)
        saw_pawn_near_promotion = saw_pawn_near_promotion or is_pawn_one_step_from_promotion(board, color)

        if not is_learner_move:
            if pending_learner_move and balance_after <= pending_learner_move["balance_after"] - 300:
                events.append(
                    make_strategy_event(
                        "material_swing",
                        game,
                        color,
                        pending_learner_move["ply"],
                        pending_learner_move["move"],
                        pending_learner_move["san"],
                        pending_learner_move["fen_before"],
                        pending_learner_move["fen_after"],
                        f"opponent reply {san} shifted material by {pending_learner_move['balance_after'] - balance_after} centipawns",
                    )
                )
            pending_learner_move = None
            continue

        if safe_king_captures and move not in safe_king_captures:
            captures = ", ".join(capture.uci() for capture in safe_king_captures[:4])
            events.append(
                make_strategy_event(
                    "missed_king_capture",
                    game,
                    color,
                    index,
                    move_text,
                    san,
                    fen_before,
                    fen_after,
                    f"legal king capture candidate(s) were available: {captures}",
                )
            )

        moved_piece = board.piece_at(move.to_square)
        checking_move = board.is_check()
        destination_defended = board.is_attacked_by(side_to_move, move.to_square)
        reply_captures = legal_captures_to_square(board, move.to_square)
        reply_king_captures = [
            reply
            for reply in reply_captures
            if board.piece_at(reply.from_square) and board.piece_at(reply.from_square).piece_type == chess.KING
        ]
        if moved_piece and checking_move and reply_king_captures and not destination_defended:
            events.append(
                make_strategy_event(
                    "hanging_checking_piece",
                    game,
                    color,
                    index,
                    move_text,
                    san,
                    fen_before,
                    fen_after,
                    f"checking {moved_piece.symbol()} can be captured by king move(s): {', '.join(reply.uci() for reply in reply_king_captures)}",
                )
            )
        elif moved_piece and (checking_move or was_capture) and reply_captures and not destination_defended:
            events.append(
                make_strategy_event(
                    "undefended_forcing_piece",
                    game,
                    color,
                    index,
                    move_text,
                    san,
                    fen_before,
                    fen_after,
                    f"forcing move leaves {moved_piece.symbol()} capturable by {', '.join(reply.uci() for reply in reply_captures[:4])}",
                )
            )

        pending_learner_move = {
            "ply": index,
            "move": move_text,
            "san": san,
            "fen_before": fen_before,
            "fen_after": fen_after,
            "balance_after": balance_after,
        }

    point = learner_points(game)
    reason = game.get("reason", "")
    if point == 0.0 and reason_class(reason) == "mate":
        add_game_level_event(events, "mate_loss", game, f"lost by mate after {game['plies']} plies")
    if point == 0.0 and reason_class(reason) == "time":
        add_game_level_event(events, "time_loss", game, f"lost on time after {game['plies']} plies")
    if point == 0.5 and reason_class(reason) == "threefold repetition":
        add_game_level_event(events, "repetition_draw", game, "drew by repetition instead of changing the position")
    if point is not None and point < 1.0 and max_learner_balance >= 500:
        add_game_level_event(
            events,
            "failed_conversion",
            game,
            f"highest material edge was at least {max_learner_balance} centipawns but result was {game['result']}",
        )
    if point is not None and point < 1.0 and saw_pawn_near_promotion:
        add_game_level_event(events, "pawn_promotion_failure", game, "had a pawn one step from promotion but did not win")

    return events


def build_strategy_lesson_summary(
    games: list[dict],
    pgn: Path,
    stdout: Path | None,
    generated_at: str | None = None,
    previous: dict | None = None,
) -> dict:
    completed = [game for game in games if game["result"] in {"1-0", "0-1", "1/2-1/2"}]
    events = []
    for game in completed:
        for event in extract_strategy_events(game):
            item = dict(event)
            item["source_pgn"] = str(pgn)
            events.append(item)
    observations: dict[str, dict] = {}
    seen_keys: list[str] = []
    seen_key_set: set[str] = set()
    concepts = previous.get("concepts", []) if previous and isinstance(previous.get("concepts"), list) else []
    concept_synthesis = previous.get("concept_synthesis", {}) if previous and isinstance(previous.get("concept_synthesis"), dict) else {}
    if previous:
        for key in previous.get("evidence_keys", []):
            if isinstance(key, str) and key not in seen_key_set:
                seen_keys.append(key)
                seen_key_set.add(key)
        for observation in previous.get("observations", previous.get("lessons", [])):
            category = observation.get("category")
            if category not in EVIDENCE_TYPES:
                continue
            evidence = observation.get("evidence", [])
            if not isinstance(evidence, list):
                evidence = []
            observations[category] = {
                "category": category,
                "description": EVIDENCE_TYPES[category]["description"],
                "evidence_count": int(observation.get("evidence_count") or 0),
                "evidence": evidence[:6],
            }
            if not previous.get("evidence_keys"):
                for event in evidence:
                    key = strategy_evidence_key(event)
                    if key not in seen_key_set:
                        seen_keys.append(key)
                        seen_key_set.add(key)

    new_events = []
    for event in events:
        key = strategy_evidence_key(event)
        if key in seen_key_set:
            continue
        seen_keys.append(key)
        seen_key_set.add(key)
        new_events.append(event)
        category = event["category"]
        entry = observations.setdefault(
            category,
            {
                "category": category,
                "description": EVIDENCE_TYPES[category]["description"],
                "evidence_count": 0,
                "evidence": [],
            },
        )
        entry["evidence_count"] += 1
        if len(entry["evidence"]) < 6:
            entry["evidence"].append(event)

    priority = {category: index for index, category in enumerate(EVIDENCE_TYPES)}
    observation_items = sorted(observations.values(), key=lambda item: (-item["evidence_count"], priority[item["category"]]))[:12]
    return {
        "generated_at": generated_at or time.strftime("%Y-%m-%d %H:%M:%S"),
        "source_pgn": str(pgn),
        "source_stdout": str(stdout) if stdout else "",
        "completed_games": len(completed),
        "observation_count": len(observation_items),
        "category_counts": {observation["category"]: observation["evidence_count"] for observation in observation_items},
        "evidence_keys": seen_keys[-1000:],
        "new_evidence": new_events[:40],
        "new_evidence_count": len(new_events),
        "observations": observation_items,
        "concepts": concepts,
        "concept_synthesis": concept_synthesis,
    }


def render_strategy_markdown(summary: dict) -> str:
    lines = [
        "# Strategy Lessons",
        "",
        f"Generated: {summary['generated_at']}",
        f"Completed games scanned: {summary['completed_games']}",
        "",
        "This file stores model-discovered concepts from self-play evidence. It must not be treated as memorized move answers.",
    ]

    synthesis = summary.get("concept_synthesis", {})
    if synthesis:
        lines += ["", "## Concept Synthesis", f"- status: {synthesis.get('status', 'unknown')}"]
        if synthesis.get("message"):
            lines.append(f"- message: {synthesis['message']}")

    concepts = summary.get("concepts", [])
    lines += ["", "## Discovered Concepts"]
    if not concepts:
        lines.append("- No model-discovered concepts yet.")
    for concept in concepts[:12]:
        name = concept.get("name", "unnamed concept")
        adjustment = concept.get("value_adjustment") or concept.get("value_update") or ""
        trigger = concept.get("trigger", "")
        confidence = concept.get("confidence", "")
        parts = [f"- {name}"]
        if confidence != "":
            parts.append(f"(confidence {confidence})")
        if trigger:
            parts.append(f"trigger: {trigger}")
        if adjustment:
            parts.append(f"value adjustment: {adjustment}")
        lines.append("; ".join(parts))

    lines += ["", "## Evidence For Reflection"]
    observations = summary.get("observations", [])
    if not observations:
        lines.append("- No self-play observations detected yet.")
    for observation in observations:
        lines.append(f"- {observation['category']} ({observation['evidence_count']} evidence): {observation['description']}")
        for event in observation["evidence"][:2]:
            move = f" move {event['move']}" if event.get("move") else ""
            lines.append(
                f"  evidence: game {event['game']} ply {event['ply']} as {event['side']}{move}: {event['detail']}"
            )
    lines.append("")
    return "\n".join(lines)


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
            "- Apply model-discovered concepts from `knowledgebase/strategy-lessons.md` as generic value adjustments, not as memorized move answers.",
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
    if updated != current:
        memory_path.write_text(updated, encoding="utf-8")


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            if path.read_text(encoding="utf-8", errors="replace") == text:
                return
        except OSError:
            pass
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
    pgn = args.pgn.resolve()
    stdout = args.stdout.resolve() if args.stdout else None
    reasons, total = read_stdout_reasons(stdout)
    games = merge_reasons(read_games(pgn), reasons)
    previous_summary = load_previous_strategy_summary(args.json)
    summary = summarize(games, total, pgn, stdout)
    run_generated_at = summary["generated_at"]
    summary = preserve_generated_at_if_unchanged(
        summary,
        previous_summary,
        {"generated_at", "source_pgn", "source_stdout"},
    )
    write_atomic(args.output, render_markdown(summary))
    write_atomic(args.json, json.dumps(summary, indent=2))
    previous_strategy = load_previous_strategy_summary(args.strategy_json)
    strategy_summary = build_strategy_lesson_summary(
        games,
        pgn,
        stdout,
        generated_at=run_generated_at,
        previous=previous_strategy,
    )
    if args.no_concept_synthesis:
        if strategy_summary.get("new_evidence"):
            strategy_summary["concept_synthesis"] = {
                "status": "disabled",
                "message": "concept synthesis disabled for this run",
            }
    else:
        concepts, synthesis = synthesize_strategy_concepts(
            strategy_summary,
            args.concept_model,
            args.concept_effort,
            args.concept_timeout,
        )
        strategy_summary["concepts"] = concepts
        strategy_summary["concept_synthesis"] = synthesis
    strategy_summary = preserve_strategy_generated_at_if_no_new_evidence(strategy_summary, previous_strategy)
    write_atomic(args.strategy_output, render_strategy_markdown(strategy_summary))
    write_atomic(args.strategy_json, json.dumps(strategy_summary, indent=2))
    thinking_json = args.thinking_json or default_thinking_json(pgn)
    thinking_md = args.thinking_md or default_thinking_md(pgn)
    thinking_archive = build_thinking_archive(games, pgn, stdout, args.engine_log_dir)
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
    parser.add_argument("--strategy-output", type=Path, default=DEFAULT_STRATEGY_KB)
    parser.add_argument("--strategy-json", type=Path, default=DEFAULT_STRATEGY_JSON)
    parser.add_argument("--concept-model", default="gpt-5.3-codex")
    parser.add_argument("--concept-effort", default="medium")
    parser.add_argument("--concept-timeout", type=int, default=180)
    parser.add_argument("--no-concept-synthesis", action="store_true")
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
