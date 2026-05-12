import tempfile
import unittest
from pathlib import Path

import chess.pgn

from tools.live_pgn_viewer import collect_stats


def write_finished_game(path: Path, event: str = "Synthetic game") -> None:
    game = chess.pgn.Game()
    game.headers["Event"] = event
    game.headers["White"] = "WhiteEngine"
    game.headers["Black"] = "BlackEngine"
    game.headers["Result"] = "1-0"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(game) + "\n", encoding="utf-8")


class LivePgnViewerStatsTests(unittest.TestCase):
    def test_collect_stats_excludes_live_and_backup_archives(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            out_dir = Path(temp_dir)
            write_finished_game(out_dir / "active.pgn", "Active")
            write_finished_game(out_dir / "live" / "active-live.pgn", "Live")
            write_finished_game(out_dir / "backups" / "games-reset" / "old.pgn", "Backup")

            stats = collect_stats(out_dir, None, None)

        self.assertEqual(stats["games"], 1)
        self.assertEqual([match["file"] for match in stats["matches"]], ["active.pgn"])


if __name__ == "__main__":
    unittest.main()
