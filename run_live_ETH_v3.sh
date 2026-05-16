#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
exec ./run_live_supervised.sh ETH \
  --mode adaptive_grid \
  --coin ETH --size 0.3 \
  --entry-bps 10 --grid-levels 2 --grid-spacing-bps 18 \
  --take-profit-bps 18 --stop-loss-bps 28 \
  --maker-offset-bps 3 \
  --min-vol-bps 1 --max-vol-bps 100 \
  --max-consecutive-losses 2 --cooloff-rounds 45 --order-timeout-rounds 8 \
  --trend-block-bps 0.25 \
  --log daemon_ETH.out --trade-log trades_ETH.jsonl --state daemon_state_ETH.json
