import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import chess
import chess.pgn

from tools.update_learner_knowledgebase import (
    DEFAULT_JSON,
    DEFAULT_KB,
    DEFAULT_MEMORY,
    DEFAULT_STRATEGY_JSON,
    DEFAULT_STRATEGY_KB,
    MEMORY_END,
    MEMORY_START,
    apply_context_defaults,
    build_strategy_lesson_summary,
    context_dir_for_engine,
    exact_move_rule_violations,
    preserve_generated_at_if_unchanged,
    preserve_strategy_generated_at_if_no_new_evidence,
    read_games,
    sanitized_strategy_summary,
    synthesize_strategy_concepts,
    update_self_extension_artifacts,
    update_memory,
)


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

    def test_pending_strategy_evidence_survives_deferred_watch_cycle(self):
        games, path = self.read_synthetic_games(1)
        first = build_strategy_lesson_summary(games, path, None, generated_at="first")
        first["concept_synthesis"] = {
            "status": "deferred",
            "message": "concept synthesis deferred while live training is running",
        }

        second = build_strategy_lesson_summary(games, path, None, generated_at="second", previous=first)

        self.assertEqual(second["new_evidence_count"], 0)
        self.assertGreater(len(second["pending_synthesis_evidence"]), 0)
        self.assertEqual(second["concept_synthesis"]["status"], "deferred")

    def test_persisted_strategy_json_omits_raw_fen_move_evidence(self):
        games, path = self.read_synthetic_games(1)
        summary = build_strategy_lesson_summary(games, path, None, generated_at="test")

        safe = sanitized_strategy_summary(summary)
        safe_text = json.dumps(safe)

        self.assertNotIn("fen_before", safe_text)
        self.assertNotIn("fen_after", safe_text)
        self.assertTrue(all(key.startswith("sha256:") for key in safe["evidence_keys"]))
        self.assertEqual(exact_move_rule_violations(safe_text), [])

    def test_pending_strategy_evidence_triggers_later_synthesis_attempt(self):
        games, path = self.read_synthetic_games(1)
        summary = build_strategy_lesson_summary(games, path, None, generated_at="test")
        summary["new_evidence"] = []
        summary["new_evidence_count"] = 0

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("tools.update_learner_knowledgebase.Path.home", return_value=Path(temp_dir)):
                concepts, synthesis = synthesize_strategy_concepts(summary, "gpt-test", "low", 1)

        previous = {**summary, "generated_at": "old", "concept_synthesis": {"status": "deferred"}}
        summary["concepts"] = concepts
        summary["concept_synthesis"] = synthesis
        stable = preserve_strategy_generated_at_if_no_new_evidence(summary, previous)

        self.assertEqual(concepts, [])
        self.assertEqual(synthesis["status"], "unavailable")
        self.assertIn("query script not found", synthesis["message"])
        self.assertEqual(stable["concept_synthesis"]["status"], "unavailable")

    def test_live_lesson_timestamp_is_preserved_when_summary_is_unchanged(self):
        previous = {
            "generated_at": "old",
            "source_pgn": "old.pgn",
            "source_stdout": "old.log",
            "completed_games": 2,
            "learner_points": 1.0,
            "reason_counts": {"mate": 1},
        }
        current = {
            "generated_at": "new",
            "source_pgn": "new.pgn",
            "source_stdout": "new.log",
            "completed_games": 2,
            "learner_points": 1.0,
            "reason_counts": {"mate": 1},
        }

        stable = preserve_generated_at_if_unchanged(
            current,
            previous,
            {"generated_at", "source_pgn", "source_stdout"},
        )
        changed = preserve_generated_at_if_unchanged(
            {**current, "completed_games": 3},
            previous,
            {"generated_at", "source_pgn", "source_stdout"},
        )

        self.assertEqual(stable["generated_at"], "old")
        self.assertEqual(stable["source_pgn"], "old.pgn")
        self.assertEqual(stable["source_stdout"], "old.log")
        self.assertEqual(changed["generated_at"], "new")

    def test_strategy_timestamp_is_preserved_when_no_new_evidence(self):
        previous = {
            "generated_at": "old",
            "source_pgn": "old.pgn",
            "source_stdout": "old.log",
            "concepts": [{"name": "keep pieces safe"}],
            "concept_synthesis": {"status": "ok", "message": "previous synthesis"},
        }
        current = {
            "generated_at": "new",
            "source_pgn": "new.pgn",
            "source_stdout": "new.log",
            "new_evidence": [],
            "new_evidence_count": 0,
            "concepts": previous["concepts"],
            "concept_synthesis": previous["concept_synthesis"],
        }

        concepts, synthesis = synthesize_strategy_concepts(current, "unused", "low", 1)
        current["concepts"] = concepts
        current["concept_synthesis"] = synthesis
        stable = preserve_strategy_generated_at_if_no_new_evidence(current, previous)

        self.assertEqual(stable["generated_at"], "old")
        self.assertEqual(stable["source_pgn"], "old.pgn")
        self.assertEqual(stable["source_stdout"], "old.log")
        self.assertEqual(stable["concept_synthesis"], previous["concept_synthesis"])

    def test_memory_update_preserves_last_updated_when_autolearn_content_is_unchanged(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "MEMORY.md"
            path.write_text(
                "\n".join(
                    [
                        "# Codex-chess-learner Memory",
                        "",
                        MEMORY_START,
                        "## Autolearn Summary",
                        "- Last updated: old",
                        "- Current match score: 1.0 / 2 (50.0%).",
                        "- Result reasons: mate=2.",
                        "- Apply `knowledgebase/live-match-lessons.md` before choosing moves.",
                        "- Apply model-discovered concepts from `knowledgebase/strategy-lessons.md` as generic value adjustments, not as memorized move answers.",
                        "- Use engine-local `skills/self-play-concepts/SKILL.md` and `tools/self_play_concepts.json` only as generalized self-play concept aids, never as exact move memory.",
                        "- Avoid threefold repetition loops unless drawing is the only practical outcome.",
                        "- Manage the clock while still choosing a move intentionally; there is no fallback or client-picked move.",
                        "- Never return a move outside `legal_moves`; never return `0000` while legal moves exist.",
                        MEMORY_END,
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            before = path.read_text(encoding="utf-8")

            update_memory(
                path,
                {
                    "generated_at": "new",
                    "learner_points": 1.0,
                    "completed_games": 2,
                    "learner_score_percent": 50.0,
                    "reason_counts": {"mate": 2},
                },
            )

            self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_autolearn_defaults_can_target_zero_context(self):
        args = Namespace(
            engine_name="Codex-chess-zero",
            context_dir=None,
            memory=DEFAULT_MEMORY,
            output=DEFAULT_KB,
            json=DEFAULT_JSON,
            strategy_output=DEFAULT_STRATEGY_KB,
            strategy_json=DEFAULT_STRATEGY_JSON,
        )

        apply_context_defaults(args)

        zero_context = context_dir_for_engine("Codex-chess-zero")
        self.assertEqual(args.context_dir, zero_context)
        self.assertEqual(args.memory, zero_context / "MEMORY.md")
        self.assertEqual(args.output, zero_context / "knowledgebase" / "live-match-lessons.md")
        self.assertEqual(args.json, zero_context / "knowledgebase" / "live-match-lessons.json")
        self.assertEqual(args.strategy_output, zero_context / "knowledgebase" / "strategy-lessons.md")
        self.assertEqual(args.strategy_json, zero_context / "knowledgebase" / "strategy-lessons.json")

    def test_self_extension_writes_engine_local_skill_and_tools_from_concepts(self):
        summary = {
            "generated_at": "test",
            "concepts": [
                {
                    "name": "Loose forcing piece",
                    "trigger": "a checking or forcing move leaves the moved piece undefended",
                    "value_adjustment": "penalize candidate unless the opponent has no safe capture",
                    "why": "self-play evidence showed material loss after forcing moves",
                    "confidence": 0.8,
                }
            ],
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            context = Path(temp_dir)
            result = update_self_extension_artifacts(context, "Codex-chess-learner", summary)

            skill = context / "skills" / "self-play-concepts" / "SKILL.md"
            concept_json = context / "tools" / "self_play_concepts.json"
            audit_tool = context / "tools" / "concept_audit.py"

            self.assertEqual(result["status"], "ok")
            self.assertTrue(skill.exists())
            self.assertTrue(concept_json.exists())
            self.assertTrue(audit_tool.exists())
            self.assertIn("Loose forcing piece", skill.read_text(encoding="utf-8"))
            manifest = json.loads(concept_json.read_text(encoding="utf-8"))
            audit_text = audit_tool.read_text(encoding="utf-8").lower()

        self.assertEqual(manifest["schema"], "learner-self-extension-v1")
        self.assertFalse(any(manifest["training_sources"].values()))
        self.assertIn("feature audit only", audit_text)

    def test_self_extension_rejects_exact_fen_move_answers(self):
        summary = {
            "generated_at": "test",
            "concepts": [
                {
                    "name": "Exact answer",
                    "trigger": "In rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1, play e2e4",
                    "value_adjustment": "always choose the move",
                }
            ],
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            result = update_self_extension_artifacts(Path(temp_dir), "Codex-chess-learner", summary)

        self.assertEqual(result["status"], "rejected")
        self.assertIn("exact FEN", result["problems"][0])


if __name__ == "__main__":
    unittest.main()
