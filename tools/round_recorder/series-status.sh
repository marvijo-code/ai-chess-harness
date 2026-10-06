#!/usr/bin/env bash
# Per-round state of a series (runs on the VPS). Usage: series-status.sh <run_dir>
base=$(cd "$(dirname "$0")/.." && pwd)
run=$1
cd "$base"
for s in aclrec aclmix; do
  tmux has-session -t "$s" 2>/dev/null && echo "tmux $s: running" || echo "tmux $s: not running"
done
[ -f "$run/SERIES_DONE" ] && echo "SERIES_DONE rc=$(cat "$run/SERIES_DONE")"
python3 - "$run" <<'PY'
import json, os, sys
run = sys.argv[1]
try:
    rows = json.load(open(os.path.join(run, "rounds.json")))
except Exception:
    rows = []
    print("no rounds.json yet (waiting for the first round)")
def size(p):
    p = os.path.join(run, p)
    return f"{os.path.getsize(p) / 1e6:.0f}MB" if os.path.exists(p) else "-"
def cat(p):
    p = os.path.join(run, p)
    return open(p).read().strip() if os.path.exists(p) else None
for e in rows:
    k = str(e.get("key", e["round"]))
    dur = (e["end_epoch"] - e["start_epoch"]) if e.get("end_epoch") else None
    rec = cat(f"R{k}_REC_DONE")
    mixed = cat(f"R{k}_MIX_DONE")
    # The newest DRAFT that has a SHA-256 file (a remix writes DRAFT002, ...); else DRAFT001.
    done = sorted(f[:-7] for f in os.listdir(run)
                  if f.startswith(f"round{k}-live-DRAFT") and f.endswith(".mkv.sha256"))
    draft = done[-1] if done else f"round{k}-live-DRAFT001.mkv"
    sha = cat(draft + ".sha256")
    print(f"ROUND key={k} recorded={'rc=' + rec if rec is not None else 'recording'} "
          f"mixed={'rc=' + mixed if mixed is not None else 'no'} raw={size(e['raw'])} draft={size(draft)} "
          f"take_s={dur and round(dur)} reason={e.get('stop_reason')} sha256={sha or '-'} name={draft}")
PY
echo "--- rec.log"; tail -6 "$run/rec.log" 2>/dev/null
echo "--- mixer.log"; tail -4 "$run/mixer.log" 2>/dev/null
for f in $(ls -t "$run"/ffmpeg-rec-r*.log 2>/dev/null | head -1); do
  echo "--- $(basename "$f")"; tail -c 300 "$f" | tr '\r' '\n' | grep frame= | tail -1
done
echo "--- disk"; df -h "$run" | tail -1
exit 0
