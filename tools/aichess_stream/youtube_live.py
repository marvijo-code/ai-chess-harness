#!/usr/bin/env python3
"""Create and inspect the YouTube live broadcast for the 24/7 AI-chess stream (runs on the laptop).

The OAuth refresh token stays on the laptop (YOUTUBE_TOKEN_DIR, default
C:\\dev\\ai-tools\\secrets\\youtube-oauth\\<slug>.json). The stream key goes ONLY to the VPS:
`install-key` pipes it over ssh stdin into ~/.config/ai-chess/youtube-stream.env (mode 600).
Nothing here prints a secret: the key is reported by length and a SHA-256 prefix.

  python tools/aichess_stream/youtube_live.py check                 # can the channel live stream? what exists?
  python tools/aichess_stream/youtube_live.py setup                 # reusable stream + PUBLIC broadcast, bound
  python tools/aichess_stream/youtube_live.py setup --test          # separate PRIVATE test stream + broadcast
  python tools/aichess_stream/youtube_live.py install-key [--test]  # key -> VPS env file (stdin, mode 600)
  python tools/aichess_stream/youtube_live.py status [--test]       # broadcast lifecycle + stream health
  python tools/aichess_stream/youtube_live.py update-meta           # re-apply title, description, tags
  python tools/aichess_stream/youtube_live.py end-test              # complete the private test broadcast

Ids are kept in out/aichess-stream/youtube-live-state.json (gitignored; ids are not secret).
Metadata (title, description, tags) is in tools/aichess_stream/youtube_live.json.
Quota: insert/bind/update 50 units each, list 1. One `setup` costs about 200 units.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
META = HERE / "youtube_live.json"
STATE_DIR = ROOT / "out" / "aichess-stream"
STATE = STATE_DIR / "youtube-live-state.json"
TOKEN_DIR = Path(os.environ.get("YOUTUBE_TOKEN_DIR", r"C:\dev\ai-tools\secrets\youtube-oauth"))
REMOTE_ENV = "~/.config/ai-chess/youtube-stream.env"
REMOTE_TEST_ENV = "~/.config/ai-chess/youtube-stream-test.env"
TAG_LIMIT = 500


def die(msg: str, code: int = 1) -> None:
    print(msg, file=sys.stderr)
    sys.exit(code)


def load_meta() -> dict:
    return json.loads(META.read_text(encoding="utf-8"))


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def studio_tag_chars(tags: list[str]) -> int:
    """Studio's counter: quotes around a tag with a space, plus one comma between tags."""
    return sum(len(t) + (2 if " " in t else 0) for t in tags) + max(0, len(tags) - 1)


def fill_tags(tags: list[str], limit: int = TAG_LIMIT) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for t in tags:
        t = t.strip()
        if not t or t.lower() in seen:
            continue
        if studio_tag_chars(out + [t]) > limit:
            continue
        out.append(t)
        seen.add(t.lower())
    return out


def check_text(meta: dict) -> None:
    text = meta["title"] + "\n".join(meta["description_lines"]) + "".join(meta["tags"])
    if "\u2013" in text or "\u2014" in text:
        die("metadata contains an en or em dash; use a plain hyphen")
    if len(meta["title"]) > 100:
        die(f"title is {len(meta['title'])} characters (limit 100)")


def service(slug: str):
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
    except ImportError:
        die("pip install google-auth-oauthlib google-api-python-client")
    p = TOKEN_DIR / f"{slug}.json"
    if not p.exists():
        die(f"no credential at {p}")
    d = json.loads(p.read_text(encoding="utf-8"))
    creds = Credentials(None, refresh_token=d["refresh_token"], client_id=d["client_id"],
                        client_secret=d["client_secret"], token_uri=d["token_uri"], scopes=d["scopes"])
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def assert_channel(yt, meta: dict) -> str:
    items = yt.channels().list(part="snippet", mine=True).execute().get("items", [])
    if not items or items[0]["id"] != meta["channel_id"]:
        die(f"credential is not channel {meta['channel_id']}: {[i['id'] for i in items]}")
    title = items[0]["snippet"]["title"]
    print(f"channel: {title} ({items[0]['id']})")
    return title


def vps_target(arg: str | None) -> str:
    if arg:
        return arg
    env = os.environ.get("ROUND_RECORDER_VPS")
    if env:
        return env
    for p in (ROOT / "out" / "aichess-push" / "vps.txt", Path(r"C:\dev\chess-harness-codex\out\aichess-push\vps.txt")):
        if p.exists():
            return p.read_text(encoding="utf-8").strip()
    die("pass --vps user@host or set ROUND_RECORDER_VPS (the VPS is not stored in the repo)")
    return ""


def description(meta: dict) -> str:
    return "\n".join(meta["description_lines"])


