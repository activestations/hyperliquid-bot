#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
exec ./run_live_supervised.sh SOL \
  --mode adaptive_grid \
  --coin SOL --size 6 \
  --entry-bps 12 --grid-levels 2 --grid-spacing-bps 22 \
  --take-profit-bps 20 --stop-loss-bps 32 \
  --maker-offset-bps 4 \
  --min-vol-bps 1 --max-vol-bps 140 \
  --max-consecutive-losses 2 --cooloff-rounds 45 --order-timeout-rounds 8 \
  --trend-block-bps 0.25 \
  --log daemon_SOL.out --trade-log trades_SOL.jsonl --state daemon_state_SOL.json
