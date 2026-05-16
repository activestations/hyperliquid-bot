from __future__ import annotations

from dataclasses import asdict, dataclass
from itertools import product
from typing import Any

from .strategy import HybridStrategy, StrategyConfig


@dataclass(frozen=True)
class BacktestResult:
    config: dict[str, Any]
    candles: int
    trades: int
    wins: int
    losses: int
    pnl_usd_before_fees_slippage: float
    ending_position: float
    score: float
    max_drawdown_usd: float


def run_backtest(closes: list[float], config: StrategyConfig) -> BacktestResult:
    strat = HybridStrategy(config)
    cash = 0.0
    pos = 0.0
    entry_px: float | None = None
    wins = 0
    losses = 0
    peak = 0.0
    max_dd = 0.0
    trades = 0
    equity = 0.0

    for idx, px in enumerate(closes):
        decision = strat.decide(mids=closes[: idx + 1], position_size=pos)
        if decision.side == "buy" and pos <= 0:
            pos += decision.size
            cash -= decision.size * px
            entry_px = px
            trades += 1
        elif decision.side == "sell" and pos > 0:
            size = min(pos, decision.size)
            pos -= size
            cash += size * px
            trades += 1
            if entry_px is not None:
                if px > entry_px:
                    wins += 1
                else:
                    losses += 1
                entry_px = None
        equity = cash + pos * px
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)

    if closes:
        equity = cash + pos * closes[-1]
    # Prefer positive pnl, fewer trades, lower drawdown. Fees/slippage are not modeled, so penalize churn.
    churn_penalty = trades * 0.002
    score = equity - max_dd * 0.25 - churn_penalty
    return BacktestResult(
        config=asdict(config),
        candles=len(closes),
        trades=trades,
        wins=wins,
        losses=losses,
        pnl_usd_before_fees_slippage=round(equity, 6),
        ending_position=pos,
        score=round(score, 6),
        max_drawdown_usd=round(max_dd, 6),
    )


def optimize_strategy(closes: list[float], coin: str, size: float) -> list[BacktestResult]:
    candidates: list[BacktestResult] = []
    for mode, lookback, fast, slow, entry, exit_, trend_entry, trend_exit, min_vol, max_vol, grid_spacing, grid_tp, grid_stop in product(
        ["mean_reversion", "trend", "hybrid", "grid"],
        [20, 48],
        [8],
        [32, 48],
        [8, 20],
        [3, 8],
        [6],
        [2],
        [1],
        [80, 140],
        [12],
        [10],
        [55],
    ):
        if fast >= slow:
            continue
        cfg = StrategyConfig(
            coin=coin,
            size=size,
            lookback=lookback,
            fast_lookback=fast,
            slow_lookback=slow,
            entry_bps=entry,
            exit_bps=exit_,
            trend_entry_bps=trend_entry,
            trend_exit_bps=trend_exit,
            maker_offset_bps=2,
            min_vol_bps=min_vol,
            max_vol_bps=max_vol,
            mode=mode,
            grid_levels=3,
            grid_spacing_bps=grid_spacing,
            grid_take_profit_bps=grid_tp,
            grid_stop_bps=grid_stop,
            inventory_skew_bps=4,
        )
        result = run_backtest(closes, cfg)
        # Avoid configs that do almost nothing or churn insanely.
        if 2 <= result.trades <= max(4, len(closes) // 4):
            candidates.append(result)
    return sorted(candidates, key=lambda r: r.score, reverse=True)
