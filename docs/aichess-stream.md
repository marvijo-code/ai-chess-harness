# 24/7 YouTube livestream of the AI-chess tournament (VPS)

The forever tournament runs on the VPS. A virtual screen shows the tournament viewer, and ffmpeg
sends that screen plus the viewer's spoken commentary to a YouTube live broadcast on the
EtymologyExplanations channel (`UCIxxnCAx77qZWK63j__ni8A`). systemd keeps every piece running.

```
ai-chess-runner  (run-ai-chess-forever.sh, claude/codex CLI moves)  -> out/live/current.json
ai-chess-viewer  (tools/llm_tournament_viewer.py, 127.0.0.1:8770)    <- reads the state, plays commentary clips
ai-chess-pusher  (tools/aichess_push.py)  -> relay ingest 127.0.0.1:8781 -> public relay :8780 -> marvijo.com/ai-chess
ai-chess-stream  Xvfb :87 + Chrome kiosk on :8770 (audio -> PulseAudio null sink "aichess")
                 + music bed player (-> null sink "music")
                 ffmpeg x11grab + both sink monitors (bed ducked under speech) -> libx264 + AAC -> RTMP YouTube
```

## YouTube

| What | Value |
| --- | --- |
| Broadcast (public, the real one) | `vnNTtpmCG_Y`, https://www.youtube.com/watch?v=vnNTtpmCG_Y |
| Its reusable stream | `IxxnCAx77qZWK63j__ni8A1791449658751985` (RTMP, resolution and frame rate variable) |
| Settings | public, auto start on, auto stop off, DVR on, latency normal, not made for kids, category Gaming (20), 27 tags (499/500) |
| Test broadcast (private) | `w8EzMPpsIc4`, completed after the pipeline test, kept private. Its stream `IxxnCAx77qZWK63j__ni8A1791449679831845` |

Auto start is on: the public broadcast goes live by itself on the first data. The stream unit
therefore sends nothing until the viewer answers and Chrome shows it (see "Safety" below).
Auto stop is off: a restart or a short outage does not end the broadcast.

`tools/aichess_stream/youtube_live.py` (runs on the laptop, token in `C:\dev\ai-tools\secrets\youtube-oauth\gamer_like_you.json`):

```powershell
python tools/aichess_stream/youtube_live.py check          # broadcasts and streams on the channel
python tools/aichess_stream/youtube_live.py status         # public broadcast lifecycle + stream health
python tools/aichess_stream/youtube_live.py update-meta    # re-apply title, description, tags from youtube_live.json
python tools/aichess_stream/youtube_live.py install-key    # stream key -> VPS (stdin, mode 600), prints length + sha256 prefix
python tools/aichess_stream/youtube_live.py tags           # show the tag list and the Studio character count
```

Title, description and tags live in `tools/aichess_stream/youtube_live.json`. Ids are kept in the
gitignored `out/aichess-stream/youtube-live-state.json`. The stream key is only on the VPS in
`~/.config/ai-chess/youtube-stream.env` (mode 600, lines `YOUTUBE_RTMP_URL=` and `YOUTUBE_STREAM_KEY=`).
It is never written on the laptop, never exported to Chrome and never logged (ffmpeg output passes
through a filter that replaces it with `<key>`). It is in ffmpeg's command line, so local users of the
VPS can see it with `ps`.

## Install and enable (VPS)

```bash
cd ~/ai-chess && git pull                                   # integration branch with tools/aichess_stream
bash tools/aichess_stream/install-services.sh               # copy stream scripts, write the 4 units, daemon-reload
bash tools/aichess_stream/install-services.sh --enable viewer,pusher,runner,stream   # or: --enable all
bash ~/.local/lib/ai-chess-stream/status.sh                 # one-screen status
```

`install-services.sh` is idempotent. It copies `stream.sh`, `cdp.py`, `screenshot.sh`, `status.sh` to
`~/.local/lib/ai-chess-stream/` (the stream does not depend on the branch of `~/ai-chess`), writes
`/etc/systemd/system/ai-chess-{runner,viewer,pusher,stream}.service`, and creates
`~/.config/ai-chess/services.env` and `stream.env` from the examples when they are missing. Without
`--enable` nothing is enabled or started. `--disable all` stops and disables them.

Before enabling the VPS pusher, stop the laptop pusher (`.\run-aichess-push.ps1 -Stop`): two pushers
would post the same relay. The relay itself stays in tmux session `aclrelay` (unchanged).

The music bed (owner-approved channel bed, `C:\dev\ai-tools\assets\audio\music\marvijo-channel-bed-cinematic-ambient.mp3`,
SHA-256 `edd85750...`) is copied once to `~/.local/share/ai-chess-stream/music-bed.mp3`:

```powershell
scp C:\dev\ai-tools\assets\audio\music\marvijo-channel-bed-cinematic-ambient.mp3 <vps>:.local/share/ai-chess-stream/music-bed.mp3
```

Remove the file or set `AICHESS_STREAM_MUSIC=` (empty) in `stream.env` for no music.

## Units

