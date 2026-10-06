#!/usr/bin/env bash
# Mix every closed take of a series, in rounds.json order (runs on the VPS in tmux session aclmix).
# Waits for R<key>_REC_DONE, runs mix.py --round <key> under nice 19, writes R<key>_MIX_DONE (= exit code)
# and round<key>-live-DRAFT001.mkv.sha256. Exits when the recorder finished (SERIES_DONE) and every closed
# take is mixed, or when a STOP file appears in the run dir.
# Usage: mixer.sh <run_dir> [tunnel_port]
# The whole body is one function, parsed before it runs, so a redeploy of this file cannot break a live mixer.
main() {
set -uo pipefail
base=$(cd "$(dirname "$0")/.." && pwd)
run=$1 port=${2:-18770}
cd "$base"
log() { echo "$(date +%H:%M:%S) $*"; }
keys_of() {
  python3 -c 'import json,sys
try:
    print(" ".join(str(e.get("key", e["round"])) for e in json.load(open(sys.argv[1]))))
except Exception:
    pass' "$run/rounds.json" 2>/dev/null
}
log "mixer up for $run"
while true; do
  if [ -f "$run/STOP" ]; then log "STOP file: exiting"; exit 0; fi
  pending=""
  for k in $(keys_of); do
    [ -f "$run/R${k}_MIX_DONE" ] && continue
    [ -f "$run/R${k}_REC_DONE" ] && pending=$k
    break                                   # strictly in order: wait for this take
  done
  if [ -n "$pending" ]; then
    free=$(df --output=avail -B1 "$run" | tail -1)
    if [ "$free" -lt 2500000000 ]; then
      log "LOW_DISK: $((free / 1000000)) MB free; waiting before mixing round $pending (prune downloaded raws)"
      sleep 60; continue
    fi
    code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$port/api/viewer-version" || true)
    if [ "$code" != "200" ]; then
      log "TUNNEL_DOWN (http $code): clip audio unreachable; waiting before mixing round $pending"
      sleep 60; continue
    fi
    log "mixing round $pending"
    nice -n 19 python3 bin/mix.py "$run" --round "$pending" --port "$port" > "$run/mix-r$pending.log" 2>&1
    rc=$?
    out="$run/round${pending}-live-DRAFT001.mkv"
    if [ "$rc" = "0" ] && [ -f "$out" ]; then
      sha256sum "$out" | cut -d' ' -f1 > "$out.sha256"
    fi
    echo "$rc" > "$run/R${pending}_MIX_DONE"
    log "round $pending mixed rc=$rc"
    tail -3 "$run/mix-r$pending.log" | sed 's/^/    /'
    continue
  fi
  if [ -f "$run/SERIES_DONE" ]; then log "series over and every closed take mixed: exiting"; exit 0; fi
  sleep 30
done
}
main "$@"
exit $?
