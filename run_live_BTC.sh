#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
exec ./run_live_supervised.sh BTC \
  --coin BTC --size 0.002 --mode grid --lookback 48 --fast-lookback 8 --slow-lookback 32 \
  --entry-bps 10 --exit-bps 8 --trend-entry-bps 8 --trend-exit-bps 4 --maker-offset-bps 4 \
  --min-vol-bps 0.5 --max-vol-bps 90 --grid-levels 3 --grid-spacing-bps 30 \
  --grid-take-profit-bps 40 --grid-stop-bps 100 --inventory-skew-bps 3 \
  --iterations 0 --sleep-seconds 30 --order-live-seconds 0 \
  --log trades_BTC.jsonl --state daemon_state_BTC.json
