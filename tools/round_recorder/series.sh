#!/usr/bin/env bash
# Arm the series recorder (tmux aclrec) and the per-round mixer (tmux aclmix) on the VPS.
# Usage: series.sh <run_dir> <from_round> <slug|-> <tunnel_port> <display> <max_min> <wait_change 0|1>
set -euo pipefail
base=$(cd "$(dirname "$0")/.." && pwd)
run=$1 from=$2 slug=$3 port=$4 disp=$5 maxmin=$6 wait=$7
rec=aclrec mixs=aclmix
cd "$base"
for s in $rec $mixs; do
  if tmux has-session -t "$s" 2>/dev/null; then echo "BUSY: tmux session $s already exists"; exit 3; fi
done
if [ -e "$run/rounds.json" ] || [ -e "$run/SERIES_DONE" ]; then echo "EXISTS: $run already holds a series; pick another run dir"; exit 4; fi
code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$port/api/viewer-version" || true)
[ "$code" = "200" ] || { echo "TUNNEL_DOWN: viewer not reachable on VPS port $port (http $code)"; exit 6; }
mkdir -p "$run"
extra=""; [ "$wait" = "1" ] && extra="--wait-change"
slugarg=""; [ "$slug" != "-" ] && slugarg="--slug $slug"
tmux new-session -d -s $rec "cd '$base' && python3 bin/rec.py series '$run' --from-round $from $slugarg --port $port --display $disp --max-min $maxmin $extra > '$run/rec.log' 2>&1; echo \$? > '$run/SERIES_DONE'"
tmux new-session -d -s $mixs "cd '$base' && bash bin/mixer.sh '$run' $port > '$run/mixer.log' 2>&1"
sleep 12
cat "$run/rec.log" 2>/dev/null || true
if [ -f "$run/SERIES_DONE" ]; then
  echo "ARM_FAILED rc=$(cat "$run/SERIES_DONE")"
  touch "$run/STOP"                        # lets the mixer we just started exit on its own
  exit 7
fi
echo "ARMED series $run from round $from (tmux $rec + $mixs)"