# ---------------------------------------------------------------- commands

def cmd_check(yt, meta, args) -> None:
    assert_channel(yt, meta)
    try:
        b = yt.liveBroadcasts().list(part="id,snippet,status,contentDetails", mine=True, maxResults=50).execute()
    except Exception as exc:  # googleapiclient.errors.HttpError
        die(f"liveBroadcasts.list failed: {exc}")
    print(f"broadcasts: {len(b.get('items', []))}")
    for it in b.get("items", []):
        st = it["status"]
        print(f"  {it['id']}  {st.get('lifeCycleStatus')}  {st.get('privacyStatus')}  "
              f"bound={it['contentDetails'].get('boundStreamId', '-')}  {it['snippet']['title'][:70]}")
    s = yt.liveStreams().list(part="id,snippet,cdn,status,contentDetails", mine=True, maxResults=50).execute()
    print(f"streams: {len(s.get('items', []))}")
    for it in s.get("items", []):
        print(f"  {it['id']}  {it['status'].get('streamStatus')}  {it['status'].get('healthStatus', {}).get('status')}  "
              f"{it['cdn'].get('resolution')}/{it['cdn'].get('frameRate')}  reusable={it['contentDetails'].get('isReusable')}  "
              f"{it['snippet']['title'][:60]}")


def create_stream(yt, title: str) -> str:
    body = {
        "snippet": {"title": title},
        "cdn": {"ingestionType": "rtmp", "resolution": "variable", "frameRate": "variable"},
        "contentDetails": {"isReusable": True},
    }
    r = yt.liveStreams().insert(part="snippet,cdn,contentDetails,status", body=body).execute()
    print(f"created liveStream {r['id']}")
    return r["id"]


def create_broadcast(yt, title: str, desc: str, privacy: str, auto_stop: bool) -> str:
    start = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    body = {
        "snippet": {"title": title, "description": desc, "scheduledStartTime": start},
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
        "contentDetails": {
            "enableAutoStart": True,
            "enableAutoStop": auto_stop,
            "enableDvr": True,
            "enableEmbed": True,
            "recordFromStart": True,
            "latencyPreference": "normal",
            "monitorStream": {"enableMonitorStream": False},
        },
    }
    r = yt.liveBroadcasts().insert(part="id,snippet,status,contentDetails", body=body).execute()
    print(f"created liveBroadcast {r['id']} ({privacy})")
    return r["id"]


def apply_video_meta(yt, video_id: str, meta: dict, title: str, desc: str, tags: list[str]) -> None:
    """Category and tags live on the video resource (the broadcast id is the video id)."""
    v = yt.videos().list(part="snippet", id=video_id).execute().get("items", [])
    if not v:
        die(f"video {video_id} not found")
    snip = v[0]["snippet"]
    snip.update({"title": title, "description": desc, "categoryId": meta["category_id"], "tags": tags})
    snip.pop("thumbnails", None)
    snip.pop("localized", None)
    yt.videos().update(part="snippet", body={"id": video_id, "snippet": snip}).execute()
    print(f"video {video_id}: category {meta['category_id']}, {len(tags)} tags, {studio_tag_chars(tags)}/500 tag characters")


def cmd_setup(yt, meta, args) -> None:
    check_text(meta)
    assert_channel(yt, meta)
    state = load_state()
    key = "test" if args.test else "main"
    slot = state.get(key, {})
    if slot.get("broadcast_id"):
        print(f"{key} broadcast already exists: {slot['broadcast_id']} (nothing created)")
        return
    tags = fill_tags(meta["tags"])
    if args.test:
        stream_id = slot.get("stream_id") or create_stream(yt, meta["stream_title"] + " TEST")
        title, desc, privacy = meta["test_broadcast_title"], "Private pipeline test. Not a real broadcast.", "private"
        bid = create_broadcast(yt, title, desc, privacy, auto_stop=True)
    else:
        stream_id = slot.get("stream_id") or create_stream(yt, meta["stream_title"])
        title, desc, privacy = meta["title"], description(meta), "public"
        bid = create_broadcast(yt, title, desc, privacy, auto_stop=False)
    slot.update({"stream_id": stream_id, "broadcast_id": bid, "privacy": privacy,
                 "watch_url": f"https://www.youtube.com/watch?v={bid}"})
    state[key] = slot
    save_state(state)
    yt.liveBroadcasts().bind(part="id,contentDetails", id=bid, streamId=stream_id).execute()
    print(f"bound broadcast {bid} -> stream {stream_id}")
    if not args.test:
        apply_video_meta(yt, bid, meta, title, desc, tags)
    print(f"watch URL: {slot['watch_url']}")


