# Hyperliquid Bot

Safe-by-default Hyperliquid automation for OpenClaw.

Defaults:
- testnet
- dry-run enabled
- small notional limits
- coin whitelist

## Setup

```bash
cd hyperliquid_bot
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
cp config.example.yaml config.yaml
```

Fill `.env` and `config.yaml`, then run:

```bash
python -m hlbot.cli status
python -m hlbot.cli mids --coins BTC ETH SOL
python -m hlbot.cli account
python -m hlbot.cli spot-account
python -m hlbot.cli plan-order --coin BTC --side buy --size 0.001 --price 50000
python -m hlbot.cli smoke-test --transfer-amount 0 --price 2200
```

## Commands

Read-only:

```bash
python -m hlbot.cli status
python -m hlbot.cli mids --coins BTC ETH SOL
python -m hlbot.cli meta --coins-only
python -m hlbot.cli account
python -m hlbot.cli spot-account
python -m hlbot.cli open-orders
```

Safe trading helpers:

```bash
# Dry-run order plan; never sends
python -m hlbot.cli plan-order --coin ETH --side buy --size 0.005 --price 2200

# Transfer USDC between spot and perp; sends only when dry_run=false
python -m hlbot.cli usd-class-transfer --amount 15 --to perp

# Testnet-only end-to-end check: balance -> optional transfer -> ALO order -> cancel -> final state
python -m hlbot.cli smoke-test --transfer-amount 0 --price 2200
```

For live testnet actions, create a temporary config with `dry_run: false`; keep `config.yaml` dry by default.

Live exchange actions require `dry_run: false` and valid `HL_PRIVATE_KEY`.

## Long-running automation

Recommended flow:

1. Tune parameters:

```bash
python -m hlbot.cli optimize --coin ETH --hours 168 --interval 15m --top 5
```

2. Run dry-run daemon:

```bash
python -m hlbot.cli daemon --iterations 0 --sleep-seconds 60 --mode hybrid
```

3. Run testnet live only after you confirm the report and want real execution:

```bash
python -m hlbot.cli daemon --iterations 0 --sleep-seconds 60 --mode hybrid
```

Set `dry_run: false` in a temporary config only.
