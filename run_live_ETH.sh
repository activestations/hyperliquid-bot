#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
exec ./run_live_supervised.sh ETH \
  --coin ETH --size 0.07 --mode grid --lookback 48 --fast-lookback 8 --slow-lookback 32 \
  --entry-bps 12 --exit-bps 9 --trend-entry-bps 10 --trend-exit-bps 4 --maker-offset-bps 3 \
  --min-vol-bps 1 --max-vol-bps 100 --grid-levels 3 --grid-spacing-bps 22 \
  --grid-take-profit-bps 28 --grid-stop-bps 70 --inventory-skew-bps 4 \
  --iterations 0 --sleep-seconds 30 --order-live-seconds 0 \
  --log trades_ETH.jsonl --state daemon_state_ETH.json
