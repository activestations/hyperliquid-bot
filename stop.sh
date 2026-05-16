#!/usr/bin/env bash
# Stop script for hlbot
# Kills all daemon/supervised processes. Does NOT write .stop sentinels
# (that's handled by the /stop API for kill-switch use).
set -euo pipefail

cd "$(dirname "$0")"

# Kill by PID files
for f in pids/*.pid; do
  [ -f "$f" ] || continue
  pid=$(cat "$f")
  kill "$pid" 2>/dev/null || true
done

# Fallback: kill any remaining hlbot daemon processes
pgrep -af "hlbot.cli.*daemon" 2>/dev/null | while read -r line; do
  pid=$(echo "$line" | awk '{print $1}')
  [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
done

# Kill supervised scripts (the while-true wrappers)
pgrep -af "run_live_supervised" 2>/dev/null | while read -r line; do
  pid=$(echo "$line" | awk '{print $1}')
  [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
done

echo "[stop.sh] All hlbot processes stopped" >&2
