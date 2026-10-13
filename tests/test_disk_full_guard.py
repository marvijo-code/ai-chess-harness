"""A full disk never loses a game: state saves that fail, game threads that stop, the gate before a round is
closed, the free-disk check before a tournament and before every round, and the "disk low" chip.

Every game here is played by scripted players (ScriptEngine): the runner never makes a move for a player,
so each test also checks that the moves in the record are exactly the players' own."""

import contextlib
import errno
import io
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "engines" / "llm-chess-engine"))

import ai_chess_forever as forever  # noqa: E402
import ai_chess_memory as mem  # noqa: E402
import disk_guard  # noqa: E402
import llm_tournament_viewer as viewer  # noqa: E402
import play_llm_series as series  # noqa: E402
import play_llm_swiss as swiss  # noqa: E402

NODE = shutil.which("node")
MB = 1024 * 1024
# Scholar's mate: seven plies, White wins. Both scripted players answer from this one game.
SCRIPT = ["e2e4", "e7e5", "d1h5", "b8c6", "f1c4", "g8f6", "h5f7"]
SANS = ["e4", "e5", "Qh5", "Nc6", "Bc4", "Nf6", "Qxf7#"]


def no_space(*_args, **_kwargs):
    raise OSError(errno.ENOSPC, "No space left on device")


def free_disk(mb: float):
    """A disk_guard.USAGE stand-in: a 75 GB disk with `mb` MB free."""
    return lambda _path: types.SimpleNamespace(total=75 * 1024 * MB, used=75 * 1024 * MB - mb * MB, free=mb * MB)


class ScriptEngine:
    """A player that answers every `go` with its own next move of SCRIPT. It records each position it was
    asked to move in, so a test can prove which moves were asked for once, twice or never."""

    made: dict[str, list["ScriptEngine"]] = {}
    think_seconds = 0.0   # real time each move takes (so the clocks really move)

    def __init__(self, player: dict, cfg: dict | None = None) -> None:
        self.player = dict(player, provider=player.get("provider", "script"), model=player.get("model", "m"),
                           effort=player.get("effort", "high"))
        self.name = player["name"]
        self.is_uci = False
        self.cond = threading.Condition()
        self.lines: list[str] = []
        self.asked: list[int] = []     # plies already on the board at each `go`
        self.new_games = 0
        self.position = "position startpos"
        self.closed = False
        self.proc = types.SimpleNamespace(pid=0, poll=lambda: None, kill=lambda: None)
        ScriptEngine.made.setdefault(self.name, []).append(self)

    def new_game(self) -> None:
        self.new_games += 1

    def go_command(self, wtime: int, btime: int, inc: int = 0) -> str:
        return f"go wtime {wtime} btime {btime} winc {inc} binc {inc}"

    def send(self, line: str) -> None:
        if line.startswith("position"):
            self.position = line
        elif line.startswith("go"):
            plies = len(self.position.split(" moves ")[1].split()) if " moves " in self.position else 0
            self.asked.append(plies)
            if self.think_seconds:
                time.sleep(self.think_seconds)
            with self.cond:
                self.lines += ["info string thinkms 100", f"bestmove {SCRIPT[plies]}"]
                self.cond.notify_all()

    def close(self) -> None:
        self.closed = True


