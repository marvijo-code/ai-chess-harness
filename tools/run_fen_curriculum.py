import argparse
import asyncio
import json
import random
import re
import shutil
import socket
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import chess
import websockets
from websockets.exceptions import ConnectionClosed

from harness_config import config_value


ROOT = Path(__file__).resolve().parents[1]
LEARNER_DIR = ROOT / "engines" / "codex-chess-learner"
MEMORY_PATH = LEARNER_DIR / "MEMORY.md"
KNOWLEDGEBASE_DIR = LEARNER_DIR / "knowledgebase"
DEFAULT_JSON = KNOWLEDGEBASE_DIR / "fen-curriculum-results.json"
DEFAULT_MD = KNOWLEDGEBASE_DIR / "fen-curriculum-lessons.md"
MEMORY_START = "<!-- fen-curriculum:start -->"
MEMORY_END = "<!-- fen-curriculum:end -->"
LETTERS = ("a", "b", "c", "d")
PIECE_NAMES = {
    chess.PAWN: "Pawn",
    chess.KNIGHT: "Knight",
    chess.BISHOP: "Bishop",
    chess.ROOK: "Rook",
    chess.QUEEN: "Queen",
    chess.KING: "King",
}


def resolve_codex_command() -> str:
    windows_cmd = Path.home() / "AppData" / "Roaming" / "npm" / "codex.cmd"
    if windows_cmd.exists():
        return str(windows_cmd)
    found = shutil.which("codex.cmd") or shutil.which("codex.exe") or shutil.which("codex")
    if found:
        return found
    raise FileNotFoundError("Could not find Codex CLI. Expected codex.cmd or codex on PATH.")


@dataclass(frozen=True)
class FenQuestion:
    id: str
    fen: str
    prompt: str
    choices: tuple[str, str, str, str]
    answer_index: int
    concept: str
    explanation: str

    @property
    def answer_letter(self) -> str:
        return LETTERS[self.answer_index]

    @property
    def answer_text(self) -> str:
        return self.choices[self.answer_index]

    def public_payload(self) -> dict:
        return {
            "id": self.id,
            "fen": self.fen,
            "question": self.prompt,
            "choices": {letter: self.choices[index] for index, letter in enumerate(LETTERS)},
            "concept": self.concept,
        }


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def piece_label(piece: chess.Piece | None) -> str:
    if piece is None:
        return "Empty"
    color = "White" if piece.color == chess.WHITE else "Black"
    return f"{color} {PIECE_NAMES[piece.piece_type]}"


def make_choice_set(answer: str, distractors: list[str], rng: random.Random) -> tuple[tuple[str, str, str, str], int]:
    seen = {answer}
    options = [answer]
    for item in distractors:
        if item not in seen:
            seen.add(item)
            options.append(item)
        if len(options) == 4:
            break
    if len(options) != 4:
        raise ValueError(f"not enough distinct choices for answer {answer!r}")
    rng.shuffle(options)
    return tuple(options), options.index(answer)


def q(
    question_id: str,
    board: chess.Board,
    prompt: str,
    answer: str,
    distractors: list[str],
    concept: str,
    explanation: str,
    rng: random.Random,
) -> FenQuestion:
    choices, answer_index = make_choice_set(answer, distractors, rng)
    return FenQuestion(question_id, board.fen(), prompt, choices, answer_index, concept, explanation)


def occupancy_question(question_id: str, fen: str, square_name: str, rng: random.Random) -> FenQuestion:
    board = chess.Board(fen)
    piece = piece_label(board.piece_at(chess.parse_square(square_name)))
    distractors = [
        "White Knight",
        "Black Knight",
        "White Bishop",
        "Black Bishop",
        "White Rook",
        "Black Rook",
        "White Queen",
        "Black Queen",
        "White Pawn",
        "Black Pawn",
        "Empty",
    ]
    return q(
        question_id,
        board,
        f"What is on {square_name}?",
        piece,
        [item for item in distractors if item != piece],
        "square occupancy",
        f"{square_name} contains {piece}. Read FEN ranks from 8 down to 1 and files from a to h; digits skip empty squares.",
        rng,
    )


