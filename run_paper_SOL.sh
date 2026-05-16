#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
exec .venv/bin/python3 paper_trader.py --config config.vhl.yaml --coin SOL --sleep-seconds 10
