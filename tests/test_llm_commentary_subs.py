"""Subscription routes, free voice, pacing, tournament switches, Stockfish depth and player notes (no network)."""
import io
import json
import os
import sys
import tempfile
import time
import unittest
import wave
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import llm_commentary as lc  # noqa: E402
from test_llm_commentary import FakeHttp, fake  # noqa: E402

CLEAN_ENV = {k: v for k, v in os.environ.items() if not k.startswith("COMMENTARY_")}


def live_state(tid="t1", moves=1, players=None, updated=None):
    game = {"id": "r1b1", "round": 1, "board": 1, "white": "Sonnet 5.5", "black": "Stockfish 19", "status": "live",
            "result": "*", "moves": [{"ply": i + 1, "side": "white" if i % 2 == 0 else "black", "san": "e4"}
                                     for i in range(moves)]}
    return {"id": tid, "games": {"r1b1": game}, "rounds": [{"round": 1, "status": "live", "pairings": []}],
            "players": players or [{"name": "Sonnet 5.5"}, {"name": "Stockfish 19", "provider": "uci", "depth": 3}],
            "current_round": 1, "updated_epoch_ms": (time.time() if updated is None else updated) * 1000}


def make(state, name="t1", **kw):
    tmp = Path(tempfile.mkdtemp())
    path = tmp / f"{name}-tournament.json"
    path.write_text(json.dumps(state), encoding="utf-8")
    c = lc.Commentator(path, tmp / f"{name}-commentary", log=lambda _m: None, **kw)
    fake(c)
    c.always = True
    return c, path