| Unit | Runs | Notes |
| --- | --- | --- |
| `ai-chess-runner` | `~/ai-chess/run-ai-chess-forever.sh $AICHESS_RUNNER_ARGS` | Only starts when the launcher exists. PATH has `~/.local/bin` (claude) and `/usr/bin` (codex). `ANTHROPIC_BASE_URL` and API-key variables are unset so the CLIs use the subscriptions. Stop kills the whole process group (CLI children too). |
| `ai-chess-viewer` | `python3 tools/llm_tournament_viewer.py $AICHESS_VIEWER_ARGS` | Default args: `--port 8770 --follow out/live/current.json --engine ~/sf19/stockfish/stockfish-linux-x86-64-universal --commentary`. `--follow` comes from the forever-runner branch. |
| `ai-chess-pusher` | `python3 tools/aichess_push.py $AICHESS_PUSHER_ARGS` | Default args post straight to `127.0.0.1:8781` with `~/acl-chess-relay/ingest.token`, live dir `~/ai-chess/out/live`. |
| `ai-chess-stream` | `~/.local/lib/ai-chess-stream/stream.sh` | Settings in `~/.config/ai-chess/stream.env`. OOMScoreAdjust -300, Nice -5, CPUWeight 200: viewers see this one. |

All units: system units with `User=marvijo`, `Restart=always`, no start limit. Arguments are in
`~/.config/ai-chess/services.env` (edit, then `sudo systemctl restart ai-chess-<name>`).

## Stream settings (`stream.env`)

1920x1080, 30 fps, x264 `veryfast` + `zerolatency`, CBR 4500 kbit/s, keyframe every 2 s, AAC 128 kbit/s 48 kHz.
Other knobs (see the header of `stream.sh`): `AICHESS_STREAM_URL`, `AICHESS_STREAM_SCALE` (Chrome zoom),
`AICHESS_STREAM_DISPLAY` (`:87`), `AICHESS_CDP_PORT` (`9223`, 127.0.0.1 only), `AICHESS_CHROME_MAX_MB`
(Chrome restart threshold, 1400), `AICHESS_STREAM_MUSIC_GAIN` (0.07), `AICHESS_STREAM_LOCALSTORAGE`
(viewer settings seeded into the page, default `{"swissCommentary":"on"}`: the local viewer plays
commentary only when this is on), `AICHESS_STREAM_OUTPUT` (`rtmp`, `null` for a CPU test, or a `.flv` path).

Measured on the VPS (4 vCPU), streaming to the private test broadcast, YouTube health "good":

| Process | CPU (cores) | RSS |
| --- | --- | --- |
| ffmpeg (encode + audio mix) | 1.0 | 240 MB |
| Chrome (all processes) | 0.35 | 600 to 715 MB |
| Xvfb | 0.04 | 180 MB |
| PulseAudio, bed player | about 0 | under 40 MB |

0 duplicated and 0 dropped frames after the first second (checked over 60 s).

## How the stream supervises itself

- ffmpeg exits (RTMP drop): restarted after 3 s, doubling to 60 s.
- Chrome exits, shows a Chrome error page while the viewer is up, or grows past `AICHESS_CHROME_MAX_MB`: restarted (fresh profile, settings re-seeded).
- Xvfb or PulseAudio exits: the script exits, systemd restarts the unit.
- The bed player exits: restarted.
- Status: `/run/ai-chess-stream/status.json` and `progress.txt` (newest ffmpeg progress block); `status.sh` prints both.

## Safety

- RTMP mode sends nothing while the viewer does not answer or Chrome shows no viewer page (tested with a dead URL).
- Everything the stream starts is its own: display `:87`, its own PulseAudio (socket in `/run/ai-chess-stream`), its own Chrome profile. Stop kills only the unit's process group.
- Screenshot of exactly what is sent: `bash ~/.local/lib/ai-chess-stream/screenshot.sh ~/frame.png` (one x11grab frame).

## Lessons (why the ffmpeg line looks like this)

- `-copyts` and wall-clock audio timestamps: without them ffmpeg zeroes every input on its own. The
  audio input opened about 1.3 s after the screen, so video looked 1.3 s ahead and ffmpeg's scheduler
  kept pausing the screen capture: only 10 to 12 real frames per second, grabbed in 2 ms bursts
  (thousands of dup/drop frames, 10 distinct frames where a manual capture had 70). `-output_ts_offset`
  brings the wall-clock timestamps back near 0 for FLV.
- The music bed is played into its own null sink by a small player with a 30 ms buffer and read back
  as a monitor. A file input with `-re` stalls under `-copyts`, and a player with the default 2 s
  buffer makes the sink deliver 2 s chunks, which pauses the screen capture again.
- `-fragment_size 1920` (10 ms) on both pulse inputs: with 50 ms fragments the two audio streams
  arrived out of phase and caused about 40 dup/drop pairs per 30 s.
- Bash runs the EXIT trap in process-substitution subshells: `cleanup` returns early unless it runs in
  the main shell, or a restarted ffmpeg's log filter would kill Xvfb.

## Open points for the integrator

- The finished-tournament screen shows a champion pop-up that waits for a click ("Back to the boards").
  Between forever tournaments the viewer should close it by itself, or the stream shows it until the next
  tournament starts.
- At 1920x1080 the current viewer page is 1151 px tall: the last rows of the round robin table are below
  the screen edge. `AICHESS_STREAM_SCALE=0.93` would fit it (not tested), or the new viewer can fit 1080.
- Memory (measured 2026-10-08): about 2.3 GB available before the stream, 2 GB swap already full.
  Stream about 1.1 GB, viewer with Stockfish about 1 GB (Stockfish alone 930 MB: the analyzer and
  annotator hash), 3 CLI calls about 0.5 to 0.9 GB. That does not fit. Two fixes: a smaller Stockfish
  hash in the viewer (64 MB saves about 700 MB), and clearing old work folders in `/dev/shm` (RAM-backed,
  1.9 GB from earlier jobs: `jd30-*`, `fa930-*`, `ety1004`; owner decision).
