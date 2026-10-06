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

## How it works

| File | Runs on | Job |
|---|---|---|
| `record-round.ps1` | laptop | hidden reverse tunnel (`run-hidden-cmd.vbs`, PID in `out\round-recorder\`), deploy, arm, status, collect |
| `rec.py` | VPS | Xvfb `:97`, kiosk Chrome via Playwright, unmutes Commentary at the round start, keeps Move sound on, logs clip and click events, records `raw.mkv` (x11grab, H.264 CRF 0, yuv444p, 30 fps) |
| `mix.py` | VPS | speech clips placed at their logged start (`adelay`, `amix normalize=0`, no ducking), about -16 LUFS, true peak under -1.5 dBTP; clicks at -20 dBFS peak; mux; verify |
| `click_sound.py` | VPS | the 50 ms wooden click, same recipe as the viewer's `playClick()` |
| `arm.sh`, `collect.sh`, `status.sh` | VPS | tmux wrappers with `REC_DONE` / `MIX_DONE` markers |

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

Own VPS run dir `~/acl-chess-round-rec`, own tmux session `aclrec`, own display `:97`, own tunnel port
18770. Never touch other agents' sessions. The launcher stops only the tunnel PID it started.
