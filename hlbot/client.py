from __future__ import annotations

import socket
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Any

from eth_account import Account
from .config import AppConfig


@dataclass(frozen=True)
class OrderPlan:
    coin: str
    asset: int
    is_buy: bool
    price: str
    size: str
    reduce_only: bool
    tif: str
    cloid: str | None = None

    def to_wire(self) -> dict[str, Any]:
        order: dict[str, Any] = {
            "a": self.asset,
            "b": self.is_buy,
            "p": self.price,
            "s": self.size,
            "r": self.reduce_only,
            "t": {"limit": {"tif": self.tif}},
        }
        if self.cloid:
            order["c"] = self.cloid
        return order


# TCP keepalive tuning to prevent router NAT table exhaustion on consumer-grade routers
# (e.g., Xiaomi with ~4096 NAT entries). By default, the SDK's requests.Session() does
# not set TCP keepalive, causing idle connections to linger in the router's NAT table,
# eventually blocking new TCP connections while ICMP/ping still works.
def _tune_session_keepalive(session):
    """Patch a requests.Session's HTTPAdapter to send TCP keepalive probes."""
    from requests.adapters import HTTPAdapter

    class KeepaliveAdapter(HTTPAdapter):
        def init_poolmanager(self, *args, **kwargs):
            kwargs["socket_options"] = kwargs.get("socket_options", []) + [
                (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1),
                (socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 60),
                (socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10),
                (socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3),
            ]
            return super().init_poolmanager(*args, **kwargs)

    adapter = KeepaliveAdapter()
    session.mount("https://", adapter)
    session.mount("http://", adapter)


class HyperliquidClient:
    def __init__(self, config: AppConfig, timeout: float = 15.0):
        self.config = config
        self.timeout = timeout
        self._info = None
        self._exchange = None
        self._meta_cache: dict[str, Any] | None = None
        self._session_tuned = False

    def close(self) -> None:
        """Best-effort close cached HTTP sessions so a fresh client can rebuild state."""
        for obj in (self._info, self._exchange):
            session = getattr(obj, "session", None)
            if session is not None and hasattr(session, "close"):
                try:
                    session.close()
                except Exception:
                    pass
        self._info = None
        self._exchange = None
        self._meta_cache = None
        self._session_tuned = False

    @property
    def info(self):
        if self._info is None:
            from hyperliquid.info import Info

            self._info = Info(self.config.base_url, skip_ws=True, timeout=self.timeout)
        if not self._session_tuned:
            _tune_session_keepalive(self._info.session)
            self._session_tuned = True
        return self._info

    @property
    def exchange(self):
        if self._exchange is None:
            if not self.config.wallet.private_key:
                raise RuntimeError("HL_PRIVATE_KEY is required for exchange actions")
            from hyperliquid.exchange import Exchange

            account = Account.from_key(self.config.wallet.private_key)
            self._exchange = Exchange(
                account,
                self.config.base_url,
                account_address=self.config.wallet.address or None,
                timeout=self.timeout,
            )
        return self._exchange

    def all_mids(self) -> dict[str, str]:
        return self.info.all_mids()

    def meta(self) -> dict[str, Any]:
        if self._meta_cache is None:
            self._meta_cache = self.info.meta()
        return self._meta_cache

    def user_state(self, address: str | None = None) -> dict[str, Any]:
        user = (address or self.config.wallet.address).lower()
        if not user:
            raise RuntimeError("wallet address is required")
        return self.info.user_state(user)

    def spot_user_state(self, address: str | None = None) -> dict[str, Any]:
        user = (address or self.config.wallet.address).lower()
        if not user:
            raise RuntimeError("wallet address is required")
        return self.info.spot_user_state(user)

    def open_orders(self, address: str | None = None) -> list[dict[str, Any]]:
        user = (address or self.config.wallet.address).lower()
        if not user:
            raise RuntimeError("wallet address is required")
        return self.info.open_orders(user)

    def candles_snapshot(self, coin: str, interval: str, start_time: int, end_time: int) -> list[dict[str, Any]]:
        return self.info.candles_snapshot(coin.upper(), interval, start_time, end_time)

    def l2_snapshot(self, coin: str) -> dict[str, Any]:
        return self.info.l2_snapshot(coin.upper())

    def coin_to_asset(self, coin: str) -> int:
        coin = coin.upper()
        universe = self.meta().get("universe", [])
        for idx, item in enumerate(universe):
            if str(item.get("name", "")).upper() == coin:
                return idx
        raise ValueError(f"coin not found in perp universe: {coin}")

    def _format_price(self, price: float | str) -> str:
        """Format price for Hyperliquid tick/significant-figure constraints.

        Hyperliquid rejects prices such as 2348.92 for ETH because they exceed the
        allowed tick/significant-figure precision. This conservative formatter
        keeps at most 5 significant digits and rounds down, which is suitable for
        maker test orders.
        """
        d = Decimal(str(price))
        if d <= 0:
            raise ValueError("price must be positive")
        adjusted = d.adjusted()  # base-10 exponent of most significant digit
        places = max(0, 4 - adjusted)
        quantum = Decimal("1") if places == 0 else Decimal("1").scaleb(-places)
        return format(d.quantize(quantum, rounding=ROUND_DOWN), "f")

    def build_limit_order(
        self,
        *,
        coin: str,
        side: str,
        size: float | str,
        price: float | str,
        reduce_only: bool = False,
        tif: str | None = None,
        cloid: str | None = None,
    ) -> OrderPlan:
        asset = self.coin_to_asset(coin)
        order_tif = tif or self.config.trading.default_tif
        if order_tif not in {"Alo", "Ioc", "Gtc"}:
            raise ValueError("tif must be Alo, Ioc, or Gtc")
        return OrderPlan(
            coin=coin.upper(),
            asset=asset,
            is_buy=side == "buy",
            price=self._format_price(price),
            size=str(size),
            reduce_only=reduce_only,
            tif=order_tif,
            cloid=cloid,
        )

    def place_limit_order(self, plan: OrderPlan) -> Any:
        # SDK order signature: order(name, is_buy, sz, limit_px, order_type, reduce_only=False, cloid=None)
        return self.exchange.order(
            plan.coin,
            plan.is_buy,
            float(plan.size),
            float(plan.price),
            {"limit": {"tif": plan.tif}},
            reduce_only=plan.reduce_only,
            cloid=plan.cloid,
        )

    def cancel(self, coin: str, oid: int) -> Any:
        return self.exchange.cancel(coin.upper(), oid)

    def usd_class_transfer(self, amount: float, to_perp: bool) -> Any:
        return self.exchange.usd_class_transfer(amount, to_perp)

    def user_fills(self, address: str | None = None) -> list[dict[str, Any]]:
        """Fetch fill history from the exchange API (real trade data with closedPnl, fee, etc.)."""
        user = (address or self.config.wallet.address).lower()
        if not user:
            raise RuntimeError("wallet address is required")
        return self.info.user_fills(user)

    def cancel_all(self) -> list[Any]:
        orders = self.open_orders()
        results = []
        for order in orders:
            results.append(self.cancel(str(order["coin"]), int(order["oid"])))
        return results
