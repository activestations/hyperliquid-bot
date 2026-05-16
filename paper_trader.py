#!/usr/bin/env python3
"""
Paper trading daemon — connects to Hyperliquid mainnet for live price data,
runs the same AdaptiveGridStrategy (v3) with the real configured params,
but NEVER places real orders. All fills are simulated at mid price,
PnL tracked in local logs.

Usage:
  python3 paper_trader.py --coin BTC                  # read config.vhl.yaml → coin BTC params
  python3 paper_trader.py --coin ETH --config path/to/config.yaml
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── 从 hlbot 包复用策略 ──────────────────────────────────
BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
from hlbot.strategy_v3 import AdaptiveGridStrategy, AdaptiveGridConfig
import yaml

# ── 费用假设 (保守) ──────────────────────────────────────
FEE_BPS = 1.0


def _load_coin_config(coin: str, config_path: str) -> dict[str, Any]:
    """Load per-coin strategy params from YAML, merging defaults + per-coin overrides."""
    path = Path(config_path)
    if not path.is_absolute():
        path = (BASE / path).resolve()
    data = yaml.safe_load(path.read_text()) or {}
    defaults = data.get("default", {}) or {}
    coins = data.get("coins", {}) or {}
    coin_key = coin.upper()
    overrides = coins.get(coin_key, {}) or {}
    merged = {**defaults, **overrides}
    return merged


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="paper_trader", description="Mainnet paper trading simulator")
    p.add_argument("--config", default="config.vhl.yaml", help="VHL config YAML path")
    p.add_argument("--coin", default="ETH", help="Coin symbol (BTC / ETH / SOL)")
    p.add_argument("--sleep-seconds", type=float, default=10.0, help="Seconds between iterations")
    p.add_argument("--log", default="", help="Override log file path (default: paper_{COIN}.jsonl)")
    p.add_argument("--state", default="", help="Override state file path (default: paper_state_{COIN}.json)")
    return p.parse_args()


def _fmt_price(price: float, coin: str) -> float:
    if coin == "BTC":
        return round(price, 1)
    elif coin == "SOL":
        return round(price, 3)
    return round(price, 2)


class PaperTrader:
    """Virtual portfolio executing AdaptiveGridStrategy decisions on mainnet mids."""

    def __init__(self, coin: str, params: dict[str, Any], sleep_seconds: float, log_path: str, state_path: str):
        self.coin = coin.upper()
        self.sleep_seconds = sleep_seconds

        cfg = AdaptiveGridConfig(
            coin=self.coin,
            base_size=float(params["base_size"]),
            lookback=int(params.get("lookback", 48)),
            entry_bps=float(params["entry_bps"]),
            grid_levels=int(params["grid_levels"]),
            grid_spacing_bps=float(params["grid_spacing_bps"]),
            take_profit_bps=float(params["take_profit_bps"]),
            stop_loss_bps=float(params["stop_loss_bps"]),
            partial_tp_ratio=float(params.get("partial_tp_ratio", 0.6)),
            extended_tp_multiplier=float(params.get("extended_tp_multiplier", 1.5)),
            volatility_scaling=bool(params.get("volatility_scaling", True)),
            vol_baseline_bps=float(params.get("vol_baseline_bps", 8.0)),
            vol_sensitivity=float(params.get("vol_sensitivity", 0.3)),
            min_vol_bps=float(params.get("min_vol_bps", 0.5)),
            max_vol_bps=float(params.get("max_vol_bps", 100.0)),
            maker_offset_bps=float(params.get("maker_offset_bps", 2.0)),
            max_consecutive_losses=int(params.get("max_consecutive_losses", 3)),
            cooloff_rounds=int(params.get("cooloff_rounds", 30)),
            order_timeout_rounds=int(params.get("order_timeout_rounds", 12)),
            trend_block_bps=float(params.get("trend_block_bps", 0.5)),
            direction_penalty_threshold=int(params.get("direction_penalty_threshold", 2)),
            direction_suspend_threshold=int(params.get("direction_suspend_threshold", 5)),
        )
        self.config = cfg
        self.strategy = AdaptiveGridStrategy(cfg)

        self.balance = float(params.get("initial_balance", 1000.0))
        self.position = 0.0
        self.entry_price: float | None = None
        self.realized_pnl = 0.0
        self.total_fees = 0.0
        self.total_volume = 0.0
        self.mids: list[float] = []

        self.log_path = Path(log_path)
        self.state_path = Path(state_path)
        self._running = True

        from hyperliquid.info import Info
        self._info = Info("https://api.hyperliquid.xyz", skip_ws=True, timeout=10.0)

    def run(self):
        signal.signal(signal.SIGTERM, lambda *_: self._stop())
        signal.signal(signal.SIGINT, lambda *_: self._stop())

        log_dir = self.log_path.parent
        if log_dir:
            log_dir.mkdir(parents=True, exist_ok=True)

        self._write_state()
        self._log_line({"event": "paper_start", "ts": self._now(), "config": asdict(self.config), "initial_balance": self.balance})

        while self._running:
            try:
                mid, ts = self._fetch_mid()
                if mid is None or mid <= 0:
                    time.sleep(self.sleep_seconds)
                    continue
            except Exception as exc:
                self._log_line({"event": "fetch_error", "ts": self._now(), "error": str(exc)[:200]})
                time.sleep(self.sleep_seconds)
                continue

            self.mids.append(mid)
            try:
                self._sync_position()
            except Exception:
                pass

            try:
                decision = self.strategy.decide(mids=self.mids, position_size=self.position)
            except Exception as exc:
                self._log_line({"event": "decide_error", "ts": self._now(), "error": str(exc)[:300]})
                time.sleep(self.sleep_seconds)
                continue

            item: dict[str, Any] = {
                "ts": ts,
                "mid": mid,
                "position": self.position,
                "entry_price": self.entry_price,
                "balance": round(self.balance, 2),
                "realized_pnl": round(self.realized_pnl, 4),
                "unrealized_pnl": round(self._unrealized_pnl(mid), 4),
                "total_fees": round(self.total_fees, 4),
                "decision_side": decision.side,
                "decision_reason": decision.reason,
                "decision_mode": decision.mode,
                "decision_price": decision.price,
                "decision_size": decision.size,
            }

            if decision.side != "hold":
                try:
                    self._execute_decision(decision, mid, ts)
                except Exception as exc:
                    self._log_line({"event": "execute_error", "ts": self._now(), "error": str(exc)[:300], "decision": str(decision)})
                else:
                    item["after_position"] = self.position
                    item["after_balance"] = round(self.balance, 2)
                    item["after_entry_price"] = self.entry_price

            try:
                self._log_line(item)
                self._write_state()
            except Exception as exc:
                print(f"[CRITICAL] log/state write failed: {exc}")

            if len(self.mids) % 10 == 0:
                self._print_status(mid)

            time.sleep(self.sleep_seconds)

    # ── 虚拟执行 ────────────────────────────────────────

    def _execute_decision(self, decision: "StrategyDecision", mid: float, ts: str):
        from hlbot.strategy import StrategyDecision

        side = decision.side
        size = decision.size
        price = float(decision.price or mid)
        price = _fmt_price(price, self.coin)
        direction = self.strategy._direction or "?"

        if not decision.reduce_only:
            notional = price * size
            fee = notional * FEE_BPS / 10000
            self.total_fees += fee
            self.total_volume += notional
            self.balance -= fee

            if side == "buy":
                new_cost = (self.position * (self.entry_price or 0)) + (size * price)
                self.position += size
                self.entry_price = new_cost / self.position if self.position != 0 else price
            else:
                new_cost = (self.position * (self.entry_price or 0)) + (size * price)
                self.position -= size
                self.entry_price = new_cost / abs(self.position) if self.position != 0 else price

            entry_level = decision.reason.split("L")[-1].split("/")[0] if "L" in decision.reason else "0"
            self._log_line({
                "event": "paper_entry", "ts": ts,
                "side": side, "price": price, "size": size,
                "fee": round(fee, 6), "notional": round(notional, 4),
                "direction": direction, "entry_level": entry_level,
                "reason": decision.reason,
            })
        else:
            notional = price * size
            fee = notional * FEE_BPS / 10000
            self.total_fees += fee
            self.total_volume += notional

            if self.position > 0 and side == "sell":
                sell_amount = min(abs(self.position), size)
                pnl = (price - (self.entry_price or 0)) * sell_amount
                self.position -= sell_amount
            elif self.position < 0 and side == "buy":
                buy_amount = min(abs(self.position), size)
                pnl = ((self.entry_price or 0) - price) * buy_amount
                self.position += buy_amount
            else:
                pnl = 0.0
                sell_amount = 0.0

            self.realized_pnl += pnl
            self.balance += pnl - fee

            if abs(self.position) < 0.0001:
                self.position = 0.0
                self.entry_price = None

            level_no = decision.reason.split("L")[-1].split("/")[0] if "L" in decision.reason else "?"
            self._log_line({
                "event": "paper_exit", "ts": ts,
                "side": side, "price": price, "size": size,
                "fee": round(fee, 6), "notional": round(notional, 4),
                "pnl": round(pnl, 6), "realized_pnl": round(self.realized_pnl, 4),
                "direction": direction, "entry_level": level_no,
                "reason": decision.reason,
            })

    def _sync_position(self):
        self.strategy.sync_position(
            entry_px=self.entry_price,
            position_size=self.position,
        )

    def _unrealized_pnl(self, mid: float) -> float:
        if self.position == 0 or self.entry_price is None:
            return 0.0
        if self.position > 0:
            return (mid - self.entry_price) * self.position
        else:
            return (self.entry_price - mid) * abs(self.position)

    def _fetch_mid(self) -> tuple[float | None, str]:
        all_mids = self._info.all_mids()
        raw = all_mids.get(self.coin)
        if raw is None:
            return None, self._now()
        return float(raw), self._now()

    # ── 日志 ────────────────────────────────────────────

    def _log_line(self, data: dict[str, Any]):
        with open(self.log_path, "a") as f:
            f.write(json.dumps(data, ensure_ascii=False, default=str) + "\n")

    def _write_state(self):
        payload = {
            "coin": self.coin,
            "position": self.position,
            "entry_price": self.entry_price,
            "balance": round(self.balance, 4),
            "realized_pnl": round(self.realized_pnl, 4),
            "unrealized_pnl": round(self._unrealized_pnl(self.mids[-1] if self.mids else 0.0), 4),
            "total_fees": round(self.total_fees, 4),
            "total_volume": round(self.total_volume, 4),
            "mid": self.mids[-1] if self.mids else None,
            "mids_count": len(self.mids),
            "ts": self._now(),
            "config": asdict(self.config),
            "direction": self.strategy._direction,
            "cooloff_remaining": self.strategy._cooloff_remaining,
            "level_index": self.strategy._level_index,
            "long_losses": self.strategy._long_losses,
            "short_losses": self.strategy._short_losses,
        }
        tmp = self.state_path.with_name(f".{self.state_path.name}.tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
        os.replace(tmp, self.state_path)

    def _print_status(self, mid: float):
        unrealized = self._unrealized_pnl(mid)
        net = self.realized_pnl + unrealized
        direction = self.strategy._direction or "none"
        print(f"[{self._now()}] {self.coin} @ {mid:.2f} | "
              f"pos={self.position:.4f} entry={self.entry_price or '--'} | "
              f"balance={self.balance:.2f} rPnL={self.realized_pnl:.4f} uPnL={unrealized:.4f} "
              f"net={net:.4f} fees={self.total_fees:.4f} vol={self.total_volume:.1f} | "
              f"dir={direction} cooloff={self.strategy._cooloff_remaining}")

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _stop(self):
        self._running = False


def main():
    args = _parse_args()
    coin = args.coin.upper()

    # ── 从 YAML 加载参数 ──
    params = _load_coin_config(coin, args.config)
    log_path = args.log or f"paper_{coin}.jsonl"
    state_path = args.state or f"paper_state_{coin}.json"

    print(f"=== VHL {coin} ===")
    print(f"Config: {Path(args.config).resolve()}")
    print(f"Log:    {Path(log_path).resolve()}")
    print(f"Params: entry_bps={params['entry_bps']} levels={params['grid_levels']} "
          f"spacing={params['grid_spacing_bps']} tp={params['take_profit_bps']} sl={params['stop_loss_bps']}")
    print(f"Initial balance: ${params.get('initial_balance', 1000)}")
    print("=" * 40)

    trader = PaperTrader(coin, params, args.sleep_seconds, log_path, state_path)
    try:
        trader.run()
    except KeyboardInterrupt:
        pass
    finally:
        mid = trader.mids[-1] if trader.mids else 0
        unrealized = trader._unrealized_pnl(mid)
        print(f"\n=== Final ===")
        print(f"Balance:  ${trader.balance:.2f}")
        print(f"Position: {trader.position:.6f} {coin} @ ${trader.entry_price or '--'}")
        print(f"Realized PnL:    ${trader.realized_pnl:.4f}")
        print(f"Unrealized PnL: ${unrealized:.4f}")
        print(f"Total fees:     ${trader.total_fees:.4f}")
        print(f"Total volume:   ${trader.total_volume:.1f}")


if __name__ == "__main__":
    main()
