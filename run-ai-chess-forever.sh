#!/usr/bin/env bash
# Non-stop AI chess tournament on Linux: the runner (tools/ai_chess_forever.py) plus the viewer
# (tools/llm_tournament_viewer.py --follow out/live/current.json on port 8770).
#
#   ./run-ai-chess-forever.sh start        both in the background (setsid + nohup), PID files in out/live
#   ./run-ai-chess-forever.sh stop         stops ONLY the process groups named in our PID files
#   ./run-ai-chess-forever.sh restart
#   ./run-ai-chess-forever.sh status       PIDs, pointer, last log lines
#   ./run-ai-chess-forever.sh run-runner   foreground runner (for systemd: Type=simple)
#   ./run-ai-chess-forever.sh run-viewer   foreground viewer (for systemd: Type=simple)
#
# Settings (environment): AI_CHESS_CONFIG, AI_CHESS_ENV_FILE (default ~/.config/ai-chess/env, KEY=VALUE,
# mode 600), AI_CHESS_VIEWER_HOST (127.0.0.1), AI_CHESS_VIEWER_PORT (8770), AI_CHESS_ENGINE (Stockfish for the
# viewer's eval bar and move marks), AI_CHESS_COMMENTARY=1 (viewer --commentary), PYTHON (python3).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SELF="$ROOT/run-ai-chess-forever.sh"
LIVE="$ROOT/out/live"
CONFIG="${AI_CHESS_CONFIG:-$ROOT/configs/ai-chess-vps.json}"
ENV_FILE="${AI_CHESS_ENV_FILE:-$HOME/.config/ai-chess/env}"
VIEWER_HOST="${AI_CHESS_VIEWER_HOST:-127.0.0.1}"
VIEWER_PORT="${AI_CHESS_VIEWER_PORT:-8770}"
ENGINE="${AI_CHESS_ENGINE:-$HOME/sf19/stockfish/stockfish-linux-x86-64-universal}"
PY="${PYTHON:-python3}"
LOG_MAX_BYTES=$((50 * 1024 * 1024))

# A service or a non-login ssh shell has a short PATH: the CLIs live in ~/.local/bin (claude) and /usr/bin (codex).
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:${PATH:-}"
export PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8

load_env() {
  # API keys for the HTTP subscription routes (OPENCODE_GO_API_KEY, ZAI_API_KEY). Never printed.
  if [[ -r "$ENV_FILE" ]]; then
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
  fi
}

pid_file() { echo "$LIVE/forever-$1.pid"; }
log_file() { echo "$LIVE/forever-$1.log"; }

# True when the PID in our PID file is alive AND is the command this script started (never a reused PID).
ours() {
  local name="$1" pid
  pid="$(cat "$(pid_file "$name")" 2>/dev/null || true)"
  [[ -n "$pid" && -r "/proc/$pid/cmdline" ]] || return 1
  tr '\0' ' ' < "/proc/$pid/cmdline" | grep -q -- "run-ai-chess-forever.sh run-$name" || return 1
  echo "$pid"
}

rotate() {
  local file="$1"
  if [[ -f "$file" ]] && (( $(stat -c %s "$file") > LOG_MAX_BYTES )); then
    mv -f "$file" "$file.1"
  fi
}

start_one() {
  local name="$1" pid
  if pid="$(ours "$name")"; then
    echo "$name already running (pid $pid)"
    return 0
  fi
  mkdir -p "$LIVE"
  rotate "$(log_file "$name")"
  # setsid: the process leads its own group, so stop can end it and its engine children together.
  setsid nohup "$SELF" "run-$name" >> "$(log_file "$name")" 2>&1 < /dev/null &
  echo $! > "$(pid_file "$name")"
  echo "$name started (pid $!, log $(log_file "$name"))"
}

stop_one() {
  local name="$1" pid
  if ! pid="$(ours "$name")"; then
    echo "$name not running (no live process from our PID file)"
    rm -f "$(pid_file "$name")"
    return 0
  fi
  kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
  for _ in $(seq 1 20); do
    [[ -d "/proc/$pid" ]] || break
    sleep 0.5
  done
  if [[ -d "/proc/$pid" ]]; then
    kill -KILL -- "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
  fi
  rm -f "$(pid_file "$name")"
  echo "$name stopped (pid $pid)"
}

status() {
  local name pid
  for name in runner viewer; do
    if pid="$(ours "$name")"; then echo "$name: running (pid $pid)"; else echo "$name: stopped"; fi
  done
  if [[ -f "$LIVE/current.json" ]]; then echo "pointer: $(cat "$LIVE/current.json" | tr -d '\n')"; fi
  for name in runner viewer; do
    if [[ -f "$(log_file "$name")" ]]; then echo "--- $(log_file "$name")"; tail -n 8 "$(log_file "$name")"; fi
  done
}

case "${1:-}" in
  run-runner)
    load_env
    cd "$ROOT"
    exec "$PY" tools/ai_chess_forever.py --config "$CONFIG"
    ;;
  run-viewer)
    cd "$ROOT"
    args=(tools/llm_tournament_viewer.py --host "$VIEWER_HOST" --port "$VIEWER_PORT" --follow "$LIVE/current.json")
    if [[ -x "$ENGINE" ]]; then args+=(--engine "$ENGINE"); else args+=(--no-analysis); fi
    if [[ "${AI_CHESS_COMMENTARY:-0}" == "1" ]]; then load_env; args+=(--commentary); fi
    exec "$PY" "${args[@]}"
    ;;
  start) start_one runner; start_one viewer ;;
  stop) stop_one viewer; stop_one runner ;;
  restart) stop_one viewer; stop_one runner; start_one runner; start_one viewer ;;
  status) status ;;
  *) sed -n '2,20p' "$SELF"; exit 2 ;;
esac