def side_question(question_id: str, fen: str, rng: random.Random) -> FenQuestion:
    board = chess.Board(fen)
    answer = "White to move" if board.turn == chess.WHITE else "Black to move"
    return q(
        question_id,
        board,
        "Which side is to move?",
        answer,
        ["Black to move", "White to move", "Both sides to move", "No side to move"],
        "side to move",
        f"The second FEN field is {'w' if board.turn == chess.WHITE else 'b'}, so {answer.lower()}.",
        rng,
    )


def check_question(question_id: str, fen: str, rng: random.Random) -> FenQuestion:
    board = chess.Board(fen)
    if board.is_check():
        answer = "White king is in check" if board.turn == chess.WHITE else "Black king is in check"
    else:
        answer = "No king is in check"
    return q(
        question_id,
        board,
        "What is the check status in this position?",
        answer,
        [
            "White king is in check",
            "Black king is in check",
            "No king is in check",
            "Both kings are in check",
        ],
        "check status",
        f"python-chess legal interpretation reports: {answer}. Check depends on attacks on the side-to-move king.",
        rng,
    )


def material_count_question(
    question_id: str,
    fen: str,
    color: chess.Color,
    piece_type: chess.PieceType,
    rng: random.Random,
) -> FenQuestion:
    board = chess.Board(fen)
    color_name = "White" if color == chess.WHITE else "Black"
    piece_name = PIECE_NAMES[piece_type]
    count = len(board.pieces(piece_type, color))
    answer = str(count)
    distractors = [str(value) for value in range(max(0, count - 2), count + 4) if value != count]
    return q(
        question_id,
        board,
        f"How many {color_name.lower()} {piece_name.lower()}s are on the board?",
        answer,
        distractors,
        "material count",
        f"There are {count} {color_name.lower()} {piece_name.lower()}s in the piece-placement field.",
        rng,
    )


def total_piece_question(question_id: str, fen: str, rng: random.Random) -> FenQuestion:
    board = chess.Board(fen)
    count = len(board.piece_map())
    answer = str(count)
    distractors = [str(value) for value in [count - 3, count - 1, count + 1, count + 2, count + 4] if value > 0]
    return q(
        question_id,
        board,
        "How many total pieces are left on the board?",
        answer,
        distractors,
        "piece count",
        f"The board has {count} occupied squares.",
        rng,
    )


def king_square_question(question_id: str, fen: str, color: chess.Color, rng: random.Random) -> FenQuestion:
    board = chess.Board(fen)
    color_name = "White" if color == chess.WHITE else "Black"
    square = chess.square_name(board.king(color))
    return q(
        question_id,
        board,
        f"Where is the {color_name.lower()} king?",
        square,
        [name for name in ["e1", "g1", "c1", "e8", "g8", "c8", "d4", "h6", "b2"] if name != square],
        "king location",
        f"The {color_name.lower()} king is on {square}.",
        rng,
    )


def castling_question(question_id: str, fen: str, rng: random.Random) -> FenQuestion:
    board = chess.Board(fen)
    rights = []
    if board.has_kingside_castling_rights(chess.WHITE):
        rights.append("White kingside")
    if board.has_queenside_castling_rights(chess.WHITE):
        rights.append("White queenside")
    if board.has_kingside_castling_rights(chess.BLACK):
        rights.append("Black kingside")
    if board.has_queenside_castling_rights(chess.BLACK):
        rights.append("Black queenside")
    answer = ", ".join(rights) if rights else "No castling rights"
    return q(
        question_id,
        board,
        "Which castling rights are present?",
        answer,
        [
            "White kingside, White queenside, Black kingside, Black queenside",
            "White kingside, Black kingside",
            "White queenside, Black queenside",
            "No castling rights",
            "White kingside, White queenside",
            "Black kingside, Black queenside",
        ],
        "castling rights",
        f"The third FEN field is {fen.split()[2]!r}, meaning: {answer}.",
        rng,
    )


