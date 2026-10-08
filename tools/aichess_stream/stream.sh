#!/usr/bin/env bash
# 24/7 YouTube stream of the AI-chess tournament viewer (runs on the VPS, started by ai-chess-stream.service).
#
#   Xvfb (virtual screen) -> Chrome kiosk on the viewer URL (audio -> PulseAudio null sink "aichess")
#   ffmpeg x11grab + pulse monitor (+ optional ducked music bed) -> libx264 + AAC -> RTMP (YouTube)
#
# Everything it starts is its own: its own X display, its own PulseAudio daemon (socket in the run dir),
# its own Chrome profile. On exit it stops exactly those processes.
# Supervision inside: ffmpeg is restarted on exit (RTMP drop) with backoff; Chrome is restarted when it
# dies, shows a Chrome error page while the viewer is up, or grows past AICHESS_CHROME_MAX_MB. If Xvfb or
# PulseAudio dies the script exits non-zero and systemd restarts the whole unit.
#
# The stream key is read from $AICHESS_STREAM_KEY_FILE (default ~/.config/ai-chess/youtube-stream.env,
# lines YOUTUBE_RTMP_URL=... and YOUTUBE_STREAM_KEY=...). It is never exported to Chrome and never
# logged: ffmpeg output passes through a filter that replaces it with <key>.
#
# Settings (environment, or ~/.config/ai-chess/stream.env through the unit):
#   AICHESS_STREAM_URL        viewer URL                      (http://127.0.0.1:8770/)
#   AICHESS_STREAM_SIZE       capture size WxH                (1920x1080)
#   AICHESS_STREAM_FPS        frames per second               (30)
#   AICHESS_STREAM_PRESET     x264 preset                     (veryfast)
#   AICHESS_STREAM_VBITRATE   video bitrate, kbit/s, CBR      (4500)
#   AICHESS_STREAM_ABITRATE   audio bitrate                   (128k)
#   AICHESS_STREAM_SCALE      Chrome device scale factor      (1)
#   AICHESS_STREAM_DISPLAY    X display                       (:87)
#   AICHESS_CDP_PORT          Chrome DevTools port, 127.0.0.1 (9223)
#   AICHESS_STREAM_MUSIC      music bed file, "" = none       (~/.local/share/ai-chess-stream/music-bed.mp3 if present)
#   AICHESS_STREAM_MUSIC_GAIN bed gain before ducking         (0.07)
#   AICHESS_STREAM_LOCALSTORAGE  JSON of viewer settings      ({"swissCommentary":"on"})
#   AICHESS_CHROME_MAX_MB     restart Chrome above this RSS   (1400)
#   AICHESS_STREAM_OUTPUT     "rtmp" (default), "null" (encode, discard) or a file path (.flv) for tests
#   AICHESS_STREAM_KEY_FILE   env file with the key           (~/.config/ai-chess/youtube-stream.env)
#   AICHESS_CHROME            browser binary                  (google-chrome, else Playwright chromium)
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"

URL="${AICHESS_STREAM_URL:-http://127.0.0.1:8770/}"
SIZE="${AICHESS_STREAM_SIZE:-1920x1080}"
FPS="${AICHESS_STREAM_FPS:-30}"
PRESET="${AICHESS_STREAM_PRESET:-veryfast}"
VB="${AICHESS_STREAM_VBITRATE:-4500}"
AB="${AICHESS_STREAM_ABITRATE:-128k}"
SCALE="${AICHESS_STREAM_SCALE:-1}"
DISP="${AICHESS_STREAM_DISPLAY:-:87}"
CDP_PORT="${AICHESS_CDP_PORT:-9223}"
DEFAULT_MUSIC="$HOME/.local/share/ai-chess-stream/music-bed.mp3"
if [ -z "${AICHESS_STREAM_MUSIC+x}" ]; then
  if [ -f "$DEFAULT_MUSIC" ]; then MUSIC="$DEFAULT_MUSIC"; else MUSIC=""; fi
else
  MUSIC="$AICHESS_STREAM_MUSIC"
