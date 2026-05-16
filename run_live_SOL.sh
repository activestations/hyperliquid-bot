#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
exec ./run_live_supervised.sh SOL \
  --coin SOL --size 1.0 --mode grid --lookback 48 --fast-lookback 8 --slow-lookback 32 \
  --entry-bps 18 --exit-bps 12 --trend-entry-bps 14 --trend-exit-bps 5 --maker-offset-bps 4 \
  --min-vol-bps 1 --max-vol-bps 140 --grid-levels 3 --grid-spacing-bps 32 \
  --grid-take-profit-bps 28 --grid-stop-bps 80 --inventory-skew-bps 5 \
  --iterations 0 --sleep-seconds 30 --order-live-seconds 0 \
  --log trades_SOL.jsonl --state daemon_state_SOL.json
