from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
import os

import yaml
from dotenv import load_dotenv

Environment = Literal["testnet", "mainnet"]


@dataclass(frozen=True)
class WalletConfig:
    address: str = ""
    private_key: str = ""


@dataclass(frozen=True)
class RiskConfig:
    allowed_coins: list[str] = field(default_factory=lambda: ["BTC", "ETH", "SOL"])
    max_order_notional_usd: float = 20.0
    max_daily_orders: int = 20
    allow_reduce_only_only: bool = False
    require_testnet_for_live: bool = True
    max_position_size: float = 0.01
    max_position_notional_usd: float = 250.0
    max_drawdown_usd: float = 5.0
    max_spread_bps: float = 25.0
    emergency_cancel_on_error: bool = True


@dataclass(frozen=True)
class TradingConfig:
    default_tif: str = "Gtc"
    default_slippage_bps: int = 20


@dataclass(frozen=True)
class AppConfig:
    environment: Environment = "testnet"
    dry_run: bool = True
    wallet: WalletConfig = field(default_factory=WalletConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    trading: TradingConfig = field(default_factory=TradingConfig)
    root: Path = Path.cwd()

    @property
    def is_mainnet(self) -> bool:
        return self.environment == "mainnet"

    @property
    def base_url(self) -> str:
        if self.environment == "mainnet":
            return "https://api.hyperliquid.xyz"
        return "https://api.hyperliquid-testnet.xyz"

    @property
    def ws_url(self) -> str:
        if self.environment == "mainnet":
            return "wss://api.hyperliquid.xyz/ws"
        return "wss://api.hyperliquid-testnet.xyz/ws"


def _dict_get(data: dict[str, Any], key: str, default: Any) -> Any:
    value = data.get(key, default)
    return default if value is None else value


def load_config(path: str | Path = "config.yaml") -> AppConfig:
    root = Path.cwd()
    config_path = Path(path)
    if not config_path.is_absolute():
        config_path = root / config_path

    load_dotenv(root / ".env")

    data: dict[str, Any] = {}
    if config_path.exists():
        loaded = yaml.safe_load(config_path.read_text())
        if loaded:
            if not isinstance(loaded, dict):
                raise ValueError(f"Config must be a YAML mapping: {config_path}")
            data = loaded

    env = _dict_get(data, "environment", "testnet")
    if env not in {"testnet", "mainnet"}:
        raise ValueError("environment must be 'testnet' or 'mainnet'")

    wallet_data = data.get("wallet") or {}
    risk_data = data.get("risk") or {}
    trading_data = data.get("trading") or {}

    # Prefer environment-specific variables. This supports Hyperliquid API/
    # operation keys where the signing key differs from the main/unified trading
    # account whose balance/orders should be managed.
    prefix = "HL_MAINNET" if env == "mainnet" else "HL_TESTNET"
    wallet = WalletConfig(
        address=(
            wallet_data.get("address")
            or os.getenv(f"{prefix}_ACCOUNT_ADDRESS")
            or os.getenv("HL_WALLET_ADDRESS")
            or ""
        ).strip(),
        private_key=(os.getenv(f"{prefix}_PRIVATE_KEY") or os.getenv("HL_PRIVATE_KEY") or "").strip(),
    )
    risk = RiskConfig(
        allowed_coins=[str(c).upper() for c in risk_data.get("allowed_coins", ["BTC", "ETH", "SOL"])],
        max_order_notional_usd=float(risk_data.get("max_order_notional_usd", 20)),
        max_daily_orders=int(risk_data.get("max_daily_orders", 20)),
        allow_reduce_only_only=bool(risk_data.get("allow_reduce_only_only", False)),
        require_testnet_for_live=bool(risk_data.get("require_testnet_for_live", True)),
        max_position_size=float(risk_data.get("max_position_size", 0.01)),
        max_position_notional_usd=float(risk_data.get("max_position_notional_usd", 250)),
        max_drawdown_usd=float(risk_data.get("max_drawdown_usd", 5)),
        max_spread_bps=float(risk_data.get("max_spread_bps", 25)),
        emergency_cancel_on_error=bool(risk_data.get("emergency_cancel_on_error", True)),
    )
    trading = TradingConfig(
        default_tif=str(trading_data.get("default_tif", "Gtc")),
        default_slippage_bps=int(trading_data.get("default_slippage_bps", 20)),
    )

    return AppConfig(
        environment=env,
        dry_run=bool(data.get("dry_run", True)),
        wallet=wallet,
        risk=risk,
        trading=trading,
        root=root,
    )
