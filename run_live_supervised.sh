#!/usr/bin/env bash
set -euo pipefail
coin="$1"
shift
cd "$(dirname "$0")"
SENTINEL=".stop.${coin}"

# Remove stale sentinel (from a remote kill) on cold start so the daemon actually runs
if [ -f "$SENTINEL" ]; then
  rm -f "$SENTINEL"
fi

while true; do
  # Check for kill-switch sentinel — if present, exit without restarting.
  if [ -f "$SENTINEL" ] || [ -f ".stop.ALL" ]; then
    echo "$(date -Is) ${coin} sentinel .stop found; exiting without restart" >&2
    exit 0
  fi

  set +e
  . .venv/bin/activate
  python -m hlbot.cli --config config.live-testnet.yaml daemon "$@"
  code=$?
  set -e
  echo "$(date -Is) ${coin} daemon exited with code ${code}; restarting in 20s" >&2
  sleep 20
done
