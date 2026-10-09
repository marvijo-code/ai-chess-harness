"""Pusher logic tests with a fake local viewer and a fake relay (no network)."""

from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import aichess_push as push  # noqa: E402

LOCAL = "http://local"
RELAY = "http://relay"


class Clock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeHttp:
    """A fake local viewer plus a fake relay ingest."""

    def __init__(self):
        self.state = {"id": "t-1", "updated_epoch_ms": 1, "state_age_s": 0.5, "server_now_ms": 10,
                      "games": {"r1b1": {"status": "live", "plies": 4}, "r1b2": {"status": "finished", "plies": 30}}}
        self.clips = [{"seq": s, "game": "r1b1", "audio": f"clip-{s}.wav", "text": f"c{s}", "age_s": 1.0}
                      for s in range(1, 51)]
        self.thinking = {("r1b1", 5): {"exists": True, "size": 10, "text": "0123456789", "from": 0, "truncated": False}}
        self.local_down = False
        self.relay_down = False
        self.relay_health = {"ok": True, "last_clip_seq": 0, "tournament_id": None}
        self.posts: list[tuple[str, bytes, dict]] = []
        self.gets: list[str] = []

    def get(self, url, timeout=10):
        self.gets.append(url)
        if url.startswith(RELAY):
            if self.relay_down:
                raise ConnectionRefusedError()
            return 200, json.dumps(self.relay_health).encode()
        if self.local_down:
            raise ConnectionRefusedError()
        path = url[len(LOCAL):]
        if path == "/api/tournament":
            return 200, json.dumps(self.state).encode()
        if path.startswith("/api/commentary?after="):
            after = int(path.split("=")[1])
            return 200, json.dumps({"enabled": True, "clips": [c for c in self.clips if c["seq"] > after]}).encode()
        if path.startswith("/api/commentary/audio/"):
            return 200, b"RIFF....WAVE" + path.encode()
        if path.startswith("/api/thinking"):
            q = dict(part.split("=") for part in path.split("?")[1].split("&"))
            rec = self.thinking.get((q["game"], int(q["ply"])))
            return 200, json.dumps(rec or {"exists": False, "size": 0, "text": "", "from": 0, "truncated": False}).encode()
        if path == "/api/viewer-version":
            return 200, b'{"version": "v1"}'
        return 404, b"not found"

    def post(self, url, body, headers, timeout=30):
        if self.relay_down:
            raise ConnectionRefusedError()
        self.posts.append((url[len(RELAY):], body, headers))
        return 200, b'{"ok": true}'

    def paths(self, prefix: str = "") -> list[str]:
        return [p for p, _, _ in self.posts if p.startswith(prefix)]


def make(tmp_path, http=None, wall=None, mono=None):
    http = http or FakeHttp()
    wall = wall or Clock(1_700_000_000.0)
    mono = mono or Clock(1000.0)
    logs: list[str] = []
    p = push.Pusher(LOCAL, RELAY, "secret-token-value-123", tmp_path, http=http, clock=wall, mono=mono, log=logs.append)
    return p, http, mono, logs


def test_fingerprint_ignores_volatile_fields():
    a = {"id": "x", "games": {"g": {"plies": 3}}, "state_age_s": 1.0, "server_now_ms": 5}
    b = dict(a, state_age_s=99.0, server_now_ms=12345)
    c = dict(a, games={"g": {"plies": 4}})
    assert push.state_fingerprint(a) == push.state_fingerprint(b)
    assert push.state_fingerprint(a) != push.state_fingerprint(c)


def test_select_new_clips_first_start_and_gap():
    clips = [{"seq": s} for s in range(1, 418)]
    assert [c["seq"] for c in push.select_new_clips(clips, None)] == list(range(398, 418))
    assert [c["seq"] for c in push.select_new_clips(clips, 410)] == list(range(411, 418))
    assert [c["seq"] for c in push.select_new_clips(clips, 100)] == list(range(398, 418))   # long gap: newest 20
    assert push.select_new_clips(clips, 417) == []


def test_live_boards():
    state = {"games": {"a": {"status": "live", "plies": 0}, "b": {"status": "live", "moves": [1, 2]},
                       "c": {"status": "finished", "plies": 9}}}
    assert push.live_boards(state) == {"a": 1, "b": 3}


