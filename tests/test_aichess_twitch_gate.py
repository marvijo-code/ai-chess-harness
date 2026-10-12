"""Twitch gate: the daily show window, the relay command and the switch between Twitch and nowhere."""
import datetime as dt
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "aichess_stream"))
import twitch_gate as tg  # noqa: E402

UTC = dt.timezone.utc


def utc(*a):
    return dt.datetime(*a, tzinfo=UTC)


class ConfigTest(unittest.TestCase):
    def test_no_lines_means_always(self):
        self.assertEqual(tg.parse_config("").mode, "always")

    def test_daily_values(self):
        show = tg.parse_config("# c\nTWITCH_SHOW_MODE=daily\nTWITCH_SHOW_START=07:30\nTWITCH_SHOW_MINUTES=90\nTWITCH_SHOW_TZ=UTC\n")
        self.assertEqual((show.mode, show.start_minute, show.minutes, show.tz), ("daily", 450, 90, "UTC"))
        self.assertEqual(show.start_text, "07:30")

    def test_bad_values_are_refused(self):
        for text in ("TWITCH_SHOW_MODE=sometimes", "TWITCH_SHOW_START=25:00", "TWITCH_SHOW_START=7pm",
                     "TWITCH_SHOW_MINUTES=0", "TWITCH_SHOW_MINUTES=two", "TWITCH_SHOW_TZ=Mars/Olympus"):
            with self.assertRaises(ValueError, msg=text):
                tg.parse_config(text)

    def test_render_reads_back(self):
        show = tg.Show("daily", 19 * 60, 120, "Africa/Johannesburg")
        self.assertEqual(tg.parse_config(tg.render_config(show)), show)


class WindowTest(unittest.TestCase):
    SAST = tg.Show("daily", 19 * 60, 120, "Africa/Johannesburg")   # 17:00 to 19:00 UTC

    def test_always_and_off(self):
        self.assertEqual(tg.window(tg.Show("always"), utc(2026, 10, 13, 3)), (True, None))
        self.assertEqual(tg.window(tg.Show("off"), utc(2026, 10, 13, 18)), (False, None))

    def test_before_the_show(self):
        on, change = tg.window(self.SAST, utc(2026, 10, 13, 16, 59, 59))
        self.assertFalse(on)
        self.assertEqual(change, utc(2026, 10, 13, 17))

    def test_first_and_last_second(self):
        self.assertEqual(tg.window(self.SAST, utc(2026, 10, 13, 17)), (True, utc(2026, 10, 13, 19)))
        self.assertEqual(tg.window(self.SAST, utc(2026, 10, 13, 18, 59, 59)), (True, utc(2026, 10, 13, 19)))

    def test_after_the_show_points_at_tomorrow(self):
        self.assertEqual(tg.window(self.SAST, utc(2026, 10, 13, 19)), (False, utc(2026, 10, 14, 17)))

    def test_show_over_midnight(self):
        late = tg.Show("daily", 23 * 60, 120, "UTC")
        self.assertEqual(tg.window(late, utc(2026, 10, 14, 0, 30)), (True, utc(2026, 10, 14, 1)))
        self.assertEqual(tg.window(late, utc(2026, 10, 14, 1)), (False, utc(2026, 10, 14, 23)))

    def test_whole_day_is_always(self):
        self.assertEqual(tg.window(tg.Show("daily", 0, 1440, "UTC"), utc(2026, 10, 13, 5)), (True, None))

    def test_clock_change_keeps_the_local_time(self):
        show = tg.Show("daily", 19 * 60, 60, "Europe/Berlin")
        self.assertEqual(tg.window(show, utc(2026, 10, 24, 12))[1], utc(2026, 10, 24, 17))   # summer time, +2
        self.assertEqual(tg.window(show, utc(2026, 10, 26, 12))[1], utc(2026, 10, 26, 18))   # winter time, +1


