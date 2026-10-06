"""Relay tests: real HTTP on ephemeral ports in a tmp dir, no network beyond 127.0.0.1."""

from __future__ import annotations

import gzip
import http.client
import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import aichess_relay as relay  # noqa: E402

TOKEN = "t" * 64


class Clock:
    def __init__(self, now: float = 1_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def wav(n: int = 1000, fill: bytes = b"\x01") -> bytes:
    return b"RIFF" + (n + 36).to_bytes(4, "little") + b"WAVEfmt " + fill * n


def state_payload(updated: int = 111, age: float = 2.0, **extra) -> dict:
    payload = {"id": "llm-swiss-test", "updated_epoch_ms": updated, "state_age_s": age, "server_now_ms": 5,
               "games": {"r1b1": {"status": "live", "plies": 3, "pgn_path": "C:/secret/path.pgn",
                                  "moves": [{"uci": "e2e4", "pgn_path": "x"}]}},
               "standings": [{"name": "A", "score": 1.0}]}
    payload.update(extra)
    return payload


class Relay:
    def __init__(self, data_dir: Path, clock: Clock | None = None, **store_kw):
        self.clock = clock or Clock()
        self.logs: list[str] = []
        self.store = relay.Store(data_dir, clock=self.clock, log=self.logs.append, **store_kw)
        self.pub, self.ing = relay.make_servers(self.store, TOKEN.encode(), "127.0.0.1", 0, 0, self.logs.append)
        for server in (self.pub, self.ing):
            threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.pub_port = self.pub.server_address[1]
        self.ing_port = self.ing.server_address[1]

    def close(self) -> None:
        for server in (self.pub, self.ing):
            server.shutdown()
            server.server_close()

    def request(self, port: int, method: str, path: str, body: bytes | None = None, headers: dict | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            return resp.status, dict(resp.getheaders()), resp.read()
        finally:
            conn.close()

    def get(self, path: str, headers: dict | None = None):
        return self.request(self.pub_port, "GET", path, headers=headers)

    def get_json(self, path: str):
        status, _, body = self.get(path)
        return status, json.loads(body)

    def ingest(self, path: str, body: bytes, token: str = TOKEN, headers: dict | None = None):
        hdrs = {"X-Ingest-Token": token} if token is not None else {}
        hdrs.update(headers or {})
        return self.request(self.ing_port, "POST", path, body, hdrs)


@pytest.fixture()
def rl(tmp_path):
    r = Relay(tmp_path / "data")
    yield r
    r.close()


def test_state_roundtrip_gzip_and_patched_fields(rl):
    status, _, body = rl.ingest("/ingest/state", gzip.compress(json.dumps(state_payload()).encode()),
                                headers={"Content-Encoding": "gzip"})
    assert status == 200, body
    rl.clock.now += 10
    status, headers, body = rl.get("/api/tournament", {"Accept-Encoding": "gzip, deflate"})
    assert status == 200
    assert headers["Content-Encoding"] == "gzip"
    assert headers["Cache-Control"] == "no-store"
    assert int(headers["Content-Length"]) == len(body)
    data = json.loads(gzip.decompress(body))
    assert data["id"] == "llm-swiss-test"
    assert data["hosted"] is True
    assert data["state_age_s"] == 12.0                        # 2.0 at push + 10 s on the relay clock
    assert data["server_now_ms"] == int(rl.clock.now * 1000)
    assert data["relay_received_epoch_ms"] == int((rl.clock.now - 10) * 1000)
    assert "pgn_path" not in json.dumps(data)                  # stripped everywhere
    assert data["games"]["r1b1"]["moves"][0]["uci"] == "e2e4"
    plain_status, plain_headers, plain = rl.get("/api/tournament")
    assert "Content-Encoding" not in plain_headers
    assert json.loads(plain) == data


def test_gzip_splice_handles_unicode_and_repeat_reads(rl):
    rl.ingest("/ingest/state", json.dumps(state_payload(title="Kwen \u2654 vs GLM \u00e9")).encode())
    for step in range(3):
        rl.clock.now += 1.25
        _, _, body = rl.get("/api/tournament", {"Accept-Encoding": "gzip"})
        data = json.loads(gzip.decompress(body).decode("utf-8"))
        assert data["title"] == "Kwen \u2654 vs GLM \u00e9"
        assert data["state_age_s"] == round(2.0 + 1.25 * (step + 1), 1)


def test_since_unchanged(rl):
    rl.ingest("/ingest/state", json.dumps(state_payload(updated=777)).encode())
    rl.clock.now += 3
    status, data = rl.get_json("/api/tournament?since=777")
    assert status == 200
    assert data["unchanged"] is True and data["state_age_s"] == 5.0
    assert data["server_now_ms"] == int(rl.clock.now * 1000)
    status, data = rl.get_json("/api/tournament?since=776")
    assert "games" in data


def test_no_state_yet_and_unknown_paths(rl):
    assert rl.get("/api/tournament")[0] == 404
    assert rl.get("/nope")[0] == 404
    assert rl.get("/state.json.gz")[0] == 404
    assert rl.get_json("/api/analyze") == (200, {"enabled": False})
    assert rl.get_json("/api/viewer-version") == (200, {"version": "relay"})


def test_head_has_headers_without_body(rl):
    rl.ingest("/ingest/state", json.dumps(state_payload()).encode())
    status, headers, body = rl.request(rl.pub_port, "HEAD", "/api/tournament")
    assert status == 200 and body == b"" and int(headers["Content-Length"]) > 100


def test_token_required(rl):
    payload = json.dumps(state_payload()).encode()
    assert rl.ingest("/ingest/state", payload, token="wrong")[0] == 403
    assert rl.ingest("/ingest/state", payload, token=None)[0] == 403
    assert rl.get("/api/tournament")[0] == 404
    assert not any(TOKEN in line for line in rl.logs)


def test_ingest_not_reachable_on_public_port(rl):
    status, _, _ = rl.request(rl.pub_port, "POST", "/ingest/state", json.dumps(state_payload()).encode(),
                              {"X-Ingest-Token": TOKEN})
    assert status == 404
    assert rl.get("/api/tournament")[0] == 404
    assert rl.ing.server_address[0] == "127.0.0.1"


def test_state_without_id_refused(rl):
    payload = state_payload()
    del payload["id"]
    assert rl.ingest("/ingest/state", json.dumps(payload).encode())[0] == 400
    assert rl.ingest("/ingest/state", b"not json")[0] == 400


def test_body_size_limits(rl, monkeypatch):
    monkeypatch.setattr(relay, "MAX_BODY", 1000)
    status, _, _ = rl.ingest("/ingest/state", b"x" * 2000)
    assert status == 413
    assert rl.ingest("/ingest/clip-audio/clip-1.wav", wav(10))[0] == 200


def test_wav_size_and_type_limits(rl, monkeypatch):
    monkeypatch.setattr(relay, "MAX_WAV", 2000)
    assert rl.ingest("/ingest/clip-audio/clip-1.wav", wav(3000))[0] == 400
    assert rl.ingest("/ingest/clip-audio/clip-2.wav", b"ID3 mp3 bytes here")[0] == 400
    assert rl.ingest("/ingest/clip-audio/clip-3.wav", wav(100))[0] == 200


def test_clips_audio_and_age(rl):
    audio = wav(2000, b"\x07")
    assert rl.ingest("/ingest/clip-audio/clip-5.wav", audio)[0] == 200
    clips = [{"seq": 5, "game": "r1b1", "ply": 9, "text": "hello", "audio": "clip-5.wav", "seconds": 3.1,
              "final": False, "age_s": 4.0},
             {"seq": 6, "game": "r1b2", "ply": 2, "text": "two", "audio": "clip-6.wav", "seconds": 2.0,
              "final": False, "age_s": 1.0}]
    status, _, body = rl.ingest("/ingest/clips", json.dumps(clips).encode())
    assert status == 200 and json.loads(body)["added"] == 2
    assert json.loads(rl.ingest("/ingest/clips", json.dumps(clips).encode())[2])["added"] == 0   # idempotent
    rl.clock.now += 6
    status, data = rl.get_json("/api/commentary?after=0")
    assert data["enabled"] is True and data["quiet_s"] == 0 and data["held"] is False
    assert [c["seq"] for c in data["clips"]] == [5, 6]
    assert data["clips"][0]["age_s"] == 10.0 and data["clips"][1]["age_s"] == 7.0
    assert not any(k.startswith("_") for c in data["clips"] for k in c)
    assert [c["seq"] for c in rl.get_json("/api/commentary?after=5")[1]["clips"]] == [6]
    assert [c["seq"] for c in rl.get_json("/api/commentary?after=0&game=r1b1")[1]["clips"]] == [5]
    status, headers, body = rl.get("/api/commentary/audio/clip-5.wav")
    assert status == 200 and body == audio
    assert headers["Content-Type"] == "audio/wav"
    assert headers["Cache-Control"] == "public, max-age=86400, immutable"
    assert rl.get("/api/commentary/audio/clip-6.wav")[0] == 404          # metadata only, no audio


@pytest.mark.parametrize("name", ["..%2Fstate.json.gz", "..%2F..%2Fetc%2Fpasswd", "clip-1.wav%00", "clip-a.wav",
                                  "clip-1.mp3", "clip-1.wav%0A", "%2E%2E%2Fclips.json", "clip-1.wav%2F..%2F..%2Fstate.meta.json",
                                  "..\\clips.json"])
def test_clip_name_safety(rl, name):
    rl.ingest("/ingest/clip-audio/clip-1.wav", wav(10))
    assert rl.get(f"/api/commentary/audio/{name}")[0] == 404
    assert rl.ingest(f"/ingest/clip-audio/{name}", wav(10))[0] in (400, 404)


def test_bad_clip_metadata_refused(rl):
    assert rl.ingest("/ingest/clips", json.dumps([{"seq": "1"}]).encode())[0] == 400
    assert rl.ingest("/ingest/clips", json.dumps([{"seq": 1, "audio": "../x.wav"}]).encode())[0] == 400
    assert rl.ingest("/ingest/clips", json.dumps({"seq": 1}).encode())[0] == 400


def test_thinking_offsets(rl):
    text = "abc " * 100                     # 400 bytes, file offset 1000..1400
    rec = {"game": "r1b1", "ply": 7, "size": 1400, "text": text, "from": 1000}
    assert rl.ingest("/ingest/thinking", json.dumps(rec).encode())[0] == 200
    status, data = rl.get_json("/api/thinking?game=r1b1&ply=7&since=0")
    assert data == {"exists": True, "size": 1400, "text": text, "from": 1000, "truncated": True}
    _, data = rl.get_json("/api/thinking?game=r1b1&ply=7&since=1200")
    assert data["from"] == 1200 and data["text"] == text[200:] and data["truncated"] is False
    _, data = rl.get_json("/api/thinking?game=r1b1&ply=7&since=1400")
    assert data["text"] == "" and data["size"] == 1400
    _, data = rl.get_json("/api/thinking?game=r1b1&ply=7&since=99999")    # replaced file: start over
    assert data["from"] == 1000 and data["truncated"] is True
    _, data = rl.get_json("/api/thinking?game=r1b1&ply=8&since=0")
    assert data == {"exists": False, "size": 0, "text": "", "from": 0, "truncated": False}
    assert rl.get("/api/thinking?game=../x&ply=1&since=0")[0] == 400
    assert rl.get("/api/thinking?game=r1b1&ply=0&since=0")[0] == 400
    assert rl.get("/api/thinking?game=r1b1&ply=x")[0] == 400


def test_thinking_tail_limit_and_ply_window(rl):
    big = "\u00e9" * 40_000                 # 80 000 bytes of 2-byte characters
    rl.ingest("/ingest/thinking", json.dumps({"game": "g1", "ply": 1, "text": big, "from": 0}).encode())
    _, data = rl.get_json("/api/thinking?game=g1&ply=1&since=0")
    assert data["size"] == 80_000
    assert data["truncated"] is True and len(data["text"].encode()) <= 60_000
    assert set(data["text"]) == {"\u00e9"}
    rec = rl.store.thinking["g1"][1]
    assert len(rec["data"]) <= relay.THINK_TAIL_MAX and rec["base"] + len(rec["data"]) == 80_000
    for ply in range(2, 50):
        rl.ingest("/ingest/thinking", json.dumps({"game": "g1", "ply": ply, "text": "x", "from": 0}).encode())
    assert sorted(rl.store.thinking["g1"]) == list(range(10, 50))
    assert not (rl.store.dir / "thinking" / "g1" / "1.json").exists()


def test_restart_persistence(tmp_path):
    data_dir = tmp_path / "data"
    first = Relay(data_dir)
    first.ingest("/ingest/state", json.dumps(state_payload(updated=42, age=1.0)).encode())
    first.ingest("/ingest/clip-audio/clip-9.wav", wav(50))
    first.ingest("/ingest/clips", json.dumps([{"seq": 9, "audio": "clip-9.wav", "text": "hi", "age_s": 2.0}]).encode())
    first.ingest("/ingest/thinking", json.dumps({"game": "r2b1", "ply": 4, "text": "deep", "from": 10}).encode())
    first.ingest("/ingest/version", json.dumps({"version": "v123"}).encode())
    first.close()
    clock = Clock(first.clock.now + 20)
    second = Relay(data_dir, clock)
    try:
        _, data = second.get_json("/api/tournament")
        assert data["updated_epoch_ms"] == 42 and data["state_age_s"] == 21.0
        _, comm = second.get_json("/api/commentary?after=0")
        assert comm["clips"][0]["seq"] == 9 and comm["clips"][0]["age_s"] == 22.0
        assert second.get("/api/commentary/audio/clip-9.wav")[2] == wav(50)
        assert second.get_json("/api/thinking?game=r2b1&ply=4&since=0")[1]["text"] == "deep"
        assert second.get_json("/api/viewer-version")[1] == {"version": "v123"}
        _, health = second.get_json("/healthz")
        assert health["last_clip_seq"] == 9 and health["clips"] == 1 and health["state_updated_epoch_ms"] == 42
    finally:
        second.close()


def test_prune_oldest_wavs(tmp_path):
    r = Relay(tmp_path / "data", clips_cap=2500)
    try:
        for seq in range(1, 6):
            assert r.ingest(f"/ingest/clip-audio/clip-{seq}.wav", wav(1000))[0] == 200   # 1048 bytes each
        assert sorted(r.store.wavs, key=relay.clip_seq) == ["clip-4.wav", "clip-5.wav"]
        assert not (r.store.dir / "clips" / "clip-1.wav").exists()
        assert r.get("/api/commentary/audio/clip-1.wav")[0] == 404
        assert r.get("/api/commentary/audio/clip-5.wav")[0] == 200
    finally:
        r.close()


def test_healthz_moves(rl):
    _, before = rl.get_json("/healthz")
    assert before["ok"] is True and before["last_clip_seq"] == 0 and before["state_age_s"] is None
    rl.ingest("/ingest/state", json.dumps(state_payload(updated=5)).encode())
    rl.ingest("/ingest/clips", json.dumps([{"seq": 3, "text": "a", "age_s": 0}]).encode())
    rl.clock.now += 4
    _, after = rl.get_json("/healthz")
    assert after["last_clip_seq"] == 3 and after["clips"] == 1 and after["state_age_s"] == 6.0
    assert after["state_updated_epoch_ms"] == 5
    status, _, body = rl.request(rl.ing_port, "GET", "/healthz")
    assert status == 200 and json.loads(body)["last_clip_seq"] == 3


def test_refuses_to_start_without_token(tmp_path):
    with pytest.raises(SystemExit):
        relay.main(["--token-file", str(tmp_path / "missing.token"), "--data-dir", str(tmp_path / "d"),
                    "--public-port", "0", "--ingest-port", "0"])
    (tmp_path / "empty.token").write_text("  \n")
    with pytest.raises(SystemExit):
        relay.main(["--token-file", str(tmp_path / "empty.token"), "--data-dir", str(tmp_path / "d"),
                    "--public-port", "0", "--ingest-port", "0"])


def test_disk_write_failure_still_serves_from_memory(rl, monkeypatch):
    def full(path, data):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(relay, "atomic_write", full)
    assert rl.ingest("/ingest/state", json.dumps(state_payload(updated=9)).encode())[0] == 200
    assert rl.get_json("/api/tournament")[1]["updated_epoch_ms"] == 9
    assert rl.ingest("/ingest/thinking", json.dumps({"game": "g", "ply": 1, "text": "t", "from": 0}).encode())[0] == 200
    assert rl.ingest("/ingest/clips", json.dumps([{"seq": 1, "text": "x"}]).encode())[0] == 200
    assert rl.ingest("/ingest/clip-audio/clip-1.wav", wav(10))[0] == 500          # audio needs the disk
    assert any("persist failed" in line for line in rl.logs)