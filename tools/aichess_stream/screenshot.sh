#!/usr/bin/env bash
# One PNG of exactly what the stream sends: a single x11grab frame of the stream's X display.
#   screenshot.sh [out.png] [display] [WxH]
# Defaults: ~/ai-chess-stream-frame.png, AICHESS_STREAM_DISPLAY (:87), AICHESS_STREAM_SIZE (1920x1080).
set -eu
OUT="${1:-$HOME/ai-chess-stream-frame.png}"
DISP="${2:-${AICHESS_STREAM_DISPLAY:-:87}}"
SIZE="${3:-${AICHESS_STREAM_SIZE:-1920x1080}}"
ffmpeg -hide_banner -loglevel error -y -f x11grab -draw_mouse 0 -video_size "$SIZE" -i "$DISP.0" -frames:v 1 "$OUT"
echo "$OUT"