def ep_question(question_id: str, fen: str, rng: random.Random) -> FenQuestion:
    board = chess.Board(fen)
    answer = chess.square_name(board.ep_square) if board.ep_square is not None else "No en-passant square"
    return q(
        question_id,
        board,
        "What is the en-passant target square?",
        answer,
        ["No en-passant square", "e3", "e6", "d3", "d6", "c3", "c6", "f3", "f6"],
        "en-passant field",
        f"The fourth FEN field is {fen.split()[3]!r}, so the en-passant target is {answer}.",
        rng,
    )


def legal_move_question(question_id: str, fen: str, answer_move: str, distractors: list[str], rng: random.Random) -> FenQuestion:
    board = chess.Board(fen)
    if chess.Move.from_uci(answer_move) not in board.legal_moves:
        raise ValueError(f"{answer_move} is not legal in {fen}")
    for move_uci in distractors:
        try:
            move = chess.Move.from_uci(move_uci)
        except ValueError:
            raise ValueError(f"{move_uci} is not valid UCI")
        if move in board.legal_moves:
            raise ValueError(f"{move_uci} is a legal distractor in {fen}")
    return q(
        question_id,
        board,
        "Which listed move is legal in this position?",
        answer_move,
        distractors,
        "legal move recognition",
        f"{answer_move} is legal for the side to move in this FEN. Legal moves must match side, piece movement, blockers, check rules, and promotion syntax.",
        rng,
    )


