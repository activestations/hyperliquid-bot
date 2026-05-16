#!/usr/bin/env bash
# Stop paper daemons by PID file
cd "$(dirname "$0")" || exit 1
for pidfile in pids/paper_*.pid; do
  [ -f "$pidfile" ] || continue
  pid=$(cat "$pidfile" 2>/dev/null || true)
  coin=$(basename "$pidfile" .pid | sed 's/paper_//')
  if [ -n "$pid" ] && kill "$pid" 2>/dev/null; then
    echo "stopped paper $coin (pid $pid)"
  fi
  rm -f "$pidfile" 2>/dev/null
done
# Also kill any lingering paper_trader.py processes
pkill -f "paper_trader.py" 2>/dev/null || true
printf 'done\n'
