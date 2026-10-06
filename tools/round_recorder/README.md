# Round recorder

Records one live round of the LLM chess tournament viewer on a VPS (set ROUND_RECORDER_VPS=user@host): the page (Auto-focus view) at
1920x1080, the spoken commentary and the move clicks. The laptop only runs a hidden SSH tunnel; all
recording, mixing and encoding run on the VPS (no ffmpeg on the laptop).

## One command

```powershell
# Before the round starts (the recorder waits up to 12 h and starts at the round start):
pwsh tools\round_recorder\record-round.ps1 -Round 3

# While it runs:
pwsh tools\round_recorder\record-round.ps1 -Round 3 -Status

# After the round ended: mix, verify, download, copy to C:\temp (SHA-256 checked):
pwsh tools\round_recorder\record-round.ps1 -Round 3 -Collect            # MKV, fast
pwsh tools\round_recorder\record-round.ps1 -Round 3 -Collect -Format mp4 # MP4, re-encodes the video (slow)
pwsh tools\round_recorder\record-round.ps1 -Round 11 -Collect -StopTunnel
```

Options: `-Slug <tournament id>` (default: the id the viewer serves), `-Port 8770` (laptop viewer),
`-RemotePort 18770` (tunnel port on the VPS), `-Display 97`, `-MaxMinutes 75`, `-RunDir <dir under
~/acl-chess-round-rec>` (default `runs/<slug>-round<N>`), `-OutDir`, `-CopyDir`, `-Session aclrec`,
`-AllowLate` (arm a round that already started; the take then misses its start), `-WaitChange` (the
tournament is paused with the round frozen at its first plies: the take starts the moment the resumed
runner changes that round's games, so arm it while paused and resume whenever).

Outputs: `<OutDir>\round<N>-live-DRAFT###.mkv|mp4`, a `-qc` folder next to it (frame grabs at
commentary times, `verify.txt`, `plan.json`), and a byte-equal copy `C:\temp\acl-ai-chess-round<N>-live-DRAFT###.*`.
The raw take, events and logs stay on the VPS in the run dir. If the drive is too full for a second copy
and `-CopyDir` is on the same drive, the C:\temp file is an NTFS hard link to the master (same bytes).

## Series mode (the rest of the tournament, no pauses)

```powershell
# Arm once while the tournament is paused with round 5 paired, then resume the runner:
pwsh tools\round_recorder\record-round.ps1 -Series -FromRound 5 -WaitChange
# Per round: recorded?, mixed?, sizes, SHA-256, last rec.log lines, VPS free disk:
pwsh tools\round_recorder\record-round.ps1 -SeriesStatus
# Download every new DRAFT (SHA-256 checked), hard-link it into C:\temp, then free the VPS raws:
pwsh tools\round_recorder\record-round.ps1 -CollectNew -Prune
```

One Xvfb (`:97`) and one kiosk Chrome page stay up for the whole series (`rec.py series`, tmux `aclrec`).
The first take starts when round N starts (same start rule as record mode, including `--wait-change`),
then every round gets its own lossless take `raw-r<R>.mkv` (same ffmpeg settings). At a round boundary the
next ffmpeg starts first; the boundary epoch is the new take's x11grab start, and only then is the previous
take stopped, so the takes overlap by a second or two and the cut has no gap. Round boundaries:

| Round | Take ends at |
|---|---|
| round robin (no `stage`) | the `rcard hide mode=results round=R` director event + 0.5 s; fallback round_done + 90 s |
| knockout, not the last (semifinals) | round_done + 20 s (an Armageddon decider reopens the round and resets this) |
| final (stage `final`) | champion set, `champion show` seen, `heardEvents.has('champion')`, no clip playing, then 12 s; cap 180 s after the champion appears |

round_done = every game of the round has a result and the round status is `finished` (re-read every 1 s
poll). Each closed take adds or updates its entry in `rounds.json` (`round, key, raw, ffmpeg_popen_epoch,
ffmpeg_input_start, start_epoch, end_epoch, stop_reason, ...`) and touches `R<key>_REC_DONE` (content =
ffmpeg rc). If ffmpeg dies mid round, a new part starts at once (`key` = `5p2`, file `raw-r5p2.mkv`).
A failed `page.evaluate` (crash, navigation) reloads the page, or relaunches Chrome, and re-asserts Move
sound, Commentary and Auto-focus; one failed poll never ends the series. Cap: `--max-min 360`.

`mixer.sh` (tmux `aclmix`) waits for each `R<key>_REC_DONE` in `rounds.json` order and runs
`mix.py --round <key>` under `nice -n 19` while the next round records. It writes `R<key>_MIX_DONE`
(content = exit code) and `round<key>-live-DRAFT001.mkv.sha256`, waits when the disk has under 2.5 GB free
or the tunnel is down, and exits when `SERIES_DONE` exists and every closed take is mixed, or on a `STOP`
file in the run dir. `series.sh` arms both sessions.

### Director contract (page -> recorder)

The viewer dispatches `window.dispatchEvent(new CustomEvent("acl-director", {detail}))`; the init script
writes each one to `events.jsonl` as `{kind: "director", t: <page epoch ms>, ...detail}`:

- `{k:"tour", a:"start", speed:10, boards:[...], why:"quiet"|"due"}` - a board tour (time-lapse span) begins
- `{k:"tour", a:"board", game, n, of}` - informational
- `{k:"tour", a:"end"}` - the tour is over
- `{k:"rcard", a:"show"|"hide", mode:"intro"|"results"|"ko", round}` - full-screen round card
- `{k:"champion", a:"show"|"hide"}` - champion overlay

### Time-lapse and fast-forward edit (mix.py --round R)

Two kinds of sped-up span:

- Tour span (x10): from a tour `start` to the next tour `end`, clamped to the round window (a start with
  no end closes at the window end). A tour in which a host clip starts plays at normal speed; a tour that
  begins while a clip is still playing starts after that clip (plus its tail); spans shorter than 8 s are
  ignored; speed = the event's `speed` (default 10). The page shows its own ribbon here.
- Fast-forward span (x5, `FF_SPEED`): every other gap longer than 10 s (`FF_MIN_GAP`) that is not busy.
  Busy = a host clip playing (offset to offset + played) with a 1.0 s lead and a 0.5 s tail, a round card
  on screen (`rcard show` to `hide`), the champion overlay (`champion show` to `hide`). A gold `>> x5`
  cue (ffmpeg drawtext, DejaVu Sans Bold, bottom centre) is drawn on these segments only; without drawtext
  or the font the cue is skipped with a warning. `--no-fast-forward` keeps the tour-only edit.

Measured on the real takes: rounds 5, 6 and 7 (33.3, 30.1 and 26.0 min raw) plan to 12.1, 10.6 and 10.5
min with fast-forward, against 24.6, 22.8 and 18.5 min with tours only.

The segment table `[(raw_start, raw_end, speed, kind)]` covers the round, frame aligned, each sped-up
segment a whole multiple of its speed in frames. The video is ONE pass: per segment
`trim=start_frame:end_frame,setpts=(PTS-STARTPTS)/S` (`fps=30` after it when S > 1, plus the cue on
fast-forward segments) joined with `concat`, libx264 ultrafast CRF 0 yuv444p 30 fps, 2 threads, nice 19.
The audio is built on the OUTPUT timeline: each speech clip offset and click time goes through `remap(t)`
(piecewise linear), clicks inside sped-up spans are dropped, a clip still playing at the window start is
kept from that point, the music bed is looped for the output duration and ducked under the speech. The
24-bit FLAC premix then gets a gain to -16 LUFS and a limiter at -2.0 dBFS written straight to the
delivered 16-bit FLAC; that file is measured (EBUR128, true peak) and the pass re-run with a corrected
gain (and a lower ceiling after a true-peak miss) until it lands within -16 +-0.3 LUFS and <= -1.5 dBTP
(at most 3 passes). The video pass muxes those exact bytes (`-c:a copy`). Outputs per round, in the run dir:

- `round<R>-live-DRAFT001.mkv` (lossless video, FLAC audio) and `round<R>-live-DRAFT001.mkv.sha256`
- `round<R>-verify.txt`: durations (video, audio, planned), frames vs duration x 30, loudness, raw vs
  output length and how much raw time each speed covers, the segment table (raw span, speed, output
  span, kind), tours kept normal and why, dropped clicks, volumedetect at clip offsets
- `grabs-r<R>/`: 4 frame grabs, one inside the longest fast-forward span and one inside the longest tour
- `mix-r<R>/plan.json` and the filter scripts

Re-mix a finished round with a new name (the raw must still be on the VPS; not while `aclmix` is mixing):

```bash
cd ~/acl-chess-round-rec && nice -n 19 python3 bin/mix.py runs/<series-run> round7-live-DRAFT002.mkv --round 7 --port 18770 > runs/<series-run>/mix-r7-draft002.log 2>&1
```

`-SeriesStatus` and `-CollectNew` follow the newest DRAFT of each round that has a `.sha256` file.

### Disk (the VPS has about 14 GB free)

Per round about 0.8 GB raw plus about 0.7 GB DRAFT (less when tours are lapsed). The mix keeps its
intermediates small (16-bit clicks track, 24-bit FLAC premix; no float WAVs) and deletes them after a
successful mix. `-CollectNew -Prune` removes a round's `raw-r<R>.mkv` only after its DRAFT was downloaded
and SHA-256 verified; nothing else is ever deleted.

## How it works

| File | Runs on | Job |
|---|---|---|
| `record-round.ps1` | laptop | hidden reverse tunnel (`run-hidden-cmd.vbs`, PID in `out\round-recorder\`), deploy, arm, status, collect |
| `rec.py` | VPS | Xvfb `:97`, kiosk Chrome via Playwright, unmutes Commentary at the round start, keeps Move sound on, logs clip and click events, records `raw.mkv` (x11grab, H.264 CRF 0, yuv444p, 30 fps) |
| `mix.py` | VPS | speech clips placed at their logged start (`adelay`, `amix normalize=0`, no ducking), about -16 LUFS, true peak under -1.5 dBTP; clicks at -20 dBFS peak; mux; verify |
| `click_sound.py` | VPS | the 50 ms wooden click, same recipe as the viewer's `playClick()` |
| `timeline.py` | VPS | pure python: lapse spans, segment table, `remap(t)`, round windows, round boundary state machine (unit tested in `tests/test_round_recorder_timeline.py`) |
| `arm.sh`, `collect.sh`, `status.sh` | VPS | tmux wrappers with `REC_DONE` / `MIX_DONE` markers |
| `series.sh`, `mixer.sh`, `series-status.sh` | VPS | series mode: arm `aclrec` + `aclmix`, mix rounds in order, per-round status |

Timing: the page logs `performance.timeOrigin + performance.now()` for every clip `playing`/`pause`/`ended`
and every `AudioBufferSourceNode.start` (the click). ffmpeg prints the x11grab input start as a wall-clock
epoch; both use the VPS clock, so `offset = event - x11grab start`. Each clip is trimmed to what the page
actually played (a clip cut short by the page is cut short in the mix too).

Stop rule: every game in the round's pairings has a result AND the round status is `finished`, then a
15 s tail. Pairings are re-read every 2 s, so a knockout Armageddon decider added late (rounds 10, 11)
re-opens the round and the take keeps going. Cap: `-MaxMinutes`.

The commentator only speaks while a page listens; the recorder unmutes only when the round starts, so
no speech budget is spent while it waits.

## Formats

- `mkv` (default): the lossless raw video is stream-copied (H.264 CRF 0, High 4:4:4, yuv444p) with FLAC
  audio. Ready a minute after the round. Plays in VLC/mpv; some built-in Windows players may not
  decode 4:4:4.
- `mp4`: video re-encoded to H.264 CRF 0 yuv420p (still lossless in the encoder, chroma subsampled) with
  AAC 320k. Takes roughly the round length again on the VPS.

## Rules kept

Own VPS run dir `~/acl-chess-round-rec`, own tmux sessions `aclrec` and `aclmix`, own display `:97`, own
tunnel port 18770. Never touch other agents' sessions. The launcher stops only the tunnel PID it started.
`-SeriesStatus`, `-CollectNew` and `-Prune` never redeploy the scripts (a live mixer must not be overwritten).