class RouteSelectionTest(unittest.TestCase):
    def test_default_is_codex_luna_at_low_effort_with_the_free_voice(self):
        got = lc.resolve_settings(env={})
        self.assertEqual((got["route"], got["model"], got["effort"]), ("codex", "gpt-6-luna", "low"))
        self.assertEqual((got["tts"], got["voice"], got["calls_per_hour"]), ("edge", lc.EDGE_VOICE, 90))

    def test_routes_are_configurable(self):
        self.assertEqual(lc.resolve_settings(env={"COMMENTARY_ROUTE": "claude"})["model"], "haiku")
        self.assertEqual(lc.resolve_settings(env={"COMMENTARY_ROUTE": "opencode-go"})["route"], "opencode-go")
        got = lc.resolve_settings(route="codex", model="gpt-6-sol", effort="medium", calls_per_hour=30, env={})
        self.assertEqual((got["model"], got["effort"], got["calls_per_hour"]), ("gpt-6-sol", "medium", 30))
        with self.assertRaises(ValueError):
            lc.resolve_settings(env={"COMMENTARY_ROUTE": "gemini"})

    def test_openrouter_needs_the_explicit_opt_in(self):
        for env in ({"COMMENTARY_ROUTE": "openrouter"}, {"COMMENTARY_TTS": "openrouter"}):
            with self.assertRaises(ValueError, msg=env):
                lc.resolve_settings(env=env)
            got = lc.resolve_settings(env={**env, "COMMENTARY_ALLOW_OPENROUTER": "1"})
            self.assertTrue(got["allow_openrouter"])
        with mock.patch.dict(os.environ, CLEAN_ENV, clear=True):
            c, _ = make(live_state(), route="openrouter")
            self.assertIn("COMMENTARY_ALLOW_OPENROUTER", c.readiness())
            self.assertFalse(c.start(), "a refused metered route stays off")

    def test_codex_call_is_locked_down_and_the_prompt_prefix_is_static(self):
        seen = []

        def runner(argv, prompt, timeout, workdir, strip):
            seen.append((argv, prompt, strip))
            Path(argv[argv.index("-o") + 1]).write_text("Sonnet castles short and the king is safe.", encoding="utf-8")
            return '{"type":"turn.completed"}\n'

        with mock.patch.dict(os.environ, {**CLEAN_ENV, "COMMENTARY_CODEX_BIN": "codex-test"}, clear=True):
            c, _ = make(live_state())
            c.text_backend, c.runner = None, runner
            self.assertEqual(c._chat(lc.COMMENTATOR_PROMPT, "Board 1 facts", 120, 0.8),
                             "Sonnet castles short and the king is safe.")
            c._chat(lc.EVENT_PROMPT.format(words="40 to 60"), "Round 2 facts", 260, 0.9)
        argv, prompt, strip = seen[0]
        self.assertEqual(argv[:2], ["codex-test", "exec"])
        self.assertIn("--ignore-user-config", argv)
        self.assertEqual(argv[argv.index("-m") + 1], "gpt-6-luna")
        self.assertIn("model_reasoning_effort=low", argv)
        for feature in ("shell_tool", "unified_exec", "apps", "browser_use", "multi_agent", "plugins", "hooks"):
            self.assertIn(feature, argv)
        self.assertIn("OPENAI_API_KEY", strip, "a metered key never reaches the CLI")
        self.assertTrue(prompt.startswith(lc.HOST_PROMPT + "\n\nMODE LINE"))
        self.assertTrue(seen[1][1].startswith(lc.HOST_PROMPT + "\n\nMODE EVENT"), "byte-identical prefix on every call")
        self.assertTrue(prompt.endswith("Board 1 facts"), "the changing facts come last")

    def test_a_codex_tool_call_is_rejected_and_backs_off(self):
        def runner(argv, prompt, timeout, workdir, strip):
            Path(argv[argv.index("-o") + 1]).write_text("text", encoding="utf-8")
            return '{"type":"item.completed","item":{"type":"command_execution"}}\n'

        with mock.patch.dict(os.environ, {**CLEAN_ENV, "COMMENTARY_CODEX_BIN": "codex-test"}, clear=True):
            c, _ = make(live_state())
            c.text_backend, c.runner = None, runner
            self.assertIsNone(c._chat(lc.COMMENTATOR_PROMPT, "x", 120, 0.8))
        self.assertGreater(c._backoff_until, time.time())

    def test_claude_route_has_no_tools_and_the_static_system_prompt(self):
        seen = []

        def runner(argv, prompt, timeout, workdir, strip):
            seen.append((argv, prompt, strip))
            return json.dumps({"type": "result", "is_error": False, "result": "Haiku says hello."})

        with mock.patch.dict(os.environ, {**CLEAN_ENV, "COMMENTARY_CLAUDE_BIN": "claude-test"}, clear=True):
            c, _ = make(live_state(), route="claude")
            c.text_backend, c.runner = None, runner
            self.assertEqual(c._chat(lc.COMMENTATOR_PROMPT, "facts", 120, 0.8), "Haiku says hello.")
        argv, prompt, strip = seen[0]
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertEqual(argv[argv.index("--system-prompt") + 1], lc.HOST_PROMPT)
        self.assertEqual(argv[argv.index("--model") + 1], "haiku")
        self.assertIn("ANTHROPIC_API_KEY", strip)
        self.assertEqual(prompt, lc.COMMENTATOR_PROMPT + "\n\nfacts")

    def test_a_plan_limit_pauses_the_route_for_long(self):
        def runner(*_a):
            raise lc.RouteError("codex exited 1: usageLimitExceeded")

        with mock.patch.dict(os.environ, {**CLEAN_ENV, "COMMENTARY_CODEX_BIN": "codex-test"}, clear=True):
            c, _ = make(live_state())
            c.text_backend, c.runner = None, runner
            self.assertIsNone(c._chat(lc.COMMENTATOR_PROMPT, "x", 120, 0.8))
        self.assertGreater(c._backoff_until, time.time() + lc.UNAVAILABLE_BACKOFF_SECONDS - 5)