def test_state_pushed_once_until_changed_and_rate_limited(tmp_path):
    p, http, mono, _ = make(tmp_path)
    p.cycle()
    assert http.paths("/ingest/state") == ["/ingest/state"]
    path, body, headers = http.posts[0]
    assert headers["Content-Encoding"] == "gzip" and headers["X-Ingest-Token"] == "secret-token-value-123"
    assert json.loads(gzip.decompress(body))["id"] == "t-1"
    mono.now += 2
    http.state["state_age_s"] = 7.0                  # volatile only: no push
    http.state["server_now_ms"] = 99
    p.cycle()
    assert len(http.paths("/ingest/state")) == 1
    http.state["updated_epoch_ms"] = 2               # real change, but within 1.5 s of the last push
    mono.now += 0.5
    p.last_state_push_mono = mono.now - 0.5
    p.cycle()
    assert len(http.paths("/ingest/state")) == 1
    mono.now += 1.5
    p.cycle()
    assert len(http.paths("/ingest/state")) == 2


def test_first_start_pushes_only_newest_20_clips_audio_first(tmp_path):
    p, http, mono, _ = make(tmp_path)
    for _ in range(10):
        p.cycle()
        mono.now += 1.1
    clip_posts = [x for x in http.paths() if "clip" in x]
    metas = [json.loads(b)[0]["seq"] for path, b, _ in http.posts if path == "/ingest/clips"]
    assert metas == list(range(31, 51))
    assert clip_posts[0] == "/ingest/clip-audio/clip-31.wav" and clip_posts[1] == "/ingest/clips"
    assert p.last_seq == 50 and p.clips_pushed == 20
    status = json.loads((tmp_path / "t-1-publish-status.json").read_text())
    assert status["last_clip_seq"] == 50 and status["relay_up"] is True and status["clips_pushed"] == 20
    assert "secret-token" not in (tmp_path / "t-1-publish-status.json").read_text()


def test_restart_uses_relay_last_seq(tmp_path):
    http = FakeHttp()
    http.relay_health = {"ok": True, "last_clip_seq": 45, "tournament_id": "t-1"}
    p, http, mono, _ = make(tmp_path, http)
    for _ in range(4):
        p.cycle()
        mono.now += 1.1
    metas = [json.loads(b)[0]["seq"] for path, b, _ in http.posts if path == "/ingest/clips"]
    assert metas == [46, 47, 48, 49, 50]


def clip_metas(http) -> list[dict]:
    return [json.loads(b)[0] for path, b, _ in http.posts if path == "/ingest/clips"]


def run_cycles(p, mono, n, step=1.1):
    for _ in range(n):
        p.cycle()
        mono.now += step


def test_archived_relay_beats_a_stale_status_file(tmp_path):
    # 2026-10-08 on the VPS: relay data dir archived (healthz 0), status file still said 509.
    (tmp_path / "t-1-publish-status.json").write_text(json.dumps({"last_clip_seq": 509}))
    http = FakeHttp()
    http.clips = [{"seq": s, "game": "r1b1", "audio": f"clip-{s}.wav", "text": f"c{s}"} for s in range(1, 521)]
    http.relay_health = {"ok": True, "last_clip_seq": 0, "tournament_id": "t-1"}
    p, http, mono, logs = make(tmp_path, http)
    run_cycles(p, mono, 6)
    assert [c["seq"] for c in clip_metas(http)] == list(range(501, 521))
    assert p.clips_pushed == 20 and p.last_seq == 520
    assert any("cursor 509 -> None" in line for line in logs)


def test_relay_reset_while_running_resends_newest(tmp_path):
    p, http, mono, _ = make(tmp_path)
    run_cycles(p, mono, 6)
    assert len(clip_metas(http)) == 20 and p.last_seq == 50
    http.relay_health = {"ok": True, "last_clip_seq": 0, "tournament_id": "t-1"}   # relay archived
    run_cycles(p, mono, 5)                    # before the next health read: nothing new to send
    assert len(clip_metas(http)) == 20
    mono.now += push.HEALTH_EVERY
    run_cycles(p, mono, 6)
    assert [c["seq"] for c in clip_metas(http)][20:] == list(range(31, 51))


def test_relay_ahead_of_status_file_still_wins(tmp_path):
    (tmp_path / "t-1-publish-status.json").write_text(json.dumps({"last_clip_seq": 10}))
    http = FakeHttp()
    http.relay_health = {"ok": True, "last_clip_seq": 48, "tournament_id": "t-1"}
    p, http, mono, _ = make(tmp_path, http)
    run_cycles(p, mono, 3)
    assert [c["seq"] for c in clip_metas(http)] == [49, 50]


