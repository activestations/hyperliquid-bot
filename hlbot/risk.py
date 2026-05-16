from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import json
import os
from pathlib import Path
from typing import Any

from .config import AppConfig


@dataclass(frozen=True)
class RiskDecision:
    ok: bool
    reason: str = "ok"


class RiskManager:
    def __init__(self, config: AppConfig):
        self.config = config
        self.state_path = config.root / "state.json"
        self._state = self._load_state()

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"daily": {}}
        try:
            return json.loads(self.state_path.read_text())
        except json.JSONDecodeError:
            return {"daily": {}}

    def _atomic_write_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_name(f".{self.state_path.name}.tmp")
        tmp.write_text(json.dumps(self._state, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def save(self) -> None:
        self._atomic_write_state()

    def _today_state(self) -> dict[str, Any]:
        key = date.today().isoformat()
        daily = self._state.setdefault("daily", {})
        return daily.setdefault(key, {"orders": 0})

    def validate_live_enabled(self) -> RiskDecision:
        if self.config.dry_run:
            return RiskDecision(False, "dry_run is enabled; exchange actions will not be sent")
        if self.config.risk.require_testnet_for_live and self.config.environment != "testnet":
            return RiskDecision(False, "live mainnet trading is blocked by require_testnet_for_live")
        if not self.config.wallet.private_key:
            return RiskDecision(False, "HL_PRIVATE_KEY is missing")
        if not self.config.wallet.address:
            return RiskDecision(False, "HL_WALLET_ADDRESS or wallet.address is missing")
        return RiskDecision(True)

    def validate_order(
        self,
        *,
        coin: str,
        side: str,
        size: float,
        price: float,
        reduce_only: bool,
        current_position_size: float = 0.0,
    ) -> RiskDecision:
        coin = coin.upper()
        if coin not in self.config.risk.allowed_coins:
            return RiskDecision(False, f"coin {coin} is not in allowed_coins")
        if side not in {"buy", "sell"}:
            return RiskDecision(False, "side must be buy or sell")
        if size <= 0:
            return RiskDecision(False, "size must be positive")
        if price <= 0:
            return RiskDecision(False, "price must be positive")
        if self.config.risk.allow_reduce_only_only and not reduce_only:
            return RiskDecision(False, "risk.allow_reduce_only_only requires reduce_only=true")
        projected_position = current_position_size
        if side == "buy" and not reduce_only:
            projected_position += size
        elif side == "sell" and not reduce_only:
            projected_position -= size
        if abs(projected_position) > self.config.risk.max_position_size:
            return RiskDecision(
                False,
                f"projected position {projected_position:.8f} exceeds max_position_size {self.config.risk.max_position_size}",
            )
        projected_notional = abs(projected_position) * price
        if projected_notional > self.config.risk.max_position_notional_usd:
            return RiskDecision(
                False,
                f"projected position notional {projected_notional:.4f} exceeds max_position_notional_usd {self.config.risk.max_position_notional_usd}",
            )
        notional = size * price
        if notional > self.config.risk.max_order_notional_usd:
            return RiskDecision(
                False,
                f"order notional {notional:.4f} exceeds max_order_notional_usd {self.config.risk.max_order_notional_usd}",
            )
        today = self._today_state()
        if int(today.get("orders", 0)) >= self.config.risk.max_daily_orders:
            return RiskDecision(False, "max_daily_orders reached")
        return RiskDecision(True)

    def record_order(self) -> None:
        today = self._today_state()
        today["orders"] = int(today.get("orders", 0)) + 1
        self.save()