class PacingTest(unittest.TestCase):
    def test_hourly_cap_and_burst(self):
        now = [1000.0]
        pacer = lc.Pacer(90, 6, clock=lambda: now[0])
        self.assertEqual(sum(pacer.take() for _ in range(50)), 6, "a burst is capped at the bucket size")
        taken = 6
        for _ in range(3600):   # one call attempt per second for an hour
            now[0] += 1
            taken += pacer.take()
        self.assertLessEqual(taken, 96)
        self.assertLessEqual(pacer.last_hour(), 90, "never more than 90 calls in any rolling hour")
        self.assertGreaterEqual(pacer.last_hour(), 85, "the cap is used, not starved")

    def test_low_priority_stops_at_three_quarters(self):
        now = [0.0]
        pacer = lc.Pacer(8, 100, clock=lambda: now[0])
        self.assertEqual(sum(pacer.take(low=True) for _ in range(20)), 6)
        self.assertEqual(sum(pacer.take() for _ in range(20)), 2, "big moments keep the last quarter")
        now[0] += 3601
        self.assertTrue(pacer.take(low=True), "a new hour, new room")

    def test_the_commentator_makes_no_call_past_the_cap(self):
        c, path = make(live_state(), calls_per_hour=2)
        c.gating = False
        c._events_done.add("opening")
        made = 0
        for ply in range(2, 8):
            path.write_text(json.dumps(live_state(moves=ply)), encoding="utf-8")
            c._busy_until = 0
            made += c.tick() is not None
        self.assertEqual(made, 2)
        self.assertEqual(len([p for p, _ in c.http.calls if p == "/chat/completions"]), 2)
        self.assertEqual(c.calls_total, 2)


class TournamentSwitchTest(unittest.TestCase):
    def test_a_new_tournament_id_resets_memory_and_opens_with_a_fresh_intro(self):
        c, path = make(live_state("t1", moves=4))
        c.gating = False
        first = c.tick()
        self.assertEqual(first["event"], "opening")
        c._busy_until = 0
        c.tick()
        self.assertTrue(c._done_ply)
        seq_before = c._seq
        path.write_text(json.dumps(live_state("t2", moves=2)), encoding="utf-8")
        c._busy_until = 0
        clip = c.tick()
        self.assertEqual(c.tournament, "t2")
        self.assertEqual(clip["event"], "opening")
        self.assertGreater(clip["seq"], seq_before, "clip numbers keep rising across tournaments")
        facts = c.http.calls[-2][1]["messages"][1]["content"]
        self.assertIn("brand new tournament", facts)
        self.assertEqual([x["seq"] for x in c.clips_all(0)], [clip["seq"]], "old clips are gone with the old tournament")
        self.assertEqual(c._done_ply, {}, "per-board memory starts empty")

    def test_follow_dir_moves_to_the_newest_tournament_file(self):
        c, path = make(live_state("t1", moves=2))
        c.follow_dir = path.parent
        c.gating = False
        c.tick()
        later = path.parent / "t9-tournament.json"
        later.write_text(json.dumps(live_state("t9", moves=1)), encoding="utf-8")
        os.utime(later, (time.time() + 5, time.time() + 5))
        c._busy_until = 0
        clip = c.tick()
        self.assertEqual(c.state_path, later)
        self.assertEqual(c.out_dir.name, "t9-commentary")
        self.assertEqual(clip["event"], "opening")
        self.assertIsNotNone(c.audio_path(clip["audio"]))
        ledger = json.loads((c.out_dir / "commentary-ledger.json").read_text(encoding="utf-8"))
        self.assertEqual(ledger["tournament"], "t9")
        restarted = lc.Commentator(later, path.parent / "t9-commentary", log=lambda _m: None)
        self.assertGreaterEqual(restarted._seq, clip["seq"], "a restart continues the clip numbers")


