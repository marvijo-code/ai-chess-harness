#!/usr/bin/env bash
# Arm the round recorder in its own tmux session (runs on the VPS).
# Usage: arm.sh <run_dir> <round> <slug|-> <tunnel_port> <display> <max_min> <start_mode 0=normal|1=allow-late|2=wait-change> <session>
set -euo pipefail
base=$(cd "$(dirname "$0")/.." && pwd)
run=$1 round=$2 slug=$3 port=$4 disp=$5 maxmin=$6 late=$7 sess=$8
cd "$base"
if tmux has-session -t "$sess" 2>/dev/null; then echo "BUSY: tmux session $sess already exists"; exit 3; fi
if [ -e "$run/raw.mkv" ] || [ -e "$run/REC_DONE" ]; then echo "EXISTS: $run already holds a take; pick another run name"; exit 4; fi
code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$port/api/viewer-version" || true)
[ "$code" = "200" ] || { echo "TUNNEL_DOWN: viewer not reachable on VPS port $port (http $code)"; exit 6; }
mkdir -p "$run"
extra=""; [ "$late" = "1" ] && extra="--allow-late"; [ "$late" = "2" ] && extra="--wait-change"
slugarg=""; [ "$slug" != "-" ] && slugarg="--slug $slug"
tmux new-session -d -s "$sess" "cd '$base' && python3 bin/rec.py record '$run' --round $round $slugarg --port $port --display $disp --max-min $maxmin $extra > '$run/rec.log' 2>&1; echo \$? > '$run/REC_DONE'"
sleep 12
cat "$run/rec.log" 2>/dev/null || true
if [ -f "$run/REC_DONE" ]; then echo "ARM_FAILED rc=$(cat "$run/REC_DONE")"; exit 7; fi
echo "ARMED $run"