class RelayTest(unittest.TestCase):
    def test_nowhere(self):
        cmd = tg.relay_cmd("rtmp://127.0.0.1:19350/live/aichess", None)
        self.assertEqual(cmd[-3:], ["-f", "null", "-"])
        self.assertIn("-listen", cmd)
        self.assertEqual(cmd[cmd.index("-c") + 1], "copy")

    def test_to_twitch(self):
        cmd = tg.relay_cmd("rtmp://127.0.0.1:19350/live/aichess", "rtmp://ingest/app/KEY")
        self.assertEqual(cmd[-1], "rtmp://ingest/app/KEY")
        self.assertEqual(cmd[cmd.index("-i") + 1], "rtmp://127.0.0.1:19350/live/aichess")
        self.assertNotIn("libx264", cmd)

    def test_key_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "twitch.env")
            Path(p).write_text("TWITCH_RTMP_URL=rtmp://in.example/app/\nTWITCH_STREAM_KEY=live_abc\n", encoding="utf-8")
            self.assertEqual(tg.read_twitch_target(p), ("rtmp://in.example/app/live_abc", "live_abc"))
            Path(p).write_text("TWITCH_STREAM_KEY=live_abc\n", encoding="utf-8")
            self.assertEqual(tg.read_twitch_target(p)[0], tg.DEFAULT_TWITCH_URL + "/live_abc")
            Path(p).write_text("TWITCH_RTMP_URL=rtmp://in.example/app\n", encoding="utf-8")
            self.assertEqual(tg.read_twitch_target(p), (None, None))
            self.assertEqual(tg.read_twitch_target(os.path.join(d, "missing")), (None, None))

    def test_key_never_in_a_log_line(self):
        self.assertEqual(tg.mask("open rtmp://x/app/live_abc failed", ["live_abc", None]), "open rtmp://x/app/<key> failed")


class FakeProc:
    n = 0

    def __init__(self, cmd):
        FakeProc.n += 1
        self.pid, self.cmd, self.returncode, self.stopped = 1000 + FakeProc.n, cmd, None, False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.stopped, self.returncode = True, -15

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.terminate()


class GateTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.paths = {"gate": "rtmp://127.0.0.1:19350/live/aichess", "key_file": os.path.join(self.dir, "twitch.env"),
                      "show_file": os.path.join(self.dir, "show.env"), "run_dir": self.dir}
        Path(self.paths["key_file"]).write_text("TWITCH_RTMP_URL=rtmp://in.example/app\nTWITCH_STREAM_KEY=live_abc\n", encoding="utf-8")
        self.spawned, self.secrets, self.t = [], [], 1000.0

        def spawn(cmd, secrets):
            self.spawned.append(FakeProc(cmd))
            self.secrets.append(secrets)
            return self.spawned[-1]
        self.gate = tg.Gate(self.paths, spawn=spawn, clock=lambda: self.t)

    def show(self, text):
        Path(self.paths["show_file"]).write_text(text, encoding="utf-8")

    def step(self, now):
        with redirect_stdout(io.StringIO()) as out:
            st = self.gate.step(now)
        self.log = out.getvalue()
        return st

    def test_no_schedule_file_sends_all_day(self):
        st = self.step(utc(2026, 10, 13, 3))
        self.assertEqual((st["mode"], st["on"], st["sending"]), ("always", True, "twitch"))
        self.assertEqual(self.spawned[0].cmd[-1], "rtmp://in.example/app/live_abc")
        self.assertNotIn("live_abc", self.log)
        self.assertEqual(self.secrets[0], ["live_abc"])

    def test_daily_show_opens_and_closes(self):
        self.show("TWITCH_SHOW_MODE=daily\nTWITCH_SHOW_START=19:00\nTWITCH_SHOW_MINUTES=120\nTWITCH_SHOW_TZ=Africa/Johannesburg\n")
        st = self.step(utc(2026, 10, 13, 16, 0))
        self.assertEqual((st["on"], st["sending"], st["next_change"]), (False, "nowhere", "2026-10-13T17:00:00+00:00"))
        self.assertEqual(self.spawned[0].cmd[-3:], ["-f", "null", "-"])
        self.step(utc(2026, 10, 13, 16, 30))
        self.assertEqual(len(self.spawned), 1, "no change = the relay is left alone")

        st = self.step(utc(2026, 10, 13, 17, 0))
        self.assertTrue(self.spawned[0].stopped)
        self.assertEqual((st["on"], st["sending"], len(self.spawned)), (True, "twitch", 2))
        self.assertEqual(self.spawned[1].cmd[-1], "rtmp://in.example/app/live_abc")

        st = self.step(utc(2026, 10, 13, 19, 0))
        self.assertTrue(self.spawned[1].stopped)
        self.assertEqual((st["on"], st["sending"], st["next_change"]), (False, "nowhere", "2026-10-14T17:00:00+00:00"))

    def test_schedule_change_is_picked_up_without_a_restart(self):
        self.show("TWITCH_SHOW_MODE=off\n")
        self.assertEqual(self.step(utc(2026, 10, 13, 18))["sending"], "nowhere")
        self.show("TWITCH_SHOW_MODE=always\nTWITCH_SHOW_MINUTES=120\n")
        self.assertEqual(self.step(utc(2026, 10, 13, 18))["sending"], "twitch")

    def test_bad_schedule_keeps_the_last_good_one(self):
        self.show("TWITCH_SHOW_MODE=off\n")
        self.step(utc(2026, 10, 13, 18))
        self.show("TWITCH_SHOW_MODE=whenever\nTWITCH_SHOW_START=19:00\n")
        st = self.step(utc(2026, 10, 13, 18))
        self.assertEqual((st["mode"], st["sending"]), ("off", "nowhere"))
        self.assertIn("not valid", self.log)

    def test_bad_schedule_at_start_means_off_not_all_day(self):
        self.show("TWITCH_SHOW_MODE=whenever\n")
        st = self.step(utc(2026, 10, 13, 18))
        self.assertEqual((st["mode"], st["sending"]), ("off", "nowhere"))

    def test_show_on_without_a_key_sends_nowhere_and_says_so(self):
        os.remove(self.paths["key_file"])
        st = self.step(utc(2026, 10, 13, 18))
        self.assertEqual((st["on"], st["sending"], st["has_key"]), (True, "nowhere", False))
        self.assertIn("no Twitch key", self.log)

    def test_dead_relay_is_started_again_with_a_pause_when_it_dies_fast(self):
        self.step(utc(2026, 10, 13, 18))
        self.spawned[0].returncode = 1
        self.t += 1                                  # died after 1 s: wait before the next try
        st = self.step(utc(2026, 10, 13, 18, 0, 2))
        self.assertEqual((st["sending"], st["relay_restarts"], len(self.spawned)), (None, 1, 1))
        self.t += 10
        self.assertEqual(self.step(utc(2026, 10, 13, 18, 0, 12))["sending"], "twitch")
        self.spawned[1].returncode = 0
        self.t += 3600                               # ran a long time (the stream restarted): start at once
        self.assertEqual(self.step(utc(2026, 10, 13, 19, 0, 12))["sending"], "twitch")
        self.assertEqual(len(self.spawned), 3)


class StatusLineTest(unittest.TestCase):
    BASE = {"schedule": "daily 19:00 for 120 min (Africa/Johannesburg)", "tz": "Africa/Johannesburg"}

    def test_off_air_names_the_local_start(self):
        st = dict(self.BASE, on=False, sending="nowhere", next_change="2026-10-13T17:00:00+00:00", updated="2026-10-13T14:30:00+00:00")
        line = tg.status_line(st, utc(2026, 10, 13, 14, 30, 5))
        self.assertIn("now off air, starts Tue 19:00 (in 2h29m)", line)
        self.assertNotIn("<-", line)

    def test_on_air(self):
        st = dict(self.BASE, on=True, sending="twitch", next_change="2026-10-13T19:00:00+00:00", updated="2026-10-13T17:30:00+00:00")
        self.assertIn("now ON AIR, ends Tue 21:00 (in 1h30m)", tg.status_line(st, utc(2026, 10, 13, 17, 30)))

    def test_on_but_not_sending_and_stale_are_flagged(self):
        st = dict(self.BASE, on=True, sending="nowhere", next_change=None, updated="2026-10-13T17:00:00+00:00")
        line = tg.status_line(st, utc(2026, 10, 13, 17, 5))
        self.assertIn("nothing is sent", line)
        self.assertIn("has not reported for 300 s", line)