fi
MUSIC_GAIN="${AICHESS_STREAM_MUSIC_GAIN:-0.07}"
LOCALSTORAGE="${AICHESS_STREAM_LOCALSTORAGE:-}"
[ -n "$LOCALSTORAGE" ] || LOCALSTORAGE='{"swissCommentary":"on"}'
CHROME_MAX_MB="${AICHESS_CHROME_MAX_MB:-1400}"
OUTPUT="${AICHESS_STREAM_OUTPUT:-rtmp}"
KEY_FILE="${AICHESS_STREAM_KEY_FILE:-$HOME/.config/ai-chess/youtube-stream.env}"
RUN="${RUNTIME_DIRECTORY:-${XDG_RUNTIME_DIR:-/tmp}/ai-chess-stream}"
STATE_DIR="$HOME/.local/share/ai-chess-stream"
PROFILE="$STATE_DIR/chrome-profile"
W="${SIZE%x*}"; H="${SIZE#*x}"

log() { echo "$(date '+%F %T') stream: $*"; }

CHROME_BIN="${AICHESS_CHROME:-}"
if [ -z "$CHROME_BIN" ]; then
  if command -v google-chrome >/dev/null 2>&1; then CHROME_BIN="$(command -v google-chrome)"
  else CHROME_BIN="$(ls -d "$HOME"/.cache/ms-playwright/chromium-*/chrome-linux*/chrome 2>/dev/null | sort | tail -n1)"; fi
fi
[ -x "$CHROME_BIN" ] || { log "no Chrome/Chromium found"; exit 1; }

mkdir -p "$RUN" "$STATE_DIR"
chmod 700 "$RUN"

XVFB_PID=""; PULSE_PID=""; CHROME_PID=""; FF_PID=""; PROG_PID=""
cleanup() {
  # bash also runs this EXIT trap in subshells (the ffmpeg log filter); only the main shell may clean up,
  # or a restarted ffmpeg's old log filter would kill Xvfb (seen in testing).
  [ "${BASHPID:-$$}" = "$$" ] || return 0
  trap - EXIT INT TERM
  log "stopping"
  [ -n "$FF_PID" ] && kill "$FF_PID" 2>/dev/null
  [ -n "${MUSIC_PID:-}" ] && kill "$MUSIC_PID" 2>/dev/null
  stop_chrome
  [ -n "$PROG_PID" ] && kill "$PROG_PID" 2>/dev/null
  [ -n "$PULSE_PID" ] && kill "$PULSE_PID" 2>/dev/null
  [ -n "$XVFB_PID" ] && kill "$XVFB_PID" 2>/dev/null
  sleep 1
  [ -n "$XVFB_PID" ] && kill -9 "$XVFB_PID" 2>/dev/null
  rm -f "$RUN/progress.fifo"
}
trap cleanup EXIT
trap 'exit 0' INT TERM

alive() { [ -n "$1" ] && kill -0 "$1" 2>/dev/null; }
my_chrome_pids() { pgrep -u "$(id -u)" -f -- "--user-data-dir=$PROFILE" || true; }
chrome_rss_mb() { local p; p="$(my_chrome_pids | tr '\n' ',' | sed 's/,$//')"; [ -z "$p" ] && { echo 0; return; }; ps -o rss= -p "$p" | awk '{s+=$1} END {print int(s/1024)}'; }
stop_chrome() {
  local p; p="$(my_chrome_pids)"
  [ -n "$p" ] && kill $p 2>/dev/null
  for _ in 1 2 3 4 5; do [ -z "$(my_chrome_pids)" ] && break; sleep 1; done
  p="$(my_chrome_pids)"; [ -n "$p" ] && kill -9 $p 2>/dev/null
  CHROME_PID=""
}

# ---- 1. virtual screen
DNUM="${DISP#:}"
if [ -e "/tmp/.X${DNUM}-lock" ]; then
  owner="$(cat "/tmp/.X${DNUM}-lock" 2>/dev/null | tr -d ' ')"
  if [ -n "$owner" ] && kill -0 "$owner" 2>/dev/null; then log "display $DISP is in use by pid $owner; pick another AICHESS_STREAM_DISPLAY"; exit 1; fi
  rm -f "/tmp/.X${DNUM}-lock" "/tmp/.X11-unix/X${DNUM}" 2>/dev/null
fi
Xvfb "$DISP" -screen 0 "${W}x${H}x24" -nolisten tcp -noreset -dpi 96 >"$RUN/xvfb.log" 2>&1 &
XVFB_PID=$!
for _ in $(seq 1 50); do [ -e "/tmp/.X11-unix/X${DNUM}" ] && break; sleep 0.1; done
alive "$XVFB_PID" || { log "Xvfb failed: $(tail -n 3 "$RUN/xvfb.log")"; exit 1; }
export DISPLAY="$DISP"