def test_fresh_viewer_renumbered_from_1_is_not_skipped(tmp_path):
    # The relay persisted clips up to 509; the new viewer process counts from 1 again.
    http = FakeHttp()
    http.clips = [{"seq": s, "game": "r1b1", "audio": f"clip-{s}.wav", "text": f"new{s}"} for s in range(1, 6)]
    http.relay_health = {"ok": True, "last_clip_seq": 509, "tournament_id": "t-1"}
    p, http, mono, logs = make(tmp_path, http)
    run_cycles(p, mono, 4)
    metas = clip_metas(http)
    assert [c["seq"] for c in metas] == [1, 2, 3, 4, 5]
    assert all(c["tournament_id"] == "t-1" for c in metas)
    assert any("local newest seq 5 is below cursor 509" in line for line in logs)
    # An old relay still says 509 on the next health read: the cursor must not jump back up.
    mono.now += push.HEALTH_EVERY
    http.clips.append({"seq": 6, "game": "r1b1", "audio": "clip-6.wav", "text": "new6"})
    run_cycles(p, mono, 2)
    assert [c["seq"] for c in clip_metas(http)] == [1, 2, 3, 4, 5, 6] and p.last_seq == 6


def test_empty_fresh_viewer_pushes_its_first_clip(tmp_path):
    http = FakeHttp()
    http.clips = []
    http.relay_health = {"ok": True, "last_clip_seq": 509, "tournament_id": "t-1"}
    p, http, mono, _ = make(tmp_path, http)
    run_cycles(p, mono, 2)
    assert p.last_seq is None
    mono.now += push.HEALTH_EVERY                # health read again (still 509): stays None
    http.clips = [{"seq": 1, "game": "r1b1", "audio": "clip-1.wav", "text": "first"}]
    run_cycles(p, mono, 2)
    assert [c["seq"] for c in clip_metas(http)] == [1]


def test_relay_clips_of_another_tournament_mean_newest_20(tmp_path):
    http = FakeHttp()
    http.relay_health = {"ok": True, "last_clip_seq": 45, "tournament_id": "t-1", "clips_tournament_id": "t-0"}
    p, http, mono, _ = make(tmp_path, http)
    run_cycles(p, mono, 6)
    assert [c["seq"] for c in clip_metas(http)] == list(range(31, 51))


def test_idle_local_probe_is_rate_limited(tmp_path):
    http = FakeHttp()
    http.relay_health = {"ok": True, "last_clip_seq": 50, "tournament_id": "t-1"}
    p, http, mono, _ = make(tmp_path, http)
    run_cycles(p, mono, 10)                   # 11 s idle at the cursor: one probe only
    probes = [g for g in http.gets if g == LOCAL + "/api/commentary?after=0"]
    assert len(probes) == 1 and p.last_seq == 50 and clip_metas(http) == []


def test_thinking_pushed_when_size_changes_and_rate_limited(tmp_path):
    p, http, mono, _ = make(tmp_path)
    http.clips = []
    p.cycle()
    think = [json.loads(b) for path, b, _ in http.posts if path == "/ingest/thinking"]
    assert think == [{"game": "r1b1", "ply": 5, "size": 10, "text": "0123456789", "from": 0}]
    mono.now += 2.5
    p.cycle()                                         # same size: nothing new
    assert len(http.paths("/ingest/thinking")) == 1
    http.thinking[("r1b1", 5)] = {"exists": True, "size": 14, "text": "0123456789abcd", "from": 0}
    mono.now += 0.5
    p.cycle()                                         # within 2 s of the last read of this board
    assert len(http.paths("/ingest/thinking")) == 1
    mono.now += 2.0
    p.cycle()
    assert len(http.paths("/ingest/thinking")) == 2


def test_thinking_final_text_of_previous_ply_is_sent(tmp_path):
    p, http, mono, _ = make(tmp_path)
    http.clips = []
    p.cycle()
    http.thinking[("r1b1", 5)] = {"exists": True, "size": 20, "text": "x" * 20, "from": 0}
    http.state["games"]["r1b1"]["plies"] = 5         # the move was played: now thinking about ply 6
    http.state["updated_epoch_ms"] = 2
    mono.now += 2.5
    p.cycle()
    sent = [(json.loads(b)["ply"], json.loads(b)["size"]) for path, b, _ in http.posts if path == "/ingest/thinking"]
    assert sent == [(5, 10), (5, 20)]


