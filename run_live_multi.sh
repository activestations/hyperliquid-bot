#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p pids
for coin in BTC ETH SOL; do
  script="./run_live_${coin}_v3.sh"
  nohup "$script" > "daemon_${coin}.out" 2>&1 &
  echo $! > "pids/${coin}.pid"
done
printf 'started BTC/ETH/SOL v3 adaptive-grid daemons\n'
