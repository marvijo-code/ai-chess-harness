#!/bin/sh
# Restart loop for the AI-chess relay (tools/aichess_relay.py) on the VPS.
# deploy.ps1 copies this file to ~/acl-chess-relay/ and starts it in tmux session "aclrelay".
DIR="$HOME/acl-chess-relay"
cd "$DIR" || exit 1
while true; do
  python3 "$DIR/aichess_relay.py" \
    --data-dir "$DIR/data" \
    --token-file "$DIR/ingest.token" \
    --public-port "${ACL_RELAY_PUBLIC_PORT:-8780}" \
    --ingest-port "${ACL_RELAY_INGEST_PORT:-8781}" \
    --log-file "$DIR/relay.log" 2>> "$DIR/relay.err"
  echo "$(date '+%F %T') relay exited with code $?, restarting in 3 s" >> "$DIR/relay.log"
  sleep 3
done