def generate_questions(seed: int = 53) -> list[FenQuestion]:
    rng = random.Random(seed)
    questions: list[FenQuestion] = []
    add = questions.append

    add(occupancy_question("fen-001", chess.STARTING_FEN, "e1", rng))
    add(occupancy_question("fen-002", chess.STARTING_FEN, "d8", rng))
    add(occupancy_question("fen-003", chess.STARTING_FEN, "e5", rng))
    add(side_question("fen-004", chess.STARTING_FEN, rng))
    add(castling_question("fen-005", chess.STARTING_FEN, rng))
    add(ep_question("fen-006", chess.STARTING_FEN, rng))
    add(total_piece_question("fen-007", chess.STARTING_FEN, rng))
    add(material_count_question("fen-008", chess.STARTING_FEN, chess.WHITE, chess.PAWN, rng))

    italian = "r1bqkbnr/pppp1ppp/2n5/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R b KQkq - 2 3"
    add(occupancy_question("fen-009", italian, "c4", rng))
    add(occupancy_question("fen-010", italian, "c6", rng))
    add(side_question("fen-011", italian, rng))
    add(king_square_question("fen-012", italian, chess.WHITE, rng))
    add(castling_question("fen-013", italian, rng))
    add(material_count_question("fen-014", italian, chess.BLACK, chess.KNIGHT, rng))

    check_pos = "4k3/8/8/8/4Q3/8/8/4K3 b - - 0 1"
    add(check_question("fen-015", check_pos, rng))
    add(occupancy_question("fen-016", check_pos, "e4", rng))
    add(king_square_question("fen-017", check_pos, chess.BLACK, rng))
    add(total_piece_question("fen-018", check_pos, rng))
    add(legal_move_question("fen-019", check_pos, "e8d8", ["e8e7", "e8e6", "a7a6"], rng))

    endgame = "8/8/8/3k4/8/4K3/8/8 w - - 20 71"
    add(occupancy_question("fen-020", endgame, "d5", rng))
    add(side_question("fen-021", endgame, rng))
    add(total_piece_question("fen-022", endgame, rng))
    add(check_question("fen-023", endgame, rng))

    ep = "rnbqkbnr/ppp1pppp/8/3pP3/8/8/PPPP1PPP/RNBQKBNR w KQkq d6 0 3"
    add(ep_question("fen-024", ep, rng))
    add(occupancy_question("fen-025", ep, "e5", rng))
    add(legal_move_question("fen-026", ep, "e5d6", ["e7e5", "d5d4", "e5e4"], rng))

    castle = "r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1"
    add(castling_question("fen-027", castle, rng))
    add(legal_move_question("fen-028", castle, "e1g1", ["e1e3", "a1b2", "e8g8"], rng))
    add(king_square_question("fen-029", castle, chess.BLACK, rng))
    add(material_count_question("fen-030", castle, chess.WHITE, chess.ROOK, rng))

    promo = "4k3/P7/8/8/8/8/8/4K3 w - - 0 1"
    add(occupancy_question("fen-031", promo, "a7", rng))
    add(legal_move_question("fen-032", promo, "a7a8q", ["a7a8", "a7b8q", "a7a6"], rng))
    add(total_piece_question("fen-033", promo, rng))

    pinned = "4k3/8/8/8/8/8/4r3/4K3 w - - 0 1"
    add(check_question("fen-034", pinned, rng))
    add(occupancy_question("fen-035", pinned, "e2", rng))
    add(legal_move_question("fen-036", pinned, "e1f1", ["e1f2", "e2e1", "a1a2"], rng))

    mid = "2r2rk1/pp1n1ppp/2pbpn2/q7/3P4/2N1PN2/PPQ1BPPP/2R2RK1 w - - 4 12"
    add(occupancy_question("fen-037", mid, "c8", rng))
    add(occupancy_question("fen-038", mid, "q7".replace("q", "a"), rng))
    add(side_question("fen-039", mid, rng))
    add(material_count_question("fen-040", mid, chess.BLACK, chess.BISHOP, rng))
    add(material_count_question("fen-041", mid, chess.WHITE, chess.KNIGHT, rng))
    add(total_piece_question("fen-042", mid, rng))
    add(check_question("fen-043", mid, rng))

    rook_end = "8/5pk1/6p1/8/3R4/6P1/5PK1/8 b - - 1 42"
    add(side_question("fen-044", rook_end, rng))
    add(occupancy_question("fen-045", rook_end, "d4", rng))
    add(material_count_question("fen-046", rook_end, chess.BLACK, chess.PAWN, rng))
    add(total_piece_question("fen-047", rook_end, rng))

    mate_net = "7k/6Q1/6K1/8/8/8/8/8 b - - 0 1"
    add(check_question("fen-048", mate_net, rng))
    add(occupancy_question("fen-049", mate_net, "g7", rng))
    add(total_piece_question("fen-050", mate_net, rng))

    if len(questions) != 50:
        raise AssertionError(f"expected 50 questions, got {len(questions)}")
    return questions


def validate_questions(questions: list[FenQuestion]) -> None:
    ids = set()
    for question in questions:
        if question.id in ids:
            raise AssertionError(f"duplicate question id {question.id}")
        ids.add(question.id)
        chess.Board(question.fen)
        if len(question.choices) != 4:
            raise AssertionError(f"{question.id} does not have four choices")
        if len(set(question.choices)) != 4:
            raise AssertionError(f"{question.id} has duplicate choices")
        if question.answer_index < 0 or question.answer_index > 3:
            raise AssertionError(f"{question.id} has invalid answer index")
        if not question.explanation:
            raise AssertionError(f"{question.id} has no explanation")


