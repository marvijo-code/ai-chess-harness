#!/usr/bin/env bash
# Show the state of a take (runs on the VPS). Usage: status.sh <run_dir> <session>
base=$(cd "$(dirname "$0")/.." && pwd)
run=$1 sess=$2
cd "$base"
tmux has-session -t "$sess" 2>/dev/null && echo "tmux $sess: running" || echo "tmux $sess: not running"
echo "--- rec.log"; tail -6 "$run/rec.log" 2>/dev/null
[ -f "$run/REC_DONE" ] && echo "REC_DONE rc=$(cat "$run/REC_DONE")"
[ -f "$run/MIX_DONE" ] && echo "MIX_DONE rc=$(cat "$run/MIX_DONE")"
[ -f "$run/ffmpeg-rec.log" ] && tail -c 300 "$run/ffmpeg-rec.log" | tr '\r' '\n' | grep frame= | tail -1
exit 0