# ---- 2. own PulseAudio with one null sink; Chrome plays into it, ffmpeg records its monitor
export XDG_RUNTIME_DIR="$RUN" PULSE_RUNTIME_PATH="$RUN/pulse" PULSE_STATE_PATH="$RUN/pulse-state"
mkdir -p "$PULSE_RUNTIME_PATH" "$PULSE_STATE_PATH"
cat >"$RUN/stream.pa" <<EOF
load-module module-native-protocol-unix socket=$PULSE_RUNTIME_PATH/native auth-anonymous=1
load-module module-null-sink sink_name=aichess rate=48000 channels=2 sink_properties=device.description=aichess
load-module module-null-sink sink_name=music rate=48000 channels=2 sink_properties=device.description=music
set-default-sink aichess
set-default-source aichess.monitor
EOF
pulseaudio -n --daemonize=no --exit-idle-time=-1 --use-pid-file=no --disallow-module-loading=no \
  -F "$RUN/stream.pa" --log-target=stderr --log-level=warning >"$RUN/pulse.log" 2>&1 &
PULSE_PID=$!
export PULSE_SERVER="unix:$PULSE_RUNTIME_PATH/native"
for _ in $(seq 1 50); do pactl info >/dev/null 2>&1 && break; sleep 0.2; done
pactl info >/dev/null 2>&1 || { log "PulseAudio failed: $(tail -n 3 "$RUN/pulse.log")"; exit 1; }

# ---- 3. Chrome kiosk on the viewer
start_chrome() {
  # wait for the viewer (at most 120 s); after that open it anyway: the watchdog reloads the error page
  for _ in $(seq 1 120); do curl -sf -o /dev/null --max-time 3 "$URL" && break; sleep 1; done
  rm -rf "$PROFILE"; mkdir -p "$PROFILE"
  "$CHROME_BIN" --user-data-dir="$PROFILE" --kiosk --app="$URL" \
    --window-position=0,0 --window-size="$W,$H" --force-device-scale-factor="$SCALE" \
    --autoplay-policy=no-user-gesture-required --no-first-run --no-default-browser-check --noerrdialogs \
    --disable-infobars --disable-session-crashed-bubble --disable-translate --disable-features=Translate,MediaRouter \
    --disable-sync --disable-background-networking --disable-component-update --password-store=basic \
    --disable-background-timer-throttling --disable-renderer-backgrounding --disable-backgrounding-occluded-windows \
    --disable-gpu --hide-scrollbars --renderer-process-limit=2 --disk-cache-size=52428800 \
    --remote-debugging-address=127.0.0.1 --remote-debugging-port="$CDP_PORT" \
    >"$RUN/chrome.log" 2>&1 &
  CHROME_PID=$!
  SEEDED=0
  CHROME_STARTED=$(date +%s)
  log "chrome pid $CHROME_PID on $URL (${W}x${H}, scale $SCALE)"
}
CHROME_RESTARTS=0
start_chrome

# ---- 4. ffmpeg
read_key() {
  YT_URL=""; YT_KEY=""
  [ -r "$KEY_FILE" ] || return 1
  YT_URL="$(sed -n 's/^YOUTUBE_RTMP_URL=//p' "$KEY_FILE" | tail -n1)"
  YT_KEY="$(sed -n 's/^YOUTUBE_STREAM_KEY=//p' "$KEY_FILE" | tail -n1)"
  [ -n "$YT_KEY" ] || return 1
  [ -n "$YT_URL" ] || YT_URL="rtmp://a.rtmp.youtube.com/live2"
}

rm -f "$RUN/progress.fifo"; mkfifo "$RUN/progress.fifo"
# keep only the newest ffmpeg -progress block in progress.txt (the raw stream would grow forever)
awk -F= -v out="$RUN/progress.txt" '{a[$1]=$2} /^progress=/{printf "" > out; for (k in a) print k "=" a[k] > out; close(out)}' \
  <>"$RUN/progress.fifo" &
PROG_PID=$!

