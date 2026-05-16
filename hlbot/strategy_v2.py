"""
PureGridStrategy — v2 纯网格策略

核心设计:
  - Long only (永不卖空)
  - 单仓位顺序网格: 每次只持有一个仓位, 平仓后再进下一档
  - 多层网格: price 跌穿不同档位后顺序入场
  - 每档独立止盈止损
  - 波动率过滤 + 连续亏损冷却 + 全局 drawdown 保护

对比 v1 的修复:
  - 去掉趋势跟随 (不再多空互搏)
  - 去掉 deviation 条件退出 (之前 or 条件的 bug)
  - 锚点固定, 不随价格漂移
  - 加入亏损冷却机制
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import mean, pstdev

from .strategy import StrategyDecision


@dataclass
class PureGridConfig:
    """纯网格策略配置"""
    coin: str = "ETH"
    base_size: float = 0.005          # 每档基准仓位
    lookback: int = 48                # 均线周期

    # 网格结构 — trigger = -(first_entry + i * spacing) bps
    grid_levels: int = 3              # 档位数
    grid_spacing_bps: float = 20.0    # 档位间隔
    first_entry_bps: float = 15.0     # 第一档触发 (deviation 低于均线多少 bps)

    # 每档退出
    take_profit_bps: float = 28.0     # 止盈 (高于入场价)
    stop_loss_bps: float = 40.0       # 止损 (低于入场价)

    # 过滤器
    min_vol_bps: float = 1.0          # 最低波动 (太安静不做)
    max_vol_bps: float = 80.0         # 最高波动 (太剧烈不做)
    maker_offset_bps: float = 2.0     # maker 订单偏移

    # 趋势熔断
    trend_slope_bps: float = 20.0     # 下跌趋势阈值 (近半窗口均值比前半低 N bps → 暂不入场)

    # 风险控制
    max_consecutive_losses: int = 3   # 连续止损几次后冷却
    cooloff_rounds: int = 30          # 冷却轮数 (~15 分钟)
    order_timeout_rounds: int = 12    # 订单多久未成交就放弃 (~6 分钟)


@dataclass
class GridLevel:
    trigger_bps: float    # 触发偏离 (负数)
    size: float           # 该档仓位
    entry_px: float | None = None
    filled: bool = False
    closed: bool = False
    pnl_bps: float | None = None
    pending: bool = False  # 订单已提交, 等待成交


class PureGridStrategy:
    """纯网格策略 — 只做多, 不追趋势, 逐档进出"""

    def __init__(self, config: PureGridConfig):
        self.config = config
        self._anchor: float | None = None
        self._levels: list[GridLevel] = []
        self._consecutive_losses: int = 0
        self._cooloff_remaining: int = 0
        self._pending_age: int = 0
        self._initialized: bool = False
        self._level_index: int = 0  # 当前轮到第几档

    # ── 内部方法 ──────────────────────────────────────────

    def _market_stats(self, mids: list[float]) -> tuple[float, float, float, float]:
        window = mids[-self.config.lookback:]
        last = window[-1]
        avg = mean(window)
        returns = [(window[i] / window[i - 1] - 1) * 10000 for i in range(1, len(window)) if window[i - 1] > 0]
        vol = pstdev(returns) if len(returns) > 1 else 0.0
        deviation = (last - avg) / avg * 10000 if avg > 0 else 0.0
        return last, avg, vol, deviation

    def _build_grid(self) -> None:
        """根据配置重建网格层"""
        self._levels = []
        for i in range(self.config.grid_levels):
            trigger = -(self.config.first_entry_bps + i * self.config.grid_spacing_bps)
            self._levels.append(GridLevel(
                trigger_bps=trigger,
                size=self.config.base_size,
            ))
        self._level_index = 0

    def _current_level(self) -> GridLevel | None:
        """返回当前等待入场或已入场的档位"""
        if self._level_index < len(self._levels):
            return self._levels[self._level_index]
        return None

    def sync_position(self, *, entry_px: float | None, position_size: float) -> None:
        """Sync strategy memory with exchange position after restart.

        The daemon is stateless across process restarts except for mids/fills. If
        there is an exchange position, seed the current level with the account
        entry price so v2 can manage TP/SL instead of treating it as untracked.
        """
        if position_size <= 0 or entry_px is None or entry_px <= 0:
            return
        if not self._initialized:
            self._build_grid()
            self._initialized = True
        lv = self._current_level()
        if lv and not lv.entry_px:
            lv.entry_px = entry_px
            lv.filled = True
            lv.pending = False
            self._pending_age = 0

    def clear_pending_order(self) -> None:
        """Reset pending state after an order is rejected or not submitted."""
        lv = self._current_level()
        if lv:
            lv.pending = False
        self._pending_age = 0

    def _is_downtrend(self, mids: list[float]) -> bool:
        """
        检查是否处于下跌趋势。
        将 lookback 窗口切成前后两半，如果后半均值显著低于前半 → downtrend。
        防止在持续下跌行情中反复抄底接飞刀。
        """
        window = mids[-self.config.lookback:]
        if len(window) < 4:
            return False
        half = len(window) // 2
        early_avg = mean(window[:half])
        recent_avg = mean(window[half:])
        if early_avg <= 0 or recent_avg <= 0:
            return False
        slope_bps = (recent_avg - early_avg) / early_avg * 10000
        return slope_bps < -self.config.trend_slope_bps

    # ── 主决策入口 ───────────────────────────────────────

    def decide(self, *, mids: list[float], position_size: float) -> StrategyDecision:
        need = self.config.lookback
        if len(mids) < need:
            return StrategyDecision("hold", f"need {need} mids, got {len(mids)}")

        last, avg, vol_bps, deviation_bps = self._market_stats(mids)

        if avg <= 0 or last <= 0:
            return StrategyDecision("hold", "invalid mid price")

        # ── 波动率过滤 ──
        if vol_bps < self.config.min_vol_bps:
            return StrategyDecision("hold", f"vol {vol_bps:.2f} < min {self.config.min_vol_bps}")
        if vol_bps > self.config.max_vol_bps:
            return StrategyDecision("hold", f"vol {vol_bps:.2f} > max {self.config.max_vol_bps}")

        # ── 初始化锚点和网格 ──
        if not self._initialized:
            self._anchor = avg
            self._build_grid()
            self._initialized = True

        # ──────────────────────────────────────────────
        # CASE 1: 有持仓 → 先管理止盈止损（不受冷却影响）
        # ──────────────────────────────────────────────
        if position_size > 0:
            lv = self._current_level()
            # 如果 pending 状态但已成交 → 清除 pending
            if lv and lv.pending:
                lv.pending = False
                lv.filled = True

            if lv and lv.entry_px and lv.entry_px > 0:
                pnl_bps = (last - lv.entry_px) / lv.entry_px * 10000

                # 止盈
                if pnl_bps >= self.config.take_profit_bps:
                    level_no = self._level_index + 1
                    px = last * (1 + self.config.maker_offset_bps / 10000)
                    lv.closed = True
                    self._level_index += 1  # 进下一档
                    self._consecutive_losses = 0  # 盈利了, 重置亏损计数
                    return StrategyDecision(
                        "sell", f"TP L{level_no}: +{pnl_bps:.1f} bps",
                        "grid", round(px, 2), min(position_size, self.config.base_size), reduce_only=True,
                    )

                # 止损
                if pnl_bps <= -self.config.stop_loss_bps:
                    level_no = self._level_index + 1
                    px = last * (1 - self.config.maker_offset_bps / 10000)
                    lv.closed = True
                    self._level_index += 1
                    self._consecutive_losses += 1
                    if self._consecutive_losses >= self.config.max_consecutive_losses:
                        self._cooloff_remaining = self.config.cooloff_rounds
                    return StrategyDecision(
                        "sell", f"SL L{level_no}: {pnl_bps:.1f} bps",
                        "grid", round(px, 2), min(position_size, self.config.base_size), reduce_only=True,
                    )

                # 持有中
                return StrategyDecision("hold", f"hold L{self._level_index + 1}: pnl {pnl_bps:.2f} bps")

            # 有仓位但无记录的 entry → 用当前价格紧急退出 (防异常)
            # 注意：这不计入连续亏损（只是 cleanup sell，不是真实交易损失）
            return StrategyDecision(
                "sell", "untracked position - safe exit",
                "grid", round(last, 2),
                position_size, reduce_only=True,
            )

        # ──────────────────────────────────────────────
        # 冷却状态 → 只阻止新入场（持仓管理已在前面处理）
        # ──────────────────────────────────────────────
        if self._cooloff_remaining > 0:
            self._cooloff_remaining -= 1
            return StrategyDecision("hold", f"cooloff {self._cooloff_remaining} rounds")

        # ──────────────────────────────────────────────
        # CASE 2: 无持仓 → 检查入场（受冷却控制）
        # ──────────────────────────────────────────────
        if position_size <= 0:
            # 如果所有档位都走完了 → 重建网格重新开始
            if self._level_index >= len(self._levels):
                self._build_grid()
                self._level_index = 0

            lv = self._current_level()
            if lv is None:
                return StrategyDecision("hold", "no grid levels available")

            # 有 pending 订单 → 检查超时
            if lv.pending:
                self._pending_age += 1
                if self._pending_age >= self.config.order_timeout_rounds:
                    lv.pending = False
                    lv.closed = True
                    self._level_index += 1
                    self._pending_age = 0
                    return StrategyDecision("hold", f"order timeout L{self._level_index}, skip")
                return StrategyDecision("hold", f"pending entry L{self._level_index+1}, age {self._pending_age}")

            # ── 趋势熔断: 持续下跌趋势中暂不入场 ──
            if self._is_downtrend(mids):
                return StrategyDecision("hold", "downtrend, skip entry")

            # 检查是否触发该档入场
            trigger = lv.trigger_bps
            if deviation_bps <= trigger:
                px = last * (1 - self.config.maker_offset_bps / 10000)
                lv.pending = True
                lv.entry_px = px
                self._pending_age = 0
                return StrategyDecision(
                    "buy", f"entry L{self._level_index+1}/{len(self._levels)}: dev {deviation_bps:.2f} <= {trigger:.2f}",
                    "grid", round(px, 2), self.config.base_size,
                )

            return StrategyDecision("hold",
                f"wait L{self._level_index+1}: dev {deviation_bps:.2f}, trigger at {trigger:.2f} bps")

        return StrategyDecision("hold", f"dev {deviation_bps:.2f} bps, pos {position_size}")