def one_game_state(tid: str = "t") -> dict:
    players = [{"name": "A"}, {"name": "B"}]
    cfg = dict(swiss.DEFAULTS, rounds=1, players=players)
    return {"id": tid, "title": tid, "config": {k: v for k, v in cfg.items() if k != "players"}, "players": players,
            "seed": 1, "seed_order": ["A", "B"], "current_round": 1, "finished": False, "winner": None,
            "rounds": [{"round": 1, "bye": None, "status": "live",
                        "pairings": [{"board": 1, "white": "A", "black": "B", "game_id": "r1b1"}]}],
            "games": {"r1b1": {"id": "r1b1", "round": 1, "board": 1, "white": "A", "black": "B", "status": "pending",
                               "result": "*", "moves": []}}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="diskguard-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.logs: list[str] = []
        ScriptEngine.made = {}
        self.patch(swiss, "SAVE_RETRY_DELAYS", (0, 0, 0))
        self.patch(swiss, "GAME_RESTART_WAIT_SECONDS", (0, 0, 0))
        self.patch(swiss, "LIVE_DIR", self.tmp / "live")
        self.patch(swiss, "ARCHIVE_DIR", self.tmp / "archive")
        self.patch(swiss, "LlmEngine", ScriptEngine)
        self.patch(swiss, "kill_engine_tree", lambda engine: engine.close())
        self.patch(swiss, "HOOKS", None)
        self.patch(swiss, "GAME_SLOTS", None)
        self.patch(swiss, "_quiet_reported", {})
        self.patch(disk_guard, "USAGE", free_disk(40_000))
        (self.tmp / "live").mkdir()
        (self.tmp / "archive").mkdir()

    def patch(self, owner, name, value):
        old = getattr(owner, name)
        setattr(owner, name, value)
        self.addCleanup(setattr, owner, name, old)

    def log(self, message: str) -> None:
        self.logs.append(message)

    def logged(self, text: str) -> list[str]:
        return [line for line in self.logs if text in line]

    def fill_disk(self):
        """Every file write of the runner fails with ENOSPC until the returned switch is set to False."""
        full = [True]
        real = swiss.write_text_retry

        def write(path, text, delays=None, sleep=None):
            if full[0]:
                no_space()
            real(path, text, delays=())

        self.patch(swiss, "write_text_retry", write)
        return full


class WriteRetryTest(Base):
    def test_a_failed_write_is_tried_again_with_a_backoff(self):
        calls, waits = [], []
        real = series._write_text_once

        def flaky(path, text):
            calls.append(path)
            if len(calls) < 3:
                no_space()
            real(path, text)

        self.patch(series, "_write_text_once", flaky)
        target = self.tmp / "file.json"
        series.write_text_retry(target, "{}", sleep=waits.append)
        self.assertEqual(target.read_text(), "{}")
        self.assertEqual(len(calls), 3)
        self.assertEqual(waits, list(series.WRITE_RETRY_DELAYS[:2]), "the waits grow: a backoff")
        self.assertEqual(sorted(series.WRITE_RETRY_DELAYS), list(series.WRITE_RETRY_DELAYS))

    def test_the_last_error_is_raised_for_a_caller_that_needs_the_file(self):
        self.patch(series, "_write_text_once", no_space)
        waits = []
        with self.assertRaises(OSError) as caught:
            series.write_text_retry(self.tmp / "file.json", "{}", sleep=waits.append)
        self.assertEqual(caught.exception.errno, errno.ENOSPC)
        self.assertEqual(len(waits), len(series.WRITE_RETRY_DELAYS))


class StateSaveTest(Base):
    def test_a_save_on_a_full_disk_never_raises_and_says_so_in_one_line(self):
        state = one_game_state()
        waits = []
        self.patch(swiss, "SAVE_RETRY_DELAYS", (0.5, 2.0, 5.0))
        ts = swiss.TournamentState(state, self.tmp / "live" / "t-tournament.json", self.log, sleep=waits.append)
        full = self.fill_disk()
        self.patch(disk_guard, "USAGE", free_disk(110))
        self.assertFalse(ts.save(), "False = the file on disk is not current")
        self.assertEqual(waits, [0.5, 2.0, 5.0], "tried again with a backoff before it gave up")
        self.assertEqual(len(self.logs), 1, self.logs)
        line = self.logs[0]
        self.assertIn("STATE SAVE FAILED after 4 tries", line)
        self.assertIn("the disk is full (No space left on device)", line)
        self.assertIn("110 MB free", line)
        self.assertIn("the games go on", line)
        self.assertNotIn("Traceback", line)
        # While it keeps failing: one quick try per save (no backoff on every move) and no log flood.
        for _ in range(30):
            self.assertFalse(ts.save())
        self.assertEqual(waits, [0.5, 2.0, 5.0])
        self.assertEqual(len(self.logs), 1, "one line, not one per failed save")
        self.assertEqual(ts.failed_saves, 31)
        # Space is back: the next save writes the whole state and one line says so.
        state["games"]["r1b1"]["status"] = "live"
        full[0] = False
        self.assertTrue(ts.save())
        on_disk = json.loads((self.tmp / "live" / "t-tournament.json").read_text(encoding="utf-8"))
        self.assertEqual(on_disk["games"]["r1b1"]["status"], "live")
        self.assertEqual(len(self.logs), 2)
        self.assertIn("STATE SAVE works again after 31 failed saves", self.logs[1])
        self.assertEqual((ts.failed_saves, ts.failing_since), (0, None))

    def test_a_long_outage_is_reported_once_a_minute(self):
        ts = swiss.TournamentState(one_game_state(), self.tmp / "live" / "t-tournament.json", self.log)
        self.fill_disk()
        self.patch(swiss, "SAVE_LOG_EVERY_SECONDS", 0)
        ts.save()
        ts.save()
        self.assertEqual(len(self.logs), 2)
        self.assertIn("STATE SAVE still failing: 2 failed saves", self.logs[1])

    def test_any_error_in_a_save_is_kept_away_from_the_caller(self):
        state = one_game_state()
        state["games"]["r1b1"]["bad"] = {1, 2}   # json cannot write a set
        ts = swiss.TournamentState(state, self.tmp / "live" / "t-tournament.json", self.log)
        self.assertFalse(ts.save())
        self.assertIn("TypeError", self.logs[0])


class GameOnAFullDiskTest(Base):
    def play(self, state, ts):
        white, black = ScriptEngine({"name": "A"}), ScriptEngine({"name": "B"})
        thread = threading.Thread(target=swiss.play_game_guarded, daemon=True, args=(
            "r1b1", white, black, dict(state["config"], players=state["players"]), ts,
            self.tmp / "live" / "t-r1b1-live.pgn", self.log, lambda engine: engine.close()))
        thread.start()
        thread.join(30)
        self.assertFalse(thread.is_alive(), "the game must end")
        return white, black

    def test_the_game_is_played_to_its_end_while_no_file_can_be_written(self):
        """The 2026-10-12 failure: `publish` -> `ts.save()` -> ENOSPC ended the thread after one move."""
        state = one_game_state()
        ts = swiss.TournamentState(state, self.tmp / "live" / "t-tournament.json", self.log)
        full = self.fill_disk()
        white, black = self.play(state, ts)
        game = state["games"]["r1b1"]
        self.assertEqual((game["status"], game["result"], game["end_kind"]), ("finished", "1-0", "board"))
        self.assertEqual(game["termination"], "checkmate")
        self.assertEqual([m["uci"] for m in game["moves"]], SCRIPT, "every move is a player's own move")
        self.assertEqual((white.asked, black.asked), ([0, 2, 4, 6], [1, 3, 5]), "each move was asked for once")
        self.assertEqual(self.logged("GAME THREAD"), [], "the thread never stopped")
        self.assertEqual(len(self.logged("STATE SAVE FAILED")), 1)
        self.assertEqual(len(self.logged("WRITE FAILED (live PGN")), 1)
        self.assertFalse((self.tmp / "live" / "t-tournament.json").exists(), "nothing could be written")
        # Space is back: the finished game reaches the disk with the next save (the heartbeat in a real run).
        full[0] = False
        self.assertTrue(ts.save())
        on_disk = json.loads((self.tmp / "live" / "t-tournament.json").read_text(encoding="utf-8"))
        self.assertEqual(on_disk["games"]["r1b1"]["result"], "1-0")
        self.assertEqual(len(on_disk["games"]["r1b1"]["moves"]), 7)

    def test_a_game_whose_thread_stops_goes_on_from_its_saved_moves(self):
        state = one_game_state()
        ts = swiss.TournamentState(state, self.tmp / "live" / "t-tournament.json", self.log)
        real, hits = swiss.move_comment, []

        def comment_that_breaks_once(clock_ms, elapsed_ms, tries, illegal, text):
            hits.append(1)
            if len(hits) == 4:   # while the fourth move is being recorded: an error nobody handles
                raise RuntimeError("boom")
            return real(clock_ms, elapsed_ms, tries, illegal, text)

        self.patch(swiss, "move_comment", comment_that_breaks_once)
        self.patch(ScriptEngine, "think_seconds", 0.06)
        first = [ScriptEngine({"name": "A"}), ScriptEngine({"name": "B"})]
        thread = threading.Thread(target=swiss.play_game_guarded, daemon=True, args=(
            "r1b1", first[0], first[1], dict(state["config"], players=state["players"]), ts,
            self.tmp / "live" / "t-r1b1-live.pgn", self.log, lambda engine: engine.close()),
            kwargs={"engine_for": lambda name: ScriptEngine({"name": name})})
        thread.start()
        thread.join(30)
        self.assertFalse(thread.is_alive())
        game = state["games"]["r1b1"]
        self.assertEqual((game["status"], game["result"]), ("finished", "1-0"))
        self.assertEqual([m["uci"] for m in game["moves"]], SCRIPT)
        self.assertEqual([m["san"] for m in game["moves"]], SANS)
        self.assertEqual([m["ply"] for m in game["moves"]], [1, 2, 3, 4, 5, 6, 7])
        died = self.logged("GAME THREAD DIED r1b1")
        self.assertEqual(len(died), 1, self.logs)
        self.assertIn("RuntimeError: boom", died[0])
        self.assertIn("goes on from its 3 saved moves", died[0])
        self.assertIn("no move is made for a player", died[0])
        self.assertEqual(len(self.logged("r1b1: goes on from ply 4 (3 saved moves put back)")), 1)
        self.assertEqual(game["restarts"][0]["plies"], 3)
        # Fresh engine processes took over; the three saved moves were never asked for again, and the lost
        # fourth move was asked from its player again (the runner did not keep or invent it).
        self.assertTrue(first[0].closed and first[1].closed)
        second_white, second_black = ScriptEngine.made["A"][1], ScriptEngine.made["B"][1]
        self.assertEqual((first[0].asked, first[1].asked), ([0, 2], [1, 3]))
        self.assertEqual((second_white.asked, second_black.asked), ([4, 6], [3, 5]))
        # The clocks carry over the stop (no increment here): they are not set back to 10 minutes.
        moves = game["moves"]
        self.assertEqual(moves[4]["clock_ms"], moves[2]["clock_ms"] - moves[4]["elapsed_ms"])
        self.assertEqual(moves[5]["clock_ms"], moves[3]["clock_ms"] - moves[5]["elapsed_ms"])
        self.assertLess(moves[4]["clock_ms"], 600_000 - 150, "three moves of White were charged, not one")
        self.assertIn("1. e4", game["pgn"])
        self.assertIn("Qxf7#", game["pgn"])

    def test_saved_moves_that_are_not_a_legal_game_are_not_trusted(self):
        with self.assertRaises(ValueError):
            swiss.replay_saved_moves([{"ply": 1, "side": "white", "uci": "e2e5"}], {})
        with self.assertRaises(ValueError):
            swiss.replay_saved_moves([{"ply": 2, "side": "white", "uci": "e2e4"}], {})
        board, history, clocks = swiss.replay_saved_moves(
            [{"ply": 1, "side": "white", "uci": "e2e4", "clock_ms": 590_000}, {"ply": 2, "side": "black", "uci": "e7e5"}],
            {True: 600_000, False: 600_000})
        self.assertEqual((history, clocks[True], clocks[False]), (["e2e4", "e7e5"], 590_000, 600_000))
        self.assertEqual(board.fullmove_number, 2)


class RoundGateTest(Base):
    def run_one_round(self, state):
        cfg = dict(state["config"], players=state["players"])
        return swiss.run_tournament(state, cfg, self.tmp / "live" / f"{state['id']}-tournament.json", self.log)

    def test_a_whole_tournament_survives_a_full_disk(self):
        state = one_game_state()
        self.fill_disk()
        self.assertEqual(self.run_one_round(state), 0)
        self.assertTrue(state["finished"])
        self.assertEqual(state["rounds"][0]["status"], "finished")
        self.assertEqual(state["games"]["r1b1"]["result"], "1-0")
        self.assertEqual(state["winner"], "A")
        self.assertEqual(len(self.logged("WRITE FAILED (archive PGN")), 1)

    def test_a_game_that_keeps_stopping_is_void_and_the_round_stays_open(self):
        state = one_game_state()
        real = swiss._play_game

        def always_breaks(*_args, **_kwargs):
            raise RuntimeError("boom")

        self.patch(swiss, "_play_game", always_breaks)
        self.assertEqual(self.run_one_round(state), 3, "3 = paused, never 0 = finished")
        game = state["games"]["r1b1"]
        self.assertEqual((game["status"], game["end_kind"], game["result"]), ("void", "void", "*"))
        self.assertFalse(state["finished"])
        self.assertEqual(state["rounds"][0]["status"], "live", "the round is not closed without its game")
        self.assertIn("the game stopped 4 times", state["paused"])
        self.assertEqual(len(self.logged("GAME THREAD DIED r1b1")), swiss.MAX_GAME_RESTARTS + 1)
        self.assertIn("the round is not closed without it", self.logged("GAME THREAD DIED r1b1")[-1])
        # The resume (the forever runner does it after 5 minutes) plays the game; only then the round closes.
        swiss._play_game = real
        self.assertEqual(self.run_one_round(swiss.prepare_resume(state)), 0)
        self.assertEqual((game["status"], game["result"]), ("finished", "1-0"))
        self.assertEqual([m["uci"] for m in game["moves"]], SCRIPT)
        self.assertEqual(state["rounds"][0]["status"], "finished")

    def test_a_round_never_closes_over_a_game_that_is_still_live(self):
        """The shape of r5b3 on 2026-10-12: status live, one move, no thread."""
        state = one_game_state()

        def leaves_it_live(game_id, white, black, cfg, ts, *_args, **_kwargs):
            ts.state["games"][game_id].update(status="live", moves=[{"ply": 1, "side": "white", "uci": "e2e4", "san": "e4"}])

        self.patch(swiss, "play_game_guarded", leaves_it_live)
        self.assertEqual(self.run_one_round(state), 3)
        self.assertFalse(state["finished"])
        self.assertEqual(state["rounds"][0]["status"], "live")
        self.assertEqual(state["games"]["r1b1"]["end_kind"], "void")
        self.assertEqual(len(self.logged("ROUND GATE: void: r1b1 had no result when its round ended (status live)")), 1)

    def test_no_champion_is_crowned_while_the_third_place_game_is_live(self):
        """Tournament #37 ended `finished: true` with `third: None`: its third place game had died."""
        names = ["P1", "P2", "P3", "P4"]
        cfg = dict(swiss.DEFAULTS, format="round-robin+knockout", knockoutSize=4, rounds=3, players=[{"name": n} for n in names])
        state = {"id": "ko", "title": "ko", "config": cfg, "players": cfg["players"], "seed_order": names, "rounds": [], "games": {}}
        for number in range(1, 4):
            pairings, bye = swiss.make_pairings(state, number)
            rnd = {"round": number, "bye": bye, "pairings": []}
            for board, (white, black) in enumerate(pairings, start=1):
                gid = f"r{number}b{board}"
                rnd["pairings"].append({"white": white, "black": black, "game_id": gid, "board": board})
                state["games"][gid] = {"result": "1-0" if white < black else "0-1", "end_kind": "board", "status": "finished",
                                       "white": white, "black": black}
            state["rounds"].append(rnd)
        dead = {"Third place"}

        def fake_play(game_id, white, black, cfg_, ts, live_pgn, log, replace_engine, start_ms=None, resume=False):
            game = ts.state["games"][game_id]
            if game["label"] in dead:
                game.update(status="live", moves=[{"ply": 1, "side": "white", "uci": "e2e4", "san": "e4"}])
                raise OSError(errno.ENOSPC, "No space left on device")
            game.update(result="1-0", status="finished", end_kind="board")

        self.patch(swiss, "play_game", fake_play)
        ts = swiss.TournamentState(state, self.tmp / "live" / "ko-tournament.json", self.log)
        ok = swiss.run_knockouts(state, ts, cfg, lambda name: ScriptEngine({"name": name}), lambda e: None, self.log)
        self.assertFalse(ok, "paused: the stage is not over")
        ko = state["knockout"]
        self.assertIsNone(ko["champion"])
        self.assertNotEqual(state.get("stage"), "finished")
        self.assertEqual(state["rounds"][-1]["status"], "live")
        self.assertIn("the game stopped 4 times", state["paused"])
        final = next(m for m in ko["matches"] if m["id"] == "final")
        self.assertTrue(final["winner"], "the final itself was played")
        # After the resume the third place game is played and all three places are known.
        dead.clear()
        ok = swiss.run_knockouts(swiss.prepare_resume(state), ts, cfg, lambda name: ScriptEngine({"name": name}),
                                 lambda e: None, self.log)
        self.assertTrue(ok)
        self.assertTrue(ko["champion"] and ko["runner_up"] and ko["third"])
        self.assertEqual(state["stage"], "finished")


class DiskCheckTest(Base):
    def test_levels(self):
        self.assertEqual(disk_guard.disk_status(self.tmp, usage=free_disk(110))["level"], "critical")
        self.assertEqual(disk_guard.disk_status(self.tmp, usage=free_disk(499))["level"], "critical")
        self.assertEqual(disk_guard.disk_status(self.tmp, usage=free_disk(500))["level"], "low")
        self.assertEqual(disk_guard.disk_status(self.tmp, usage=free_disk(1945))["level"], "low")
        self.assertEqual(disk_guard.disk_status(self.tmp, usage=free_disk(3072))["level"], "ok")
        self.assertEqual(disk_guard.disk_status(self.tmp / "not" / "made" / "yet", usage=free_disk(9000))["level"], "ok")
        self.assertEqual(disk_guard.disk_status(self.tmp, warn_mb=0, refuse_mb=0, usage=free_disk(1))["level"], "ok")
        unknown = disk_guard.disk_status(self.tmp, usage=no_space)
        self.assertEqual((unknown["level"], unknown["free_mb"]), ("unknown", None))
        self.assertEqual(disk_guard.size_text(110), "110 MB")
        self.assertEqual(disk_guard.size_text(1945), "1.9 GB")
        public = disk_guard.public_status(disk_guard.disk_status(self.tmp, usage=free_disk(1945)))
        self.assertEqual(public, {"level": "low", "free_mb": 1900, "warn_mb": 3072, "refuse_mb": 500})
        self.assertNotIn("path", public, "a page never shows a server path")

    def test_every_round_is_checked_and_a_low_disk_is_a_warning_not_a_stop(self):
        self.patch(disk_guard, "USAGE", free_disk(1945))
        state = one_game_state()
        cfg = dict(state["config"], players=state["players"])
        self.assertEqual(swiss.run_tournament(state, cfg, self.tmp / "live" / "t-tournament.json", self.log), 0)
        low = self.logged("DISK LOW")
        self.assertEqual(len(low), 1, self.logs)
        self.assertIn("DISK LOW (start of t, round 1): 1.9 GB free", low[0])
        self.assertIn("under the 3.0 GB warning level", low[0])
        self.assertEqual(state["disk"], {"level": "low", "free_mb": 1900, "warn_mb": 3072, "refuse_mb": 500})
        self.assertTrue(state["finished"], "a warning never stops play")

    def test_a_running_tournament_is_not_stopped_by_a_nearly_full_disk(self):
        self.patch(disk_guard, "USAGE", free_disk(110))
        state = one_game_state()
        cfg = dict(state["config"], players=state["players"])
        self.assertEqual(swiss.run_tournament(state, cfg, self.tmp / "live" / "t-tournament.json", self.log), 0)
        self.assertEqual(len(self.logged("DISK FULL (start of t, round 1): 110 MB free")), 1)
        self.assertIn("The running tournament goes on", self.logged("DISK FULL")[0])
        self.assertEqual(state["disk"]["level"], "critical")

    def test_no_new_tournament_starts_under_500_mb_and_it_starts_when_space_is_back(self):
        live = self.tmp / "live"
        repo = mem.MemoryRepo(self.tmp / "memory")
        cfg = forever.load_forever_config(ROOT / "configs" / "ai-chess-vps.json")
        cfg["forever"]["rosterCheck"] = False
        self.assertEqual((cfg["diskWarnMb"], cfg["diskRefuseMb"]), (3072, 500))
        hooks = forever.ForeverHooks(cfg, repo, None, live)
        self.patch(forever, "log", self.log)
        free = [110, 110, 1945]
        self.patch(disk_guard, "USAGE", lambda path: free_disk(free.pop(0) if len(free) > 1 else free[0])(path))
        started, waits = [], []

        def fake_run(state, cfg_, status_path, log):
            started.append(state["number"])
            state["finished"] = True
            swiss.TournamentState(state, status_path).save()
            return 0

        self.patch(swiss, "run_tournament", fake_run)
        forever.run_forever(cfg, live, repo, None, hooks, max_tournaments=1, sleep=waits.append)
        self.assertEqual(started, [1], "it started once space was back")
        self.assertEqual(waits, [600.0, 600.0], "two refusals, each with a wait before the next look")
        full = self.logged("DISK FULL (before a new tournament): 110 MB free")
        self.assertEqual(len(full), 2, self.logs)
        self.assertIn("under the 500 MB floor. No new tournament starts until space is freed.", full[0])
        self.assertEqual(len(self.logged("NOT STARTING a new tournament")), 2)
        index_of = self.logs.index
        self.assertLess(index_of(self.logged("NOT STARTING")[-1]), index_of(self.logged("starting Tournament #1")[0]))

    def test_a_tournament_that_is_already_running_is_resumed_on_a_nearly_full_disk(self):
        live = self.tmp / "live"
        repo = mem.MemoryRepo(self.tmp / "memory")
        cfg = forever.load_forever_config(ROOT / "configs" / "ai-chess-vps.json")
        cfg["forever"]["rosterCheck"] = False
        hooks = forever.ForeverHooks(cfg, repo, None, live)
        self.patch(forever, "log", self.log)
        state = swiss.new_state(cfg, "aichess-0007-x", title="T #7")
        state["number"] = 7
        path = live / "aichess-0007-x-tournament.json"
        path.write_text(json.dumps(state), encoding="utf-8")
        forever.write_pointer(live, path, state)
        self.patch(disk_guard, "USAGE", free_disk(110))
        resumed = []

        def fake_run(state_, cfg_, status_path, log):
            resumed.append(state_["number"])
            state_["finished"] = True
            return 0

        self.patch(swiss, "run_tournament", fake_run)
        forever.run_forever(cfg, live, repo, None, hooks, max_tournaments=1, sleep=lambda s: None)
        self.assertEqual(resumed, [7])
        self.assertEqual(self.logged("NOT STARTING"), [])

    def test_the_plain_runner_refuses_a_new_tournament_too(self):
        config = self.tmp / "players.json"
        config.write_text(json.dumps({"players": [{"name": "A", "provider": "script", "model": "m"},
                                                  {"name": "B", "provider": "script", "model": "m"}]}), encoding="utf-8")
        self.patch(disk_guard, "USAGE", free_disk(110))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = swiss.main(["--config", str(config), "--no-preflight"])
        self.assertEqual(code, 4)
        self.assertIn("DISK FULL (before a new tournament): 110 MB free", out.getvalue())
        self.assertIn("No new tournament starts until space is freed.", out.getvalue())
        self.assertEqual(list((self.tmp / "live").glob("*-tournament.json")), [], "no tournament was made")

    def test_a_failed_index_write_does_not_end_the_forever_runner(self):
        self.patch(forever, "log", self.log)
        self.assertFalse(forever.attempt("the pointer file", no_space))
        self.assertIn("WRITE FAILED (the pointer file): the disk is full", self.logs[0])
        self.assertTrue(forever.attempt("the pointer file", lambda: None))


class ViewerDiskChipTest(Base):
    def serve(self, state):
        live = self.tmp / "live"
        path = live / f"{state['id']}-tournament.json"
        path.write_text(json.dumps(state), encoding="utf-8")
        (live / "current.json").write_text(json.dumps({"state_path": path.name, "id": state["id"], "number": 1}))
        saved = (viewer.Handler.follow, viewer.Handler.live_dir, viewer.Handler.analyzer, viewer.Handler.annotator,
                 viewer.Handler.commentator, viewer.Handler._follow_cache)
        viewer.Handler.follow, viewer.Handler.live_dir, viewer.Handler._follow_cache = live / "current.json", live, {}
        viewer.Handler.analyzer = viewer.Handler.annotator = viewer.Handler.commentator = None

        def restore():
            (viewer.Handler.follow, viewer.Handler.live_dir, viewer.Handler.analyzer, viewer.Handler.annotator,
             viewer.Handler.commentator, viewer.Handler._follow_cache) = saved

        self.addCleanup(restore)
        server = ThreadingHTTPServer(("127.0.0.1", 0), viewer.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}"

    def test_the_viewer_measures_the_disk_itself_for_the_live_tournament(self):
        # The state file still says "ok" (the runner could not write it): the viewer's own reading wins.
        state = dict(one_game_state("aichess-0001-a"), disk={"level": "ok", "free_mb": 9000}, updated_epoch_ms=0)
        url = self.serve(state)
        self.patch(disk_guard, "USAGE", free_disk(1945))
        got = json.loads(urllib.request.urlopen(url + "/api/tournament", timeout=10).read())
        self.assertEqual(got["disk"], {"level": "low", "free_mb": 1900, "warn_mb": 3072, "refuse_mb": 500})
        self.patch(disk_guard, "USAGE", free_disk(110))
        got = json.loads(urllib.request.urlopen(url + "/api/tournament", timeout=10).read())
        self.assertEqual((got["disk"]["level"], got["disk"]["free_mb"]), ("critical", 110))
        self.patch(disk_guard, "USAGE", free_disk(40_000))
        got = json.loads(urllib.request.urlopen(url + "/api/tournament", timeout=10).read())
        self.assertEqual(got["disk"]["level"], "ok")

    def test_the_notes_card_draws_the_chip(self):
        page = viewer.PAGE
        self.assertIn("const chip = diskChip(data);", page)
        self.assertIn("names.length || benched.length || chip", page, "the card shows for the chip alone")
        self.assertIn('diskRow + benchRows', page)
        self.assertIn('<span class="chip disk ${chip.level}"', page)
        self.assertIn("#agents .chip.disk", page)

    @unittest.skipUnless(NODE, "node is not installed")
    def test_chip_text(self):
        page = viewer.PAGE
        source = page[page.index("// DISK-CHIP-START"):page.index("// DISK-CHIP-END")]
        cases = [{"disk": {"level": "low", "free_mb": 1900, "warn_mb": 3072, "refuse_mb": 500}},
                 {"disk": {"level": "critical", "free_mb": 110, "warn_mb": 3072, "refuse_mb": 500}},
                 {"disk": {"level": "ok", "free_mb": 9000}}, {"disk": {"level": "unknown", "free_mb": None}}, {},
                 {"archived": True, "disk": {"level": "critical", "free_mb": 110}},
                 {"disk": {"level": "low", "free_mb": None}}]
        script = source + f"\nconsole.log(JSON.stringify({json.dumps(cases)}.map(diskChip)));"
        out = json.loads(subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=60, check=True).stdout)
        low, critical, ok, unknown, none, archived, no_number = out
        self.assertEqual((low["level"], low["free"], low["tail"]), ("low", "1.9 GB", ""))
        self.assertIn("Under 3 GB free", low["title"])
        self.assertEqual((critical["level"], critical["free"]), ("critical", "110 MB"))
        self.assertEqual(critical["tail"], "the next tournament waits for space")
        self.assertIn("Under 500 MB free", critical["title"])
        self.assertEqual([ok, unknown, none, archived], [None, None, None, None])
        self.assertEqual((no_number["level"], no_number["free"]), ("low", ""), "no made-up number")


class NoDashTest(unittest.TestCase):
    def test_no_long_dashes_in_the_new_code(self):
        for rel in ("tools/disk_guard.py", "tests/test_disk_full_guard.py"):
            text = (ROOT / rel).read_text(encoding="utf-8")
            self.assertNotIn(chr(0x2014), text, rel)
            self.assertNotIn(chr(0x2013), text, rel)


if __name__ == "__main__":
    unittest.main()
