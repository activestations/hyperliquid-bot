#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
exec ./run_live_supervised.sh BTC \
  --mode adaptive_grid \
  --coin BTC --size 0.008 \
  --entry-bps 10 --grid-levels 2 --grid-spacing-bps 20 \
  --take-profit-bps 20 --stop-loss-bps 30 \
  --maker-offset-bps 4 \
  --min-vol-bps 0.5 --max-vol-bps 90 \
  --max-consecutive-losses 2 --cooloff-rounds 45 --order-timeout-rounds 8 \
  --trend-block-bps 0.25 \
  --log daemon_BTC.out --trade-log trades_BTC.jsonl --state daemon_state_BTC.json