start_ffmpeg() {
  local out_args=() in_music=() af
  FF_PID=""; FF_STARTED=$(date +%s)
  case "$OUTPUT" in
    rtmp)
      if ! read_key; then log "no stream key in $KEY_FILE"; return 1; fi
      # the public broadcast auto-starts on the first data: never send before the viewer answers
      if ! curl -sf -o /dev/null --max-time 3 "$URL"; then log "viewer $URL not up yet; not sending to YouTube"; return 1; fi
      if ! python3 "$HERE/cdp.py" --port "$CDP_PORT" info 2>/dev/null | grep -q '"url": "http'; then
        log "browser does not show the viewer yet; not sending to YouTube"
        python3 "$HERE/cdp.py" --port "$CDP_PORT" reload >/dev/null 2>&1 || true
        return 1
      fi
      out_args=(-f flv "$YT_URL/$YT_KEY") ;;
    null) out_args=(-f null -) ;;
    *) out_args=(-f flv -y "$OUTPUT") ;;
  esac
  af="[1:a]aresample=48000:async=1000,aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"
  if [ -n "$MUSIC_PID" ]; then
    # the bed player loops the file into the "music" null sink; reading its monitor keeps the bed on the
    # same wall clock as the screen and the commentary (a file input with -re stalls under -copyts)
    in_music=(-thread_queue_size 1024 -probesize 32 -analyzeduration 0 -use_wallclock_as_timestamps 1 -f pulse -fragment_size 1920 -sample_rate 48000 -channels 2 -i music.monitor)
    af="$af,asplit=2[sp][sc];[2:a]aresample=48000,aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,volume=$MUSIC_GAIN[bed];[bed][sc]sidechaincompress=threshold=0.012:ratio=12:attack=120:release=1400[duck];[sp][duck]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.89[aout]"
  else
    af="$af,alimiter=limit=0.89[aout]"
  fi
  local gop=$((FPS * 2))
  # -copyts keeps every input on the shared wall clock (x11grab and pulse both stamp packets with it).
  # Without it ffmpeg zeroes each input on its own; the audio input opens about a second after the
  # screen, so video looks a second "ahead" and ffmpeg's scheduler keeps pausing the screen capture:
  # measured 10-12 real frames per second plus bursts (dup/drop counters in the thousands).
  # -output_ts_offset brings the wall-clock timestamps back to about 0 for FLV (32-bit milliseconds).
  ffmpeg -nostdin -hide_banner -nostats -loglevel warning -copyts \
    -thread_queue_size 512 -probesize 32 -analyzeduration 0 \
    -f x11grab -draw_mouse 0 -framerate "$FPS" -video_size "${W}x${H}" -i "$DISP.0" \
    -thread_queue_size 1024 -probesize 32 -analyzeduration 0 -use_wallclock_as_timestamps 1 \
    -f pulse -fragment_size 1920 -sample_rate 48000 -channels 2 -i aichess.monitor \
    "${in_music[@]}" \
    -filter_complex "$af" -map 0:v -map "[aout]" \
    -c:v libx264 -preset "$PRESET" -tune zerolatency -pix_fmt yuv420p -r "$FPS" \
    -g "$gop" -keyint_min "$gop" -sc_threshold 0 \
    -b:v "${VB}k" -minrate "${VB}k" -maxrate "${VB}k" -bufsize "$((VB * 2))k" -x264-params nal-hrd=cbr \
    -c:a aac -b:a "$AB" -ar 48000 -ac 2 \
    -output_ts_offset "-$(date +%s)" -progress "$RUN/progress.fifo" \
    "${out_args[@]}" \
    2> >(YT_KEY="${YT_KEY:-}" python3 -u -c 'import os,sys
k=os.environ.get("YT_KEY") or "\0none\0"
for line in sys.stdin: sys.stdout.write(line.replace(k, "<key>"))' | sed -u 's/^/ffmpeg: /') &
  FF_PID=$!
  FF_STARTED=$(date +%s)
  log "ffmpeg pid $FF_PID ${W}x${H}@${FPS} ${PRESET} ${VB}k -> $([ "$OUTPUT" = rtmp ] && echo "$YT_URL/<key>" || echo "$OUTPUT") music=$([ -n "${in_music[*]}" ] && echo on || echo off)"
}

