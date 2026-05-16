#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p pids
for coin in BTC ETH SOL; do
  script="./run_live_${coin}.sh"
  nohup "$script" > "daemon_${coin}.out" 2>&1 &
  echo $! > "pids/${coin}.pid"
done
printf 'started ETH/BTC/SOL daemons (V1)\n'
