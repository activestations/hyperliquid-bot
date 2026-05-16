from __future__ import annotations

from dataclasses import dataclass
from statistics import mean, pstdev
from typing import Any, Literal


Side = Literal["buy", "sell", "hold"]
Mode = Literal["mean_reversion", "trend", "grid", "blocked"]


@dataclass(frozen=True)
class StrategyDecision:
    side: Side
    reason: str
    mode: Mode = "blocked"
    price: float | None = None
    size: float = 0.0
    reduce_only: bool = False


@dataclass(frozen=True)
class StrategyConfig:
    coin: str = "ETH"
    size: float = 0.005
    lookback: int = 20
    fast_lookback: int = 8
    slow_lookback: int = 32
    entry_bps: float = 8.0
    exit_bps: float = 3.0
    trend_entry_bps: float = 6.0
    trend_exit_bps: float = 2.0
    maker_offset_bps: float = 2.0
    min_vol_bps: float = 2.0
    max_vol_bps: float = 80.0
    mode: str = "hybrid"  # mean_reversion | trend | hybrid | grid
    grid_levels: int = 3
    grid_spacing_bps: float = 12.0
    grid_take_profit_bps: float = 10.0
    grid_stop_bps: float = 55.0
    inventory_skew_bps: float = 4.0


class HybridStrategy:
    """Conservative strategy collection for Hyperliquid testnet validation.

    The first version was intentionally simple to validate execution plumbing. This
    version adds a GitHub-inspired grid mode: range filter, volatility gate,
    maker-style ladder entries, inventory-aware exits, take-profit, and stop-loss.
    It still is not a profit guarantee; it is a safer forward-test framework.

    Enhancements since v1:
    - Adaptive grid levels / skew based on volatility (方案二)
    - Trend-follow on strong trend instead of blocking (方案一)
    """

    def __init__(self, config: StrategyConfig):
        self.config = config
        self._last_grid_anchor: float | None = None
        self._last_entry_price: float | None = None
        self._trend_entry_price: float | None = None

    def _required_mids(self) -> int:
        if self.config.mode == "mean_reversion":
            return self.config.lookback
        if self.config.mode == "trend":
            return max(self.config.fast_lookback, self.config.slow_lookback)
        if self.config.mode == "grid":
            return max(self.config.lookback, self.config.fast_lookback, self.config.slow_lookback)
        return max(self.config.lookback, self.config.slow_lookback, self.config.fast_lookback)

    def _market_stats(self, mids: list[float]) -> tuple[float, float, float, float, float, float]:
        window = mids[-self.config.lookback :]
        last = window[-1]
        avg = mean(window)
        returns_bps = [(window[i] / window[i - 1] - 1) * 10000 for i in range(1, len(window)) if window[i - 1] > 0]
        vol_bps = pstdev(returns_bps) if len(returns_bps) > 1 else 0.0
        fast = mean(mids[-self.config.fast_lookback :])
        slow = mean(mids[-self.config.slow_lookback :])
        trend_bps = (fast - slow) / slow * 10000 if slow > 0 else 0.0
        deviation_bps = (last - avg) / avg * 10000 if avg > 0 else 0.0
        return last, avg, vol_bps, trend_bps, deviation_bps, fast

    def decide(self, *, mids: list[float], position_size: float) -> StrategyDecision:
        need = self._required_mids()
        if len(mids) < need:
            return StrategyDecision("hold", f"need {need} mids, got {len(mids)}")
        last, avg, vol_bps, trend_bps, deviation_bps, _fast = self._market_stats(mids)
        if avg <= 0 or last <= 0:
            return StrategyDecision("hold", "invalid mid")

        if vol_bps < self.config.min_vol_bps:
            return StrategyDecision("hold", f"vol {vol_bps:.2f} bps below min_vol_bps")
        if vol_bps > self.config.max_vol_bps:
            return StrategyDecision("hold", f"vol {vol_bps:.2f} bps above max_vol_bps")

        modes = {self.config.mode}
        if self.config.mode == "hybrid":
            modes = {"trend", "mean_reversion"}

        if "grid" in modes:
            return self._decide_grid(last=last, avg=avg, vol_bps=vol_bps, trend_bps=trend_bps, deviation_bps=deviation_bps, position_size=position_size)

        if "trend" in modes:
            if position_size <= 0 and trend_bps >= self.config.trend_entry_bps:
                price = last * (1 - self.config.maker_offset_bps / 10000)
                return StrategyDecision("buy", f"trend fast>slow by {trend_bps:.2f} bps, vol {vol_bps:.2f}", "trend", round(price, 2), self.config.size)
            if position_size > 0 and trend_bps <= self.config.trend_exit_bps:
                price = last * (1 + self.config.maker_offset_bps / 10000)
                return StrategyDecision("sell", f"trend faded to {trend_bps:.2f} bps", "trend", round(price, 2), min(position_size, self.config.size), True)

        if "mean_reversion" in modes:
            if position_size <= 0 and deviation_bps <= -self.config.entry_bps:
                price = last * (1 - self.config.maker_offset_bps / 10000)
                return StrategyDecision("buy", f"mid {deviation_bps:.2f} bps below mean, vol {vol_bps:.2f}", "mean_reversion", round(price, 2), self.config.size)
            if position_size > 0 and deviation_bps >= self.config.exit_bps:
                price = last * (1 + self.config.maker_offset_bps / 10000)
                return StrategyDecision("sell", f"mid {deviation_bps:.2f} bps above mean", "mean_reversion", round(price, 2), min(position_size, self.config.size), True)

        return StrategyDecision("hold", f"no signal: dev {deviation_bps:.2f} bps, trend {trend_bps:.2f} bps, vol {vol_bps:.2f}")

    def _decide_grid(self, *, last: float, avg: float, vol_bps: float, trend_bps: float, deviation_bps: float, position_size: float) -> StrategyDecision:
        spacing = max(self.config.grid_spacing_bps, vol_bps * 1.25, 1.0)
        tp = max(self.config.grid_take_profit_bps, spacing * 0.75)
        stop = max(self.config.grid_stop_bps, spacing * 3.0)

        # 方案二: 动态网格层数和 skew — 波动率高时减少层数、放宽入场
        adaptive_levels = max(1, min(self.config.grid_levels, int(self.config.grid_levels * self.config.grid_spacing_bps / max(spacing, 0.1))))
        adaptive_skew = self.config.inventory_skew_bps * (spacing / max(self.config.grid_spacing_bps, 0.1))

        anchor = self._last_grid_anchor or avg
        anchor_move_bps = abs(last - anchor) / anchor * 10000 if anchor > 0 else 0.0
        if self._last_grid_anchor is None or anchor_move_bps >= spacing:
            self._last_grid_anchor = avg
            anchor = avg

        # 方案一: 强趋势时趋势跟随（取代原来的 trend block）
        if position_size <= 0 and abs(trend_bps) > self.config.trend_entry_bps * 2.5:
            side = "buy" if trend_bps > 0 else "sell"
            if side == "buy":
                maker_px = last * (1 - self.config.maker_offset_bps / 10000)
            else:
                maker_px = last * (1 + self.config.maker_offset_bps / 10000)
            self._trend_entry_price = maker_px
            return StrategyDecision(side, f"trend follow {trend_bps:.1f} bps", "grid", round(maker_px, 2), self.config.size)

        # 趋势持仓管理: 持有或退出
        if self._trend_entry_price is not None and position_size != 0:
            ref = self._trend_entry_price
            pnl_bps = (last - ref) / ref * 10000 if ref > 0 else 0
            # 趋势减弱 → 退出
            if abs(trend_bps) < self.config.trend_exit_bps:
                exit_side = "sell" if position_size > 0 else "buy"
                if exit_side == "sell":
                    exit_px = last * (1 + self.config.maker_offset_bps / 10000)
                else:
                    exit_px = last * (1 - self.config.maker_offset_bps / 10000)
                self._trend_entry_price = None
                return StrategyDecision(exit_side, f"trend exit trend faded to {trend_bps:.1f} bps", "grid", round(exit_px, 2), min(abs(position_size), self.config.size), True)
            # 趋势止损
            if pnl_bps <= -(stop * 2):
                exit_side = "sell" if position_size > 0 else "buy"
                if exit_side == "sell":
                    exit_px = last * (1 + self.config.maker_offset_bps / 10000)
                else:
                    exit_px = last * (1 - self.config.maker_offset_bps / 10000)
                self._trend_entry_price = None
                return StrategyDecision(exit_side, f"trend stop pnl {pnl_bps:.2f} bps <= -{stop*2:.0f} bps", "grid", round(exit_px, 2), min(abs(position_size), self.config.size), True)
            return StrategyDecision("hold", f"trend holding {trend_bps:.1f} bps, pnl {pnl_bps:.1f} bps", "grid")

        # 清理过期的趋势状态（仓位在外部被关闭后）
        self._trend_entry_price = None

        # 正常网格逻辑（使用 adaptive_levels 和 adaptive_skew）
        if position_size > 0:
            ref = self._last_entry_price or anchor
            pnl_bps = (last - ref) / ref * 10000 if ref > 0 else deviation_bps
            if pnl_bps >= tp or (pnl_bps > 0 and deviation_bps >= self.config.exit_bps):
                price = last * (1 + self.config.maker_offset_bps / 10000)
                return StrategyDecision("sell", f"grid take-profit pnl {pnl_bps:.2f} bps / dev {deviation_bps:.2f} bps", "grid", round(price, 2), min(position_size, self.config.size), True)
            if pnl_bps <= -stop:
                price = last * (1 - self.config.maker_offset_bps / 10000)
                return StrategyDecision("sell", f"grid stop pnl {pnl_bps:.2f} bps <= -{stop:.2f} bps", "grid", round(price, 2), min(position_size, self.config.size), True)
            return StrategyDecision("hold", f"grid holding pos pnl {pnl_bps:.2f} bps, dev {deviation_bps:.2f} bps", "grid")

        for level in range(1, adaptive_levels + 1):
            trigger = -(spacing * level + adaptive_skew)
            if deviation_bps <= trigger:
                px = min(last, anchor * (1 + trigger / 10000))
                px *= 1 - self.config.maker_offset_bps / 10000
                self._last_entry_price = px
                return StrategyDecision("buy", f"grid buy level {level}: dev {deviation_bps:.2f} <= {trigger:.2f}, vol {vol_bps:.2f}", "grid", round(px, 2), self.config.size)

        return StrategyDecision("hold", f"grid no level: dev {deviation_bps:.2f} bps, spacing {spacing:.2f}, trend {trend_bps:.2f}, vol {vol_bps:.2f}", "grid")


# Backwards-compatible aliases used by old CLI code/tests.
MeanReversionConfig = StrategyConfig
MeanReversionStrategy = HybridStrategy


def position_size_from_user_state(user_state: dict[str, Any], coin: str) -> float:
    coin = coin.upper()
    for item in user_state.get("assetPositions", []):
        pos = item.get("position", {})
        if str(pos.get("coin", "")).upper() == coin:
            return float(pos.get("szi", "0") or 0)
    return 0.0