class SetCommandTest(unittest.TestCase):
    def run_set(self, d, *args):
        env = {"AICHESS_TWITCH_SHOW_FILE": os.path.join(d, "show.env"), "RUNTIME_DIRECTORY": d}
        old = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        try:
            with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
                rc = tg.main(["set", *args])
            return rc, out.getvalue() + err.getvalue()
        finally:
            for k, v in old.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

    def test_set_daily_then_always_keeps_the_times(self):
        with tempfile.TemporaryDirectory() as d:
            rc, out = self.run_set(d, "--mode", "daily", "--start", "19:00", "--minutes", "120", "--tz", "Africa/Johannesburg")
            self.assertEqual(rc, 0, out)
            self.assertIn("daily 19:00 for 120 min", out)
            rc, out = self.run_set(d, "--mode", "always")
            self.assertEqual(rc, 0, out)
            show = tg.parse_config(Path(d, "show.env").read_text(encoding="utf-8"))
            self.assertEqual((show.mode, show.start_text, show.minutes), ("always", "19:00", 120))

    def test_bad_input_saves_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            rc, out = self.run_set(d, "--mode", "daily", "--start", "7pm")
            self.assertEqual(rc, 2)
            self.assertIn("not saved", out)
            self.assertFalse(Path(d, "show.env").exists())

    def test_status_without_a_gate(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ["RUNTIME_DIRECTORY"] = d
            try:
                with redirect_stdout(io.StringIO()) as out:
                    rc = tg.main(["status"])
            finally:
                os.environ.pop("RUNTIME_DIRECTORY", None)
            self.assertEqual(rc, 1)
            self.assertIn("not running", out.getvalue())
            Path(d, "status.json").write_text(json.dumps({"schedule": "always on (24 hours a day)", "on": True,
                                                         "sending": "twitch", "next_change": None, "tz": "UTC"}), encoding="utf-8")
            os.environ["RUNTIME_DIRECTORY"] = d
            try:
                with redirect_stdout(io.StringIO()) as out:
                    rc = tg.main(["status"])
            finally:
                os.environ.pop("RUNTIME_DIRECTORY", None)
            self.assertEqual(rc, 0)
            self.assertIn("now ON AIR", out.getvalue())


class StreamScriptTest(unittest.TestCase):
    STREAM = (ROOT / "tools" / "aichess_stream" / "stream.sh").read_text(encoding="utf-8")

    def test_gate_takes_the_second_output_and_youtube_stays_the_main_leg(self):
        self.assertIn('TWITCH_GATE="${AICHESS_TWITCH_GATE:-}"', self.STREAM)
        self.assertIn('tee_spec "$YT_URL/$YT_KEY" "$TWITCH_GATE"', self.STREAM)
        self.assertLess(self.STREAM.index('if [ -n "$TWITCH_GATE" ]; then'), self.STREAM.index("elif read_twitch_key; then"))
        self.assertIn("[f=flv:onfail=abort]%s|[f=fifo:", self.STREAM)

    def test_installer_ships_the_gate(self):
        inst = (ROOT / "tools" / "aichess_stream" / "install-services.sh").read_text(encoding="utf-8")
        self.assertIn("twitch_gate.py", inst)
        self.assertIn("ALL=(runner viewer pusher stream twitch-gate)", inst)
        unit = (ROOT / "tools" / "aichess_stream" / "systemd" / "ai-chess-twitch-gate.service").read_text(encoding="utf-8")
        self.assertIn("twitch_gate.py run", unit)
        self.assertIn("EnvironmentFile=-__HOME__/.config/ai-chess/stream.env", unit)

    @unittest.skipUnless(shutil.which("bash"), "bash is not installed")
    def test_shell_scripts_parse(self):
        for name in ("stream.sh", "status.sh", "install-services.sh"):
            r = subprocess.run(["bash", "-n", str(ROOT / "tools" / "aichess_stream" / name)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, name + ": " + r.stderr)


if __name__ == "__main__":
    unittest.main()