class WavTest(unittest.TestCase):
    def test_clips_are_riff_wave_24k_mono_16bit(self):
        c, _ = make(live_state(moves=1))
        c.gating = False
        c._events_done.add("opening")
        clip = c.tick()
        data = (c.out_dir / clip["audio"]).read_bytes()
        self.assertEqual((data[:4], data[8:12]), (b"RIFF", b"WAVE"))
        with wave.open(io.BytesIO(data)) as w:
            self.assertEqual((w.getframerate(), w.getnchannels(), w.getsampwidth()), (24000, 1, 2))
        self.assertAlmostEqual(clip["seconds"], 1.0)

    def test_long_audio_is_cut_below_the_relay_limit(self):
        tmp = Path(tempfile.mkdtemp()) / "clip-1.wav"
        seconds = lc.write_wav(tmp, b"\x00\x01" * (24000 * 200))
        self.assertEqual(seconds, lc.MAX_CLIP_SECONDS)
        self.assertLess(tmp.stat().st_size, 4 * 1024 * 1024)

    def test_edge_mp3_is_decoded_to_24k_mono_pcm_with_ffmpeg(self):
        calls = []

        def runner(argv, **kw):
            calls.append((argv, kw))
            return mock.Mock(returncode=0, stdout=b"\x00\x00" * 10, stderr=b"")

        with mock.patch.dict(os.environ, {"COMMENTARY_FFMPEG": "ffmpeg-test"}):
            self.assertEqual(lc.mp3_to_pcm(b"ID3fake", runner=runner), b"\x00\x00" * 10)
        argv, kw = calls[0]
        self.assertEqual(argv[0], "ffmpeg-test")
        for flag, value in (("-ar", "24000"), ("-ac", "1"), ("-f", "s16le")):
            self.assertEqual(argv[argv.index(flag) + 1], value)
        self.assertEqual(kw["input"], b"ID3fake")

    def test_a_failed_voice_keeps_the_host_silent_and_spends_nothing(self):
        logs = []
        c, _ = make(live_state(moves=1))
        c.log = logs.append
        c.gating = False
        c._events_done.add("opening")

        def broken(_text):
            raise OSError("edge-tts unreachable")

        c.tts_backend = broken
        self.assertIsNone(c.tick())
        self.assertEqual(c.clips_all(0), [])
        self.assertEqual(c.spent_usd, 0.0)
        self.assertTrue(any("line stays silent" in m for m in logs))
        self.assertEqual(c._done_ply.get("r1b1"), 1, "the moment is not retried (no second call for it)")


class PlayersTest(unittest.TestCase):
    def test_stockfish_depth_is_named_and_each_rise_is_announced_once(self):
        state = live_state(moves=1)
        self.assertEqual(lc.stockfish_depths(state), {"Stockfish 19": 3})
        self.assertIn("searching at depth three", lc.player_label(state, "Stockfish 19"))
        self.assertEqual(lc.spoken("Stockfish 19 (depth 4)"), "Stockfish at depth four")
        c, path = make(state)
        c._events_done.add("opening")
        self.assertIsNone(c._depth_event(state), "first sight: the baseline, no announcement")
        facts = lc.build_context(state["games"]["r1b1"], {}, 1, extra="\n".join(c._player_facts(state, state["games"]["r1b1"])))
        self.assertIn("searching at depth three", facts)
        import datetime as dt
        state["games"]["r0b1"] = {"id": "r0b1", "white": "Stockfish 19", "black": "Sonnet 5.5", "status": "finished",
                                  "result": "0-1", "end": dt.datetime.now().astimezone().isoformat(), "moves": []}
        state["players"][1]["depth"] = 4
        event = c._depth_event(state)
        self.assertEqual((event["key"], event["game"]), ("sfdepth-4", "r0b1"))
        self.assertIn("Sonnet five point five just beat Stockfish", event["facts"])
        self.assertIn("from three to four", event["facts"])
        c._events_done.add("sfdepth-4")
        self.assertIsNone(c._depth_event(state), "announced once")

    def test_a_players_note_is_offered_now_and_then_and_quoted_once(self):
        players = [{"name": "Sonnet 5.5", "note": {"text": "Stockfish punishes loose knights; keep pieces defended."}},
                   {"name": "Stockfish 19", "provider": "uci", "depth": 3}]
        c, path = make(live_state(moves=1, players=players))
        c.gating = False
        c._events_done.add("opening")
        c.tick()
        prompt = c.http.calls[-2][1]["messages"][1]["content"]
        self.assertIn("Stockfish punishes loose knights", prompt)
        path.write_text(json.dumps(live_state(moves=2, players=players)), encoding="utf-8")
        c._busy_until = 0
        c.tick()
        self.assertNotIn("loose knights", c.http.calls[-2][1]["messages"][1]["content"], "one note, one offer")


if __name__ == "__main__":
    unittest.main()
