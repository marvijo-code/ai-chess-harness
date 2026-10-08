#!/usr/bin/env bash
# Install the AI-chess systemd units on the VPS (system units that run as the login user).
#
#   bash tools/aichess_stream/install-services.sh                 # copy stream scripts, write units, daemon-reload
#   bash tools/aichess_stream/install-services.sh --enable stream # also enable + start the named units
#   bash tools/aichess_stream/install-services.sh --enable all    # runner, viewer, pusher, stream
#   bash tools/aichess_stream/install-services.sh --disable all   # stop + disable (units stay installed)
#
# What it does (idempotent):
#   1. copies stream.sh, cdp.py, screenshot.sh, status.sh to ~/.local/lib/ai-chess-stream/ (the stream unit runs
#      from there, so it does not depend on which branch ~/ai-chess has checked out);
#   2. writes ai-chess-{runner,viewer,pusher,stream}.service to /etc/systemd/system (user and home filled in);
#   3. creates ~/.config/ai-chess/services.env and stream.env from the examples when missing (never overwrites);
#   4. systemctl daemon-reload. Without --enable nothing is enabled or started.
# It never touches other units, tmux sessions or processes (the relay tmux session "aclrelay" stays as it is).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
RUN_USER="${AICHESS_USER:-$(id -un)}"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
ALL=(runner viewer pusher stream)

ENABLE=(); DISABLE=()
while [ $# -gt 0 ]; do
  case "$1" in
    --enable) shift; [ "${1:-}" = all ] && ENABLE=("${ALL[@]}") || IFS=, read -r -a ENABLE <<<"${1:-}";;
    --disable) shift; [ "${1:-}" = all ] && DISABLE=("${ALL[@]}") || IFS=, read -r -a DISABLE <<<"${1:-}";;
    -h|--help) sed -n '2,15p' "$0"; exit 0;;
    *) echo "unknown argument $1" >&2; exit 2;;
  esac
  shift
done

echo "user $RUN_USER, home $RUN_HOME"
LIB="$RUN_HOME/.local/lib/ai-chess-stream"
CONF="$RUN_HOME/.config/ai-chess"
install -d -m 755 -o "$RUN_USER" -g "$RUN_USER" "$LIB" "$RUN_HOME/.local/share/ai-chess-stream" 2>/dev/null \
  || sudo install -d -m 755 -o "$RUN_USER" -g "$RUN_USER" "$LIB" "$RUN_HOME/.local/share/ai-chess-stream"
install -d -m 700 "$CONF" 2>/dev/null || sudo install -d -m 700 -o "$RUN_USER" -g "$RUN_USER" "$CONF"
for f in stream.sh cdp.py screenshot.sh status.sh; do
  sed 's/\r$//' "$HERE/$f" >"$LIB/$f.new" && chmod 755 "$LIB/$f.new" && mv "$LIB/$f.new" "$LIB/$f"
done
git -C "$HERE" rev-parse --short HEAD 2>/dev/null >"$LIB/VERSION" || echo unknown >"$LIB/VERSION"
echo "stream scripts -> $LIB (rev $(cat "$LIB/VERSION"))"

for ex in services.env stream.env; do
  if [ ! -f "$CONF/$ex" ]; then
    sed 's/\r$//' "$HERE/$ex.example" | sed "s#__HOME__#$RUN_HOME#g" >"$CONF/$ex"; chmod 600 "$CONF/$ex"
    echo "created $CONF/$ex"
  fi
done

for u in "${ALL[@]}"; do
  src="$HERE/systemd/ai-chess-$u.service"
  sed -e 's/\r$//' -e "s#__HOME__#$RUN_HOME#g" -e "s#__USER__#$RUN_USER#g" "$src" \
    | sudo tee "/etc/systemd/system/ai-chess-$u.service.new" >/dev/null
  sudo mv "/etc/systemd/system/ai-chess-$u.service.new" "/etc/systemd/system/ai-chess-$u.service"
  sudo chmod 644 "/etc/systemd/system/ai-chess-$u.service"
done
sudo systemctl daemon-reload
echo "units installed: ${ALL[*]/#/ai-chess-}"

for u in "${DISABLE[@]}"; do sudo systemctl disable --now "ai-chess-$u.service" && echo "disabled + stopped ai-chess-$u"; done
for u in "${ENABLE[@]}"; do sudo systemctl enable --now "ai-chess-$u.service" && echo "enabled + started ai-chess-$u"; done

for u in "${ALL[@]}"; do
  printf '  %-24s %-9s %s\n' "ai-chess-$u" "$(systemctl is-enabled "ai-chess-$u" 2>/dev/null || true)" "$(systemctl is-active "ai-chess-$u" 2>/dev/null || true)"
done
