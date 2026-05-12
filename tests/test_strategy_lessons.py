import tempfile
import unittest
from pathlib import Path

import chess
import chess.pgn

from tools.update_learner_knowledgebase import build_strategy_lesson_summary, read_games


HANGING_CHECKER_FEN = "r7/ppr2ppk/8/3p4/5b2/8/PP3KPP/8 b - - 1 25"


def synthetic_hanging_checker_pgn(game_count: int = 1) -> str:
    games = []
    for index in range(game_count):
        game = chess.pgn.Game()
        game.headers["Event"] = "Synthetic hanging checker"
        game.headers["White"] = "Codex-chess"
        game.headers["Black"] = "Codex-chess-learner"
        game.headers["Result"] = "1-0"
        game.headers["SetUp"] = "1"
        game.headers["FEN"] = HANGING_CHECKER_FEN
        game.headers["Termination"] = "normal"
        game.headers["Round"] = str(index + 1)
        board = game.board()
        node = game
        for move_text in ["f4e3", "f2e3"]:
            move = chess.Move.from_uci(move_text)
            node = node.add_variation(move)
            board.push(move)
        games.append(str(game))
    return "\n\n".join(games) + "\n"


class StrategyLessonTests(unittest.TestCase):
    def read_synthetic_games(self, game_count: int):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "synthetic.pgn"
            path.write_text(synthetic_hanging_checker_pgn(game_count), encoding="utf-8")
            return read_games(path), path

    def test_hanging_checker_evidence_is_neutral_not_a_handwritten_rule(self):
        games, path = self.read_synthetic_games(1)
        summary = build_strategy_lesson_summary(games, path, None, generated_at="test")

        observation = next(item for item in summary["observations"] if item["category"] == "hanging_checking_piece")
        self.assertNotIn("lesson", observation)
        self.assertNotIn("title", observation)
        self.assertNotIn(HANGING_CHECKER_FEN, observation["description"])
        self.assertEqual(observation["evidence"][0]["move"], "f4e3")
        self.assertIn("f2e3", observation["evidence"][0]["detail"])
        self.assertEqual(summary["concepts"], [])

    def test_repeated_games_dedupe_and_increment_evidence(self):
        games, path = self.read_synthetic_games(2)
        summary = build_strategy_lesson_summary(games, path, None, generated_at="test")

        matching = [item for item in summary["observations"] if item["category"] == "hanging_checking_piece"]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["evidence_count"], 2)


if __name__ == "__main__":
    unittest.main()