MUSIC_PID=""
start_music() {
  MUSIC_PID=""
  [ -n "$MUSIC" ] && [ -f "$MUSIC" ] || return 0
  # small buffer: a null sink renders in blocks of its client's latency; a 2 s default buffer makes the
  # monitor deliver 2 s chunks, the mix waits for them and ffmpeg's scheduler pauses the screen capture
  PULSE_LATENCY_MSEC=30 ffmpeg -nostdin -hide_banner -loglevel error -re -stream_loop -1 -i "$MUSIC" -vn \
    -ac 2 -ar 48000 -f pulse -buffer_duration 30 -device music "music bed" >>"$RUN/music.log" 2>&1 &
  MUSIC_PID=$!
}
start_music

BACKOFF=3
FF_RESTARTS=0
start_ffmpeg || true
LAST_CHECK=0

write_status() {
  printf '{"updated":"%s","url":"%s","size":"%s","fps":%s,"preset":"%s","vbitrate_k":%s,"output":"%s","music":%s,"xvfb_pid":%s,"pulse_pid":%s,"chrome_pid":%s,"chrome_mb":%s,"chrome_restarts":%s,"ffmpeg_pid":%s,"ffmpeg_restarts":%s,"seeded":%s}\n' \
    "$(date -Is)" "$URL" "$SIZE" "$FPS" "$PRESET" "$VB" "$([ "$OUTPUT" = rtmp ] && echo rtmp || echo "$OUTPUT")" \
    "$(alive "$MUSIC_PID" && echo true || echo false)" \
    "${XVFB_PID:-0}" "${PULSE_PID:-0}" "${CHROME_PID:-0}" "$(chrome_rss_mb)" "$CHROME_RESTARTS" "${FF_PID:-0}" "$FF_RESTARTS" "$SEEDED" \
    >"$RUN/status.json.tmp" && mv "$RUN/status.json.tmp" "$RUN/status.json"
}

# ---- 5. watchdog loop
while true; do
  sleep 5
  alive "$XVFB_PID" || { log "Xvfb died"; exit 1; }
  alive "$PULSE_PID" || { log "PulseAudio died"; exit 1; }

  if [ -n "$MUSIC_PID" ] && ! alive "$MUSIC_PID"; then
    log "music bed player exited; restarting it"; start_music
  fi

  if ! alive "$CHROME_PID"; then
    log "chrome exited; restarting"
    stop_chrome; CHROME_RESTARTS=$((CHROME_RESTARTS + 1)); start_chrome
  fi

  if ! alive "$FF_PID"; then
    if [ -n "$FF_PID" ]; then
      wait "$FF_PID" 2>/dev/null; rc=$?
      ran=$(( $(date +%s) - ${FF_STARTED:-0} ))
      [ "$ran" -gt 120 ] && BACKOFF=3
      log "ffmpeg exited (code $rc after ${ran}s); restarting in ${BACKOFF}s"
    fi
    sleep "$BACKOFF"; BACKOFF=$(( BACKOFF * 2 > 60 ? 60 : BACKOFF * 2 ))
    FF_RESTARTS=$((FF_RESTARTS + 1))
    start_ffmpeg || true
  fi

  now=$(date +%s)
  if [ $(( now - LAST_CHECK )) -ge 15 ]; then
    LAST_CHECK=$now
    info="$(python3 "$HERE/cdp.py" --port "$CDP_PORT" info 2>/dev/null || true)"
    if [ "$SEEDED" = 0 ] && echo "$info" | grep -q '"url": "http'; then
      if python3 "$HERE/cdp.py" --port "$CDP_PORT" seed "$LOCALSTORAGE" >/dev/null 2>&1; then SEEDED=1; log "viewer settings seeded: $LOCALSTORAGE"; fi
    fi
    if echo "$info" | grep -q 'chrome-error://' && curl -sf -o /dev/null --max-time 3 "$URL"; then
      log "chrome shows an error page and the viewer is up; reloading"
      python3 "$HERE/cdp.py" --port "$CDP_PORT" reload >/dev/null 2>&1 || true
    fi
    mb="$(chrome_rss_mb)"
    if [ "$mb" -gt "$CHROME_MAX_MB" ]; then
      log "chrome uses ${mb} MB (> ${CHROME_MAX_MB}); restarting it"
      stop_chrome; CHROME_RESTARTS=$((CHROME_RESTARTS + 1)); start_chrome
    fi
    write_status
  fi
done
