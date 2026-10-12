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

## Twitch (optional second target)

The same encode can also go to a Twitch channel. There is no second encoder: ffmpeg's tee muxer
sends the one encoded stream to both places, so the extra cost is about 4.7 Mbit/s of upload.

- On: put the Twitch key on the VPS in `~/.config/ai-chess/twitch-stream.env` (mode 600, lines
  `TWITCH_RTMP_URL=` and `TWITCH_STREAM_KEY=`), then `sudo systemctl restart ai-chess-stream`.
  From the laptop, with the key copied from the Twitch dashboard (Settings > Stream > Copy):
  `pwsh tools/aichess_stream/install-twitch-key.ps1` (reads the clipboard, prints length + sha256 prefix only).
- Off: delete that file (or set `AICHESS_TWITCH_KEY_FILE=` empty in `stream.env`) and restart the unit.
  Without a key the script builds exactly the single-output YouTube command.
- YouTube stays the main target, a plain leg: when it fails ffmpeg exits and the watchdog restarts it, as before.
- Twitch runs behind a queue in its own thread (fifo muxer). It reconnects by itself every 10 s and
  drops its own packets while it is slow or down, so it cannot hold up or stop YouTube. Twitch ends
  every broadcast at 48 hours; the reconnect starts the next one.
- `status.sh` prints `targets: YouTube + Twitch, N of 2 connected`; the journal logs every change of N.
- Twitch rules for a stream that is also on another platform: the Twitch picture may not be worse
  than the other one (it is the same encode) and the Twitch page may not send viewers to the other
  stream. The `?stream=1` page already hides the YouTube link.

Pilot on the VPS (tiny test picture, `tee_spec` cut out of `stream.sh`, 2026-10-11): second target
dead for the whole run = main target complete (12.0 s of 12 s); second target killed at 8 s and back
at 14 s = main target complete (40.0 s of 40 s) and the second target received 22 s after its return;
main target dead = ffmpeg exits with an error.

## Twitch show times (the gate)

Twitch counts "average viewers" over every minute the channel is live. Its Affiliate rule is an average
of 3 viewers on 4 different days, so a stream that is live 24 hours needs 72 viewer-hours on such a day
and a 2 hour show needs 6. So Twitch can get a daily show while YouTube stays live all day.

- `AICHESS_TWITCH_GATE=rtmp://127.0.0.1:19350/live/aichess` in `stream.env` sends the second output to
  the gate (unit `ai-chess-twitch-gate`, `twitch_gate.py`) instead of straight to Twitch. The gate runs one
  copy-only ffmpeg: to Twitch while the show is on, to nowhere (`-f null`) while it is off. So the
  stream's second leg stays connected and `status.sh` keeps saying `2 of 2 connected`.
- Schedule: `~/.config/ai-chess/twitch-show.env`, read again whenever it changes (no restart):
  `python3 ~/.local/lib/ai-chess-stream/twitch_gate.py set --mode daily --start 19:00 --minutes 120 --tz Africa/Johannesburg`,
  `... set --mode always` (24 hours, as before the gate), `... set --mode off`. No file = `always`.
  Bad input is refused and nothing is saved; a file that is broken by hand keeps the last good schedule,
  and at a gate start it means off, never 24 hours.
- At a change the gate stops its ffmpeg and starts the other one; the stream's second leg reconnects
  in about 10 s. Every show is a new Twitch broadcast, so followers get a new go-live notice each day.
- `status.sh` prints `Twitch show: daily 19:00 for 120 min (Africa/Johannesburg) - now off air, starts Tue 19:00 (in 2h29m)`.
  `twitch_gate.py status` prints the same line plus the status file (`/run/ai-chess-twitch-gate/status.json`).
- Keep the show inside one calendar day in UTC and in US Pacific time (19:00 to 21:00 in South Africa is
  17:00 to 19:00 UTC and 10:00 to 12:00 Pacific), so no show is split over two Twitch days.
- Gate down = the second leg retries every 10 s and YouTube is not touched. Back to no gate: remove the
  `AICHESS_TWITCH_GATE` line and restart `ai-chess-stream`.

Pilot on the VPS (test picture, own ports, fake Twitch = a local `ffmpeg -listen 1`, 2026-10-12): gate
off for 24 s, on for 34 s, off again; main target complete (95.0 s of 95 s), the fake Twitch got 24 s
(the 10 s reconnect), nothing while off, the key in no log.

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
| `ai-chess-twitch-gate` | `python3 ~/.local/lib/ai-chess-stream/twitch_gate.py run` | Passes the stream on to Twitch only while the show is on (`twitch-show.env`). Reads the address and the key file from `stream.env`. Uses one copy-only ffmpeg, no encode. |

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

## The waiting screen (no game is live)

Between rounds, before an Armageddon decider and between tournaments no game is live. Every page then shows one
card in the boards area: what is next ("Round 3 of 5", "The Final", "Tournament #37"), a countdown and a winners
banner (the results of the last round and the leader of the table, or the champion with second and third place).
On the stream the card fills the boards area and the finished boards are hidden; on the other pages it sits above
them. A board picked by the viewer (focus) keeps the screen, so no card there.

The next game starts when the AIs have written their notes, so the runner has no start time to publish. The
countdown is the usual length of such a wait: `wait_stats` in `/api/tournament` (`game_typical_s`,
`tournament_typical_s`, each with its sample count) is the median over the 12 newest tournament files in the live
dir, measured from the saved start and end of every game; gaps over one hour count as outages and are left out.
The page words it "starts in about", then "any moment now" with the time waited, and "longer than usual" at three
times the usual wait. With no measured wait it shows only the time waited. It never shows a made-up time.
Logic: `waitPlan()` in the page (between `WAIT-PLAN-START` and `WAIT-PLAN-END`, tested in node by
`tests/test_aichess_wait_screen.py`), `tournament_timing()` and `wait_stats()` in the viewer.

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
