#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p pids
for coin in BTC ETH SOL; do
  script="./run_paper_${coin}.sh"
  nohup "$script" > "paper_${coin}_daemon.out" 2>&1 &
  echo $! > "pids/paper_${coin}.pid"
done
printf 'started BTC/ETH/SOL paper daemon\n'