def test_survives_relay_down_with_backoff(tmp_path):
    p, http, mono, logs = make(tmp_path)
    http.relay_down = True
    p.cycle()
    assert p.relay_up is False and p.relay_backoff.failures == 1
    status = json.loads((tmp_path / "t-1-publish-status.json").read_text())
    assert status["ok"] is False and status["relay_up"] is False and "relay" in status["error"]
    gets_before = len([g for g in http.gets if g.startswith(LOCAL + "/api/commentary")])
    mono.now += 0.5                                   # still backing off: relay work skipped
    p.cycle()
    assert p.relay_backoff.failures == 1
    for _ in range(4):
        mono.now += 40
        p.cycle()
    assert p.relay_backoff.failures == 5 and p.relay_backoff.delay == 16.0
    http.relay_down = False
    mono.now += 40
    p.cycle()
    assert p.relay_up is True and p.relay_backoff.failures == 0
    assert http.paths("/ingest/state") == ["/ingest/state"]
    assert gets_before == 0


def test_survives_local_viewer_down(tmp_path):
    p, http, mono, _ = make(tmp_path)
    http.local_down = True
    p.cycle()                                         # nothing known yet: no crash, no posts
    assert p.local_up is False and http.posts == []
    http.local_down = False
    mono.now += 2
    p.cycle()
    assert p.local_up is True and http.paths("/ingest/state")
    http.local_down = True
    mono.now += 2
    p.cycle()
    assert p.local_up is False
    status = json.loads((tmp_path / "t-1-publish-status.json").read_text())
    assert status["ok"] is False and "local" in status["error"]


def test_backoff_doubles_and_resets():
    clock = Clock(0)
    b = push.Backoff(clock, first=1, max_delay=8)
    delays = []
    for _ in range(5):
        b.fail()
        delays.append(b.delay)
    assert delays == [1, 2, 4, 8, 8]
    assert not b.ready()
    clock.now = 8
    assert b.ready()
    b.ok()
    assert b.delay == 0 and b.ready()


def test_run_never_crashes_on_unexpected_error(tmp_path, monkeypatch):
    p, http, mono, logs = make(tmp_path)
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        if calls["n"] >= 2:
            raise KeyboardInterrupt
        raise RuntimeError("weird")

    monkeypatch.setattr(p, "cycle", boom)
    monkeypatch.setattr(push.time, "sleep", lambda s: None)
    try:
        p.run()
    except KeyboardInterrupt:
        pass
    assert any("cycle error: RuntimeError: weird" in line for line in logs)


def last_state(http):
    return json.loads(gzip.decompress([b for path, b, _ in http.posts if path == "/ingest/state"][-1]))


def test_eval_track_comes_from_the_sidecar_from_whites_side(tmp_path):
    import chess

    board = chess.Board()
    after1 = board.copy(); after1.push_san("e4")
    after2 = after1.copy(); after2.push_san("e5")
    sidecar = {"depth": 16, "positions": {
        board.fen(): {"cp": 20, "best": "e2e4"},        # White to move: +20 for White
        after1.fen(): {"cp": -35, "best": "e7e5"},      # Black to move: -35 for Black is +35 for White
        after2.fen(): {"cp": 10000, "best": None},       # mate for the side to move (White)
    }}
    (tmp_path / "t-1-annotations.json").write_text(json.dumps(sidecar), encoding="utf-8")
    p, http, mono, _ = make(tmp_path)
    http.state["games"]["r1b2"]["moves"] = [{"uci": "e2e4"}, {"uci": "e7e5"}]
    p.cycle()
    sent = json.loads(gzip.decompress(http.posts[0][1]))
    track = sent["eval_track"]["r1b2"]
    assert track["depth"] == 16
    assert track["cp"] == [20, 35, 10000]
    assert track["best"] == ["e4", "e5", ""]
    assert sent["eval_track"]["r1b1"]["cp"] == [20]           # a game with no moves still has its start position


def test_eval_track_fills_in_later_and_never_blocks_the_state(tmp_path):
    import chess

    p, http, mono, _ = make(tmp_path)
    http.state["games"]["r1b2"]["moves"] = [{"uci": "e2e4"}]
    p.cycle()                                                  # no sidecar yet: state goes out untouched
    assert "eval_track" not in json.loads(gzip.decompress(http.posts[0][1]))
    board = chess.Board(); board.push_san("e4")
    side = tmp_path / "t-1-annotations.json"
    side.write_text(json.dumps({"depth": 16, "positions": {board.fen(): {"cp": -20, "best": "e7e5"}}}), encoding="utf-8")
    mono.now += 2
    p.cycle()
    track = last_state(http)["eval_track"]["r1b2"]
    assert track["cp"] == [None, 20] and track["best"] == ["", "e5"]
    side.write_text("{not json", encoding="utf-8")             # half-written sidecar: keep the last good scores
    import os; os.utime(side, (1, 2))
    mono.now += 2
    http.state["updated_epoch_ms"] = 3
    p.cycle()
    assert last_state(http)["eval_track"]["r1b2"]["cp"] == [None, 20]