def extract_text_fragment(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if isinstance(value.get("text"), str):
            return value["text"]
        if isinstance(value.get("json"), (dict, list)):
            return json.dumps(value["json"], separators=(",", ":"))
        return "".join(extract_text_fragment(item) for item in value.values())
    if isinstance(value, list):
        return "".join(extract_text_fragment(item) for item in value)
    return ""


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


class CodexFenClient:
    def __init__(self, model: str, effort: str, timeout: float):
        self.model = model
        self.effort = effort
        self.timeout = timeout
        self.url = f"ws://127.0.0.1:{free_port()}"
        self.proc: subprocess.Popen | None = None
        self.ws = None
        self.pending: dict[str, asyncio.Future] = {}
        self.turn_done: dict[str, asyncio.Future] = {}
        self.turn_text: dict[str, str] = {}
        self.thread_id = ""

    async def start(self) -> None:
        codex_cmd = resolve_codex_command()
        self.proc = subprocess.Popen(
            [codex_cmd, "app-server", "--listen", self.url],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        last_error = None
        for _ in range(80):
            try:
                self.ws = await websockets.connect(self.url)
                break
            except Exception as exc:
                last_error = exc
                await asyncio.sleep(0.25)
        if self.ws is None:
            raise RuntimeError(f"Could not connect to Codex app-server: {last_error}")
        asyncio.create_task(self._reader())
        await self.request(
            "initialize",
            {"clientInfo": {"name": "FEN curriculum", "version": "0.1.0"}, "capabilities": None},
        )
        thread = await self.request(
            "thread/start",
            {
                "cwd": str(ROOT),
                "model": self.model,
                "approvalPolicy": "never",
                "sandbox": "read-only",
                "ephemeral": True,
                "baseInstructions": (
                    "You are learning to interpret chess FEN strings. Answer only from the supplied FEN and choices. "
                    "Do not use online search. Do not call tools. Do not inspect files. Return only JSON."
                ),
                "developerInstructions": (
                    "For each question, return JSON matching the schema. Pick one letter from a,b,c,d. "
                    "Use the FEN fields directly: piece placement ranks 8 to 1, side to move, castling rights, "
                    "en-passant square, and legal chess rules when the question asks about check or legal moves. "
                    "Do not call tools or browse. Learning feedback will be provided by the host after grading."
                ),
            },
        )
        self.thread_id = thread["thread"]["id"]

    async def _reader(self) -> None:
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                request_id = msg.get("id")
                if request_id in self.pending:
                    future = self.pending.pop(request_id)
                    if "error" in msg:
                        future.set_exception(RuntimeError(json.dumps(msg["error"])))
                    else:
                        future.set_result(msg.get("result"))
                    continue

                method = msg.get("method")
                params = msg.get("params", {})
                if method in {"item/agentMessage/delta", "item/agent_message/delta"}:
                    turn_id = params["turnId"]
                    self.turn_text[turn_id] = self.turn_text.get(turn_id, "") + params.get("delta", "")
                elif method == "item/completed":
                    item = params.get("item", {})
                    if item.get("type") in {"agentMessage", "agent_message", "message"}:
                        text = extract_text_fragment(item) or self.turn_text.get(params["turnId"], "")
                        self.turn_text[params["turnId"]] = text
                elif method == "turn/completed":
                    turn = params.get("turn", {})
                    turn_id = turn.get("id")
                    future = self.turn_done.get(turn_id)
                    if future and not future.done():
                        if turn.get("status") == "failed" or turn.get("error"):
                            future.set_exception(RuntimeError(json.dumps(turn.get("error") or turn)))
                        else:
                            future.set_result(turn)
                elif method == "error":
                    turn_id = params.get("turnId")
                    future = self.turn_done.get(turn_id)
                    if future and not future.done():
                        future.set_exception(RuntimeError(json.dumps(params.get("error") or params)))
        except ConnectionClosed:
            return

    async def request(self, method: str, params: dict):
        request_id = str(uuid.uuid4())
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        await self.ws.send(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}))
        return await future

    async def ask(self, question: FenQuestion, feedback: list[str]) -> dict:
        payload = {
            "task": "fen_multiple_choice",
            "question": question.public_payload(),
            "recent_feedback": feedback[-8:],
            "rules": [
                "Do not use online search.",
                "Do not call tools.",
                "Use only the FEN and the provided answer choices.",
                "Return one choice letter.",
            ],
        }
        response = await self.request(
            "turn/start",
            {
                "threadId": self.thread_id,
                "effort": self.effort,
                "input": [{"type": "text", "text": json.dumps(payload, separators=(",", ":"))}],
                "outputSchema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["answer", "reason"],
                    "properties": {
                        "answer": {"type": "string", "enum": list(LETTERS)},
                        "reason": {"type": "string"},
                    },
                },
            },
        )
        turn_id = response["turn"]["id"]
        done = asyncio.get_running_loop().create_future()
        self.turn_done[turn_id] = done
        try:
            await asyncio.wait_for(done, timeout=self.timeout)
            text = self.turn_text.get(turn_id, "").strip()
            return parse_json_object(text)
        finally:
            self.turn_done.pop(turn_id, None)
            self.turn_text.pop(turn_id, None)

    async def ask_batch(self, questions: list[FenQuestion], feedback: list[str]) -> list[dict]:
        payload = {
            "task": "fen_multiple_choice_batch",
            "questions": [question.public_payload() for question in questions],
            "recent_feedback": feedback[-12:],
            "rules": [
                "Do not use online search.",
                "Do not call tools.",
                "Use only each FEN and its provided answer choices.",
                "Return one answer object for every question id.",
            ],
        }
        response = await self.request(
            "turn/start",
            {
                "threadId": self.thread_id,
                "effort": self.effort,
                "input": [{"type": "text", "text": json.dumps(payload, separators=(",", ":"))}],
                "outputSchema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["answers"],
                    "properties": {
                        "answers": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["id", "answer", "reason"],
                                "properties": {
                                    "id": {"type": "string"},
                                    "answer": {"type": "string", "enum": list(LETTERS)},
                                    "reason": {"type": "string"},
                                },
                            },
                        }
                    },
                },
            },
        )
        turn_id = response["turn"]["id"]
        done = asyncio.get_running_loop().create_future()
        self.turn_done[turn_id] = done
        try:
            await asyncio.wait_for(done, timeout=self.timeout)
            text = self.turn_text.get(turn_id, "").strip()
            data = parse_json_object(text)
            answers = data.get("answers")
            if not isinstance(answers, list):
                raise ValueError("batch response did not include answers array")
            return answers
        finally:
            self.turn_done.pop(turn_id, None)
            self.turn_text.pop(turn_id, None)

    async def close(self) -> None:
        if self.ws is not None:
            await self.ws.close()
        if self.proc is not None and self.proc.poll() is None:
            subprocess.run(
                ["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )


def grade_answer(question: FenQuestion, answer: str) -> bool:
    return answer.strip().lower() == question.answer_letter


def build_lesson(miss: dict) -> str:
    question = miss["question"]
    return (
        f"{question['id']} ({question['concept']}): {question['prompt']} "
        f"Correct answer: {miss['correct_letter']}) {miss['correct_text']}. "
        f"Lesson: {question['explanation']}"
    )


def render_markdown(result: dict) -> str:
    lines = [
        "# FEN Curriculum Lessons",
        "",
        f"Generated: {result['generated_at']}",
        f"Model: {result['model']}",
        f"Questions: {result['question_count']}",
        f"Cycles completed: {len(result['cycles'])}",
        f"Final score: {result['final_score']} / {result['question_count']}",
        f"Mastered: {'yes' if result['mastered'] else 'no'}",
        "",
        "## How To Read FEN",
        "- Piece placement is eight slash-separated ranks from rank 8 down to rank 1.",
        "- Files inside each rank move left to right from a-file to h-file.",
        "- Digits skip that many empty squares.",
        "- Uppercase pieces are White; lowercase pieces are Black.",
        "- The second field is side to move, the third is castling rights, and the fourth is the en-passant target.",
        "",
        "## Missed-Question Lessons",
    ]
    if not result["misses"]:
        lines.append("- No misses recorded.")
    else:
        for miss in result["misses"][-40:]:
            lines.append(f"- {build_lesson(miss)}")
    lines.append("")
    return "\n".join(lines)


def update_memory(result: dict) -> None:
    MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    current = MEMORY_PATH.read_text(encoding="utf-8", errors="replace") if MEMORY_PATH.exists() else "# Codex-chess-learner Memory\n"
    missed_concepts = sorted({miss["question"]["concept"] for miss in result["misses"]})
    block_lines = [
        MEMORY_START,
        "## FEN Curriculum Summary",
        f"- Last updated: {result['generated_at']}",
        f"- Model: {result['model']}",
        f"- Final score: {result['final_score']} / {result['question_count']}.",
        f"- Mastered held-out set: {'yes' if result['mastered'] else 'no'}.",
        "- Apply `knowledgebase/fen-curriculum-lessons.md` before interpreting any chess position.",
        "- FEN piece placement is read rank 8 to rank 1, with files a through h inside each rank and digits as empty-square skips.",
        "- Uppercase FEN letters are White pieces; lowercase letters are Black pieces.",
        "- Always account for side-to-move, check status, castling rights, en-passant field, material counts, and legal-move constraints before choosing a move.",
    ]
    if missed_concepts:
        block_lines.append(f"- Most recent weak concepts: {', '.join(missed_concepts)}.")
    block_lines.append(MEMORY_END)
    block = "\n".join(block_lines)
    if MEMORY_START in current and MEMORY_END in current:
        updated = re.sub(re.escape(MEMORY_START) + r".*?" + re.escape(MEMORY_END), block, current, flags=re.S)
    else:
        updated = current.rstrip() + "\n\n" + block + "\n"
    MEMORY_PATH.write_text(updated, encoding="utf-8")


def write_outputs(result: dict, json_path: Path, markdown_path: Path, write_memory: bool) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    markdown_path.write_text(render_markdown(result), encoding="utf-8")
    if write_memory:
        update_memory(result)


def make_result(model: str, questions: list[FenQuestion], cycles: list[dict], error: str = "") -> dict:
    latest_by_id = {}
    misses = []
    for cycle in cycles:
        for attempt in cycle["attempts"]:
            latest_by_id[attempt["question"]["id"]] = attempt
            if not attempt["correct"]:
                misses.append(attempt)
    final_score = sum(1 for attempt in latest_by_id.values() if attempt["correct"])
    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "model": model,
        "question_count": len(questions),
        "final_score": final_score,
        "mastered": final_score == len(questions) and not error,
        "error": error,
        "cycles": cycles,
        "misses": misses,
    }


def offline_validate(args: argparse.Namespace, questions: list[FenQuestion]) -> dict:
    attempts = []
    for index, question in enumerate(questions):
        answer = question.answer_letter
        if args.force_misses and index < args.force_misses:
            answer = next(letter for letter in LETTERS if letter != question.answer_letter)
        correct = grade_answer(question, answer)
        attempts.append(
            {
                "question": {
                    "id": question.id,
                    "fen": question.fen,
                    "prompt": question.prompt,
                    "concept": question.concept,
                    "explanation": question.explanation,
                },
                "model_answer": answer,
                "model_reason": "offline validation",
                "correct": correct,
                "correct_letter": question.answer_letter,
                "correct_text": question.answer_text,
            }
        )
    return make_result(args.model, questions, [{"cycle": 1, "mode": "offline", "attempts": attempts}])


async def run_curriculum(args: argparse.Namespace, questions: list[FenQuestion]) -> dict:
    client = CodexFenClient(args.model, args.effort, args.timeout)
    cycles = []
    feedback: list[str] = []
    active_questions = questions
    pending_full_check = False
    error = ""
    consecutive_errors = 0
    try:
        await client.start()
        for cycle_number in range(1, args.max_cycles + 1):
            attempts = []
            print(f"Cycle {cycle_number}: {len(active_questions)} FEN questions", flush=True)
            for start in range(0, len(active_questions), args.batch_size):
                batch = active_questions[start : start + args.batch_size]
                batch_error = ""
                try:
                    print(
                        f"  Asking {batch[0].id}..{batch[-1].id} ({len(batch)} questions)",
                        flush=True,
                    )
                    batch_answers = await client.ask_batch(batch, feedback) if args.batch_size > 1 else [
                        await client.ask(batch[0], feedback)
                    ]
                    answers_by_id = {
                        str(item.get("id") or batch[index].id): item
                        for index, item in enumerate(batch_answers)
                        if isinstance(item, dict) and index < len(batch)
                    }
                    consecutive_errors = 0
                except Exception as exc:
                    consecutive_errors += 1
                    answers_by_id = {}
                    batch_error = f"{type(exc).__name__}: {exc}"
                    print(f"  Batch failed: {batch_error}", flush=True)
                    if consecutive_errors >= args.max_consecutive_errors:
                        error = f"Stopped after {consecutive_errors} consecutive batch errors; latest: {batch_error}"
                for question in batch:
                    data = answers_by_id.get(question.id, {})
                    answer = str(data.get("answer", "")).strip().lower()
                    reason = str(data.get("reason", "")).strip() or batch_error
                    correct = grade_answer(question, answer)
                    attempt = {
                        "question": {
                            "id": question.id,
                            "fen": question.fen,
                            "prompt": question.prompt,
                            "concept": question.concept,
                            "explanation": question.explanation,
                        },
                        "model_answer": answer,
                        "model_reason": reason,
                        "correct": correct,
                        "correct_letter": question.answer_letter,
                        "correct_text": question.answer_text,
                    }
                    attempts.append(attempt)
                    if not correct:
                        feedback.append(build_lesson(attempt))
                if error:
                    break
            if error:
                cycles.append({"cycle": cycle_number, "mode": "full" if len(active_questions) == len(questions) else "misses", "attempts": attempts})
                break
            cycles.append({"cycle": cycle_number, "mode": "full" if len(active_questions) == len(questions) else "misses", "attempts": attempts})
            missed_ids = {attempt["question"]["id"] for attempt in attempts if not attempt["correct"]}
            if not missed_ids:
                if pending_full_check or len(active_questions) == len(questions):
                    break
                active_questions = questions
                pending_full_check = True
                continue
            active_questions = [question for question in questions if question.id in missed_ids]
            pending_full_check = False
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        await client.close()
    return make_result(args.model, questions, cycles, error)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a hidden-answer FEN interpretation curriculum against Codex app-server.")
    parser.add_argument("--model", default=str(config_value("fenCurriculum.model", "gpt-5.3-codex")))
    parser.add_argument("--effort", default=str(config_value("fenCurriculum.effort", "medium")))
    parser.add_argument("--timeout", type=float, default=float(config_value("codex.preflightTimeoutSeconds", 60)))
    parser.add_argument("--max-cycles", type=int, default=int(config_value("fenCurriculum.maxCycles", 4)))
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--max-consecutive-errors", type=int, default=2)
    parser.add_argument("--json", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--markdown", type=Path, default=DEFAULT_MD)
    parser.add_argument("--offline-validate", action="store_true")
    parser.add_argument("--force-misses", type=int, default=0)
    parser.add_argument("--no-write-memory", action="store_true")
    args = parser.parse_args()

    questions = generate_questions()
    validate_questions(questions)
    args.batch_size = max(1, min(args.batch_size, len(questions)))

    if args.offline_validate:
        result = offline_validate(args, questions)
    else:
        result = asyncio.run(run_curriculum(args, questions))

    write_outputs(result, args.json, args.markdown, not args.no_write_memory)
    print(
        f"FEN curriculum: score {result['final_score']}/{result['question_count']} "
        f"mastered={'yes' if result['mastered'] else 'no'} output={args.markdown}",
        flush=True,
    )
    if result.get("error"):
        print(f"FEN curriculum error: {result['error']}", flush=True)
        return 1
    return 0 if result["mastered"] or args.offline_validate else 2


if __name__ == "__main__":
    raise SystemExit(main())
