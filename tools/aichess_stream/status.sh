#!/usr/bin/env bash
# One-screen status of the AI-chess VPS services: units, ports, relay, stream health, CPU and memory.
#   bash ~/.local/lib/ai-chess-stream/status.sh        (or tools/aichess_stream/status.sh in the repo)
# Prints no secrets (the stream key is never read here).
set -u
RUN=/run/ai-chess-stream
[ -d "$RUN" ] || RUN="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/ai-chess-stream"

echo "== units"
for u in runner viewer pusher stream; do
  en="$(systemctl is-enabled "ai-chess-$u" 2>/dev/null | head -n1)"; ac="$(systemctl is-active "ai-chess-$u" 2>/dev/null | head -n1)"
  printf '  %-20s %-9s %-9s since %s\n' "ai-chess-$u" "${en:-missing}" "${ac:-unknown}" \
    "$(systemctl show -p ActiveEnterTimestamp --value "ai-chess-$u" 2>/dev/null)"
done
printf '  %-20s %s\n' "tmux aclrelay" "$(tmux has-session -t aclrelay 2>/dev/null && echo running || echo MISSING)"

echo "== ports"
ss -ltn 2>/dev/null | awk 'NR>1 {print $4}' | grep -E ':(8770|8780|8781|9223)$' | sed 's/^/  listening /'
curl -s -o /dev/null -w '  viewer 8770: HTTP %{http_code}\n' --max-time 3 http://127.0.0.1:8770/ 2>/dev/null || echo "  viewer 8770: down"
h="$(curl -s --max-time 3 http://127.0.0.1:8780/healthz 2>/dev/null)"
[ -n "$h" ] && echo "  relay 8780: $(echo "$h" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("state_age_s=%s tournament=%s clips=%s disk_free_mb=%s" % (d.get("state_age_s"), d.get("tournament_id"), d.get("clips"), d.get("disk_free_mb")))' 2>/dev/null)" || echo "  relay 8780: down"

echo "== stream"
if [ -f "$RUN/status.json" ]; then
  python3 - "$RUN/status.json" "$RUN/progress.txt" <<'EOF'
import json, sys, time, os
s = json.load(open(sys.argv[1]))
print(f"  {s['size']}@{s['fps']} {s['preset']} {s['vbitrate_k']}k -> {s['output']}  music={s['music']}  seeded={s['seeded']}")
print(f"  chrome {s['chrome_mb']} MB, restarts: chrome {s['chrome_restarts']}, ffmpeg {s['ffmpeg_restarts']}  (status {s['updated']})")
p = {}
try:
    for line in open(sys.argv[2]):
        k, _, v = line.strip().partition("=")
        p[k] = v
    age = time.time() - os.path.getmtime(sys.argv[2])
    print(f"  ffmpeg: fps={p.get('fps')} speed={p.get('speed')} out_time={p.get('out_time','')[:8]} "
          f"dup={p.get('dup_frames')} drop={p.get('drop_frames')} bytes={p.get('total_size')} (progress {age:.0f}s old)")
except OSError:
    print("  ffmpeg: no progress file")
EOF
else
  echo "  not running (no $RUN/status.json)"
fi

echo "== cpu (cores, 5 s sample) and memory (RSS MB)"
group() {  # name, pid list
  local name="$1"; shift; local pids="$*"
  [ -z "$pids" ] && { printf '  %-10s -\n' "$name"; return; }
  local a b rss
  a=$(for p in $pids; do awk '{print $14+$15}' "/proc/$p/stat" 2>/dev/null; done | awk '{s+=$1} END {print s+0}')
  echo "$name $a $pids"
}
snap() {
  group ffmpeg "$(pgrep -f 'aichess.monitor' | tr '\n' ' ')"
  group chrome "$(pgrep -f 'ai-chess-stream/chrome-profile' | tr '\n' ' ')"
  group xvfb "$(pgrep -f 'Xvfb :87' | tr '\n' ' ')"
  group viewer "$(pgrep -f 'llm_tournament_viewer.py' | tr '\n' ' ')"
  group stockfish "$(pgrep -f 'stockfish-linux' | tr '\n' ' ')"
  group claude "$(pgrep -x claude | tr '\n' ' ')"
  group codex "$(pgrep -f 'codex( |$)' | tr '\n' ' ')"
}
A="$(snap)"; sleep 5; B="$(snap)"
paste -d'|' <(echo "$A") <(echo "$B") | while IFS='|' read -r l r; do
  set -- $l; n=$1; t0=${2:-}; shift 2 2>/dev/null; pids="$*"
  set -- $r; t1=${2:-}
  [ -z "$t0" ] && { printf '  %-10s not running\n' "$n"; continue; }
  rss=$(ps -o rss= -p "$(echo $pids | tr ' ' ',')" 2>/dev/null | awk '{s+=$1} END {print int(s/1024)}')
  printf '  %-10s cores %.2f  rss %s MB  (%s procs)\n' "$n" "$(echo "$t0 $t1" | awk '{print ($2-$1)/100/5}')" "$rss" "$(echo $pids | wc -w)"
done
free -m | sed 's/^/  /'
df -h / | sed 's/^/  /'
uptime | sed 's/^/  /'