def cmd_update_meta(yt, meta, args) -> None:
    check_text(meta)
    assert_channel(yt, meta)
    bid = load_state().get("main", {}).get("broadcast_id")
    if not bid:
        die("no main broadcast in the state file; run setup first")
    apply_video_meta(yt, bid, meta, meta["title"], description(meta), fill_tags(meta["tags"]))


def cmd_install_key(yt, meta, args) -> None:
    slot = load_state().get("test" if args.test else "main", {})
    if not slot.get("stream_id"):
        die("no stream id in the state file; run setup first")
    items = yt.liveStreams().list(part="cdn", id=slot["stream_id"]).execute().get("items", [])
    if not items:
        die(f"stream {slot['stream_id']} not found")
    info = items[0]["cdn"]["ingestionInfo"]
    key, url = info["streamName"], info["ingestionAddress"]
    body = f"YOUTUBE_RTMP_URL={url}\nYOUTUBE_STREAM_KEY={key}\n"
    target = REMOTE_TEST_ENV if args.test else REMOTE_ENV
    remote = (f"umask 077; mkdir -p ~/.config/ai-chess && chmod 700 ~/.config/ai-chess && "
              f"cat > {target}.new && mv {target}.new {target} && chmod 600 {target} && "
              f"sed -n 's/^YOUTUBE_STREAM_KEY=//p' {target} | tr -d '\\n' | sha256sum | cut -c1-12 && stat -c %a {target}")
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", vps_target(args.vps), remote],
                       input=body.encode(), capture_output=True, timeout=60)
    if r.returncode != 0:
        die(f"ssh failed ({r.returncode}): {r.stderr.decode(errors='replace')[-300:]}")
    remote_sha, mode = (r.stdout.decode().split() + ["?", "?"])[:2]
    local_sha = hashlib.sha256(key.encode()).hexdigest()[:12]
    print(f"stream key: length {len(key)}, sha256 {local_sha}... | VPS {target}: sha256 {remote_sha}... mode {mode}")
    if remote_sha != local_sha:
        die("the key on the VPS does not match")


def cmd_status(yt, meta, args) -> None:
    slot = load_state().get("test" if args.test else "main", {})
    if not slot:
        die("nothing in the state file")
    b = yt.liveBroadcasts().list(part="id,snippet,status,contentDetails", id=slot["broadcast_id"]).execute().get("items", [])
    s = yt.liveStreams().list(part="id,status", id=slot["stream_id"]).execute().get("items", [])
    if b:
        st, cd = b[0]["status"], b[0]["contentDetails"]
        print(f"broadcast {b[0]['id']}: {st.get('lifeCycleStatus')} {st.get('privacyStatus')} "
              f"autoStart={cd.get('enableAutoStart')} autoStop={cd.get('enableAutoStop')} dvr={cd.get('enableDvr')} "
              f"latency={cd.get('latencyPreference')} madeForKids={st.get('madeForKids')}")
        print(f"  title: {b[0]['snippet']['title']}")
        print(f"  watch: https://www.youtube.com/watch?v={b[0]['id']}")
    if s:
        h = s[0]["status"].get("healthStatus", {})
        print(f"stream {s[0]['id']}: {s[0]['status'].get('streamStatus')} health={h.get('status')} "
              f"issues={[i.get('type') for i in h.get('configurationIssues', [])]}")


def cmd_end_test(yt, meta, args) -> None:
    slot = load_state().get("test", {})
    if not slot.get("broadcast_id"):
        die("no test broadcast")
    b = yt.liveBroadcasts().list(part="status", id=slot["broadcast_id"]).execute().get("items", [])
    life = b[0]["status"]["lifeCycleStatus"] if b else "missing"
    if life in ("live", "liveStarting", "testing"):
        yt.liveBroadcasts().transition(broadcastStatus="complete", id=slot["broadcast_id"], part="status").execute()
        print(f"test broadcast {slot['broadcast_id']}: {life} -> complete (kept, private)")
    else:
        print(f"test broadcast {slot['broadcast_id']}: {life} (no transition needed)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["check", "setup", "install-key", "status", "update-meta", "end-test", "tags"])
    ap.add_argument("--test", action="store_true", help="the separate private test stream and broadcast")
    ap.add_argument("--vps", help="user@host (else ROUND_RECORDER_VPS, else out/aichess-push/vps.txt)")
    args = ap.parse_args(argv)
    meta = load_meta()
    if args.command == "tags":
        tags = fill_tags(meta["tags"])
        print(f"{len(tags)} tags, {studio_tag_chars(tags)}/500: {', '.join(tags)}")
        return 0
    yt = service(meta["channel_slug"])
    {"check": cmd_check, "setup": cmd_setup, "install-key": cmd_install_key, "status": cmd_status,
     "update-meta": cmd_update_meta, "end-test": cmd_end_test}[args.command](yt, meta, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
