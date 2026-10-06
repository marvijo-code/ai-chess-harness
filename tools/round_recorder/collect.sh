#!/usr/bin/env bash
# Mix, mux and verify a finished take in the tmux session, wait for it, print verify.txt (runs on the VPS).
# Usage: collect.sh <run_dir> <out_name> <mkv|mp4> <tunnel_port> <session>
set -uo pipefail
base=$(cd "$(dirname "$0")/.." && pwd)
run=$1 name=$2 fmt=$3 port=$4 sess=$5
cd "$base"
if [ ! -f "$run/REC_DONE" ]; then echo "NOT_DONE: the recorder is still running"; tail -3 "$run/rec.log" 2>/dev/null; exit 5; fi
if tmux has-session -t "$sess" 2>/dev/null; then echo "BUSY: tmux session $sess still exists"; exit 3; fi
rm -f "$run/MIX_DONE"
tmux new-session -d -s "$sess" "cd '$base' && python3 bin/mix.py '$run' '$name' --format $fmt --port $port > '$run/mix.log' 2>&1; echo \$? > '$run/MIX_DONE'"
for _ in $(seq 1 480); do [ -f "$run/MIX_DONE" ] && break; sleep 30; done
rc=$(cat "$run/MIX_DONE" 2>/dev/null || echo timeout)
cat "$run/verify.txt" 2>/dev/null
echo "MIX_RC=$rc"
if [ "$rc" != "0" ]; then tail -20 "$run/mix.log"; exit 8; fi
sha256sum "$run/$name"
stat -c 'SIZE=%s' "$run/$name"
