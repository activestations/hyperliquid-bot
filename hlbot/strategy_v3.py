"""
AdaptiveGridStrategy — v3 双向自适应网格策略 (增强版)

核心设计:
  - 单仓位, 一次只做一个方向 (long 或 short)
  - 方向由价格偏离均线的方向决定:
    价格明显低于均线 → long (买跌反弹)
    价格明显高于均线 → short (卖高回落)
  - 每方向 3 档网格, DCA 入场
  - 方向惩罚: 连续亏损的方向入场门槛自动上移, 避免死扛趋势
  - 波动率自适应: 高波动时自动放宽入场/止盈/止损阈值
  - 分批止盈: 首次止盈退出部分仓位, 剩余仓位以更宽止盈持有
  - 冷却机制: 连续止损后暂停入场

对比 v2 (PureGrid):
  - v2 只做多, 单向网格抄底
  - v3 双向 + 方向惩罚 + 波动率适应, 完整风险体系
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import mean, pstdev

from .strategy import StrategyDecision

# PnL 比较容差: 修复浮点精度导致 29.9999 < 30.0 不触发的 bug
PNL_EPSILON = 0.01


@dataclass
class AdaptiveGridConfig:
    """双向自适应网格策略配置"""
    coin: str = "ETH"
    base_size: float = 0.005          # 基准仓位
    lookback: int = 48                # 均线周期

    # 入场
    entry_bps: float = 6.0            # 入场偏离阈值
    grid_levels: int = 3              # 每方向档位数
    grid_spacing_bps: float = 20.0    # 档位间隔

    # 退出
    take_profit_bps: float = 28.0     # 止盈 bps
    stop_loss_bps: float = 28.0       # 止损 bps

    # 分批止盈
    partial_tp_ratio: float = 0.6     # 首次止盈退出比例 (剩余 0.4 以 extended_tp 持有)
    extended_tp_multiplier: float = 1.5  # 剩余仓位止盈乘数

    # 波动率自适应
    volatility_scaling: bool = True   # 启用波动率自适应
    vol_baseline_bps: float = 8.0     # 基准波动率 (标准差)
    vol_sensitivity: float = 0.3      # 波动率缩放灵敏度 (0.3 = 温和, 0.5 = 激进)
    vol_scale_cap: float = 2.5        # 缩放上限倍率
    vol_scale_floor: float = 0.5      # 缩放下限倍率

    # 方向惩罚
    direction_penalty_threshold: int = 2    # 连续 N 次亏损 → 入场门槛 x1.5
    direction_suspend_threshold: int = 5    # 连续 M 次亏损 → 禁用该方向
    entry_penalty_multiplier: float = 1.5   # 惩罚后阈值乘数

    # 趋势过滤
    trend_block_bps: float = 0.5            # 线性斜率阈值 (bps/迭代), 超过则阻塞逆势方向

    # 过滤器
    min_vol_bps: float = 0.5          # 最低波动 (低于此不交易)
    max_vol_bps: float = 100.0        # 最高波动 (高于此不交易)
    maker_offset_bps: float = 2.0     # maker 偏移

    # 风险控制
    max_consecutive_losses: int = 3   # 连续止损冷却阈值
    cooloff_rounds: int = 30          # 冷却轮数
    order_timeout_rounds: int = 12    # 订单超时轮数


@dataclass
class GridLevel:
    trigger_bps: float           # 触发偏离 (long 为负, short 为正)
    size: float                  # 该档仓位
    entry_px: float | None = None
    filled: bool = False
    closed: bool = False
    pnl_bps: float | None = None
    pending: bool = False
    # 分批止盈状态 (仅当前 active level 使用)
    partial_exited: bool = False      # 是否已部分止盈
    extended_tp_bps: float | None = None  # 剩余仓位的扩展止盈


class AdaptiveGridStrategy:
    """双向自适应网格 — 单仓位, 方向由市场决定"""

    def __init__(self, config: AdaptiveGridConfig):
        self.config = config
        self._anchor: float | None = None
        self._direction: str | None = None        # "long" | "short" | None
        self._levels: list[GridLevel] = []
        self._consecutive_losses: int = 0
        self._cooloff_remaining: int = 0
        self._pending_age: int = 0
        self._initialized: bool = False
        self._level_index: int = 0

        # ── 方向惩罚状态 ──
        self._long_losses: int = 0       # 做多连续亏损次数
        self._short_losses: int = 0      # 做空连续亏损次数

        # ── 当前持仓生效的缩放值 (建网格时确定) ──
        self._effective_entry_bps: float = config.entry_bps
        self._effective_tp_bps: float = config.take_profit_bps
        self._effective_sl_bps: float = config.stop_loss_bps
        self._effective_spacing_bps: float = config.grid_spacing_bps
        self._effective_extended_tp_bps: float = config.take_profit_bps * config.extended_tp_multiplier

    def _current_size(self) -> float:
        """当前持仓的标的数量"""
        lv = self._current_level()
        if lv is None:
            return 0.0
        if lv.partial_exited:
            return lv.size * (1 - self.config.partial_tp_ratio)
        return lv.size

    # ── 内部方法 ──────────────────────────────────────────

    def _market_stats(self, mids: list[float]) -> tuple[float, float, float, float]:
        window = mids[-self.config.lookback:]
        last = window[-1]
        avg = mean(window)
        returns = [(window[i] / window[i - 1] - 1) * 10000 for i in range(1, len(window)) if window[i - 1] > 0]
        vol = pstdev(returns) if len(returns) > 1 else 0.0
        deviation = (last - avg) / avg * 10000 if avg > 0 else 0.0
        return last, avg, vol, deviation

    def _compute_slope(self, mids: list[float]) -> float:
        """线性回归趋势斜率, 返回 bps/迭代"""
        window = mids[-self.config.lookback:]
        n = len(window)
        if n < 3:
            return 0.0
        x_mean = (n - 1) / 2.0
        y_mean = mean(window)
        num = sum((i - x_mean) * (px - y_mean) for i, px in enumerate(window))
        den = sum((i - x_mean) ** 2 for i in range(n))
        if den == 0 or y_mean == 0:
            return 0.0
        slope_price = num / den
        return slope_price / y_mean * 10000  # bps per iteration

    def _compute_effective_entry_bps(self, direction: str, vol_bps: float) -> tuple[float, str]:
        """计算生效的入场阈值, 返回 (effective_bps, reason_suffix)"""
        base = self.config.entry_bps
        reasons = []

        # 波动率缩放
        scale = 1.0
        if self.config.volatility_scaling and self.config.vol_baseline_bps > 0:
            vol_ratio = vol_bps / self.config.vol_baseline_bps
            scale = 1.0 + self.config.vol_sensitivity * (vol_ratio - 1.0)
            scale = max(self.config.vol_scale_floor, min(self.config.vol_scale_cap, scale))
            if abs(scale - 1.0) > 0.1:
                reasons.append(f"vol×{scale:.2f}")

        # 方向惩罚
        loss_count = self._long_losses if direction == "long" else self._short_losses
        if loss_count >= self.config.direction_suspend_threshold:
            return float("inf"), f"suspended ({loss_count}L)"
        if loss_count >= self.config.direction_penalty_threshold:
            scale *= self.config.entry_penalty_multiplier
            reasons.append(f"penalty×{self.config.entry_penalty_multiplier}")

        eff = base * scale
        suffix = ", ".join(reasons)
        return eff, suffix

    def _build_grid(self, direction: str) -> None:
        """用当前生效参数重建网格"""
        self._direction = direction
        self._levels = []
        sign = -1 if direction == "long" else 1
        for i in range(self.config.grid_levels):
            trigger = sign * (self._effective_entry_bps + i * self._effective_spacing_bps)
            self._levels.append(GridLevel(
                trigger_bps=trigger,
                size=self.config.base_size,
            ))
        self._level_index = 0

    def _current_level(self) -> GridLevel | None:
        if self._level_index < len(self._levels):
            return self._levels[self._level_index]
        return None

    def _pnl_bps(self, entry_px: float, last: float) -> float:
        if self._direction == "long":
            return (last - entry_px) / entry_px * 10000
        else:
            return (entry_px - last) / entry_px * 10000

    def _tp_exit(self, level: GridLevel, last: float, pnl: float) -> StrategyDecision:
        """止盈退出逻辑 (支持分批)"""
        if self._direction == "long":
            side = "sell"
            px = last * (1 + self.config.maker_offset_bps / 10000)
        else:
            side = "buy"
            px = last * (1 - self.config.maker_offset_bps / 10000)

        if (not level.partial_exited
                and self.config.partial_tp_ratio < 1.0
                and self._level_index < len(self._levels) - 1):
            # ═══ 分批止盈: 退出 partial_tp_ratio, 剩余持有 ═══
            partial_size = level.size * self.config.partial_tp_ratio
            level.partial_exited = True
            level.extended_tp_bps = self._effective_extended_tp_bps
            self._consecutive_losses = 0
            if self._direction == "long":
                self._long_losses = 0
            else:
                self._short_losses = 0
            dir_label = "L" if self._direction == "long" else "S"
            return StrategyDecision(
                side, f"TP-partial {dir_label}{self._level_index+1}: +{pnl:.1f}bps ({partial_size}/{level.size})",
                "grid", round(px, 2), round(partial_size, 8), reduce_only=True,
            )
        else:
            # ═══ 全仓止盈退出 ═══
            remaining = self._current_size()  # 退出前捕获
            level.closed = True
            self._level_index += 1
            self._consecutive_losses = 0
            if self._direction == "long":
                self._long_losses = 0
            else:
                self._short_losses = 0
            dir_label = "L" if self._direction == "long" else "S"
            return StrategyDecision(
                side, f"TP {dir_label}{self._level_index}: +{pnl:.1f}bps",
                "grid", round(px, 2), round(remaining, 8), reduce_only=True,
            )

    def _sl_exit(self, level: GridLevel, last: float, pnl: float) -> StrategyDecision:
        """止损退出"""
        if self._direction == "long":
            side = "sell"
            px = last * (1 - self.config.maker_offset_bps / 10000)
        else:
            side = "buy"
            px = last * (1 + self.config.maker_offset_bps / 10000)

        remaining = self._current_size()  # 退出前捕获
        level.closed = True
        self._level_index += 1
        self._consecutive_losses += 1

        # 方向级计数
        if self._direction == "long":
            self._long_losses += 1
        else:
            self._short_losses += 1

        if self._consecutive_losses >= self.config.max_consecutive_losses:
            self._cooloff_remaining = self.config.cooloff_rounds

        dir_label = "L" if self._direction == "long" else "S"
        return StrategyDecision(
            side, f"SL {dir_label}{self._level_index}: {pnl:.1f}bps",
            "grid", round(px, 2), round(remaining, 8), reduce_only=True,
        )

    def sync_position(self, *, entry_px: float | None, position_size: float) -> None:
        """进程重启后同步持仓状态"""
        if position_size == 0 or entry_px is None or entry_px <= 0:
            return
        if not self._initialized:
            return
        lv = self._current_level()
        if lv and not lv.entry_px:
            lv.entry_px = entry_px
            lv.filled = True
            lv.pending = False
            self._pending_age = 0

    def clear_pending_order(self) -> None:
        lv = self._current_level()
        if lv:
            lv.pending = False
        self._pending_age = 0

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

        # ── 初始化锚点 ──
        if not self._initialized:
            self._anchor = avg
            self._initialized = True

        # ──────────────────────────────────────────────
        # CASE 1: 有持仓 → 止盈/止损/持有
        # ──────────────────────────────────────────────
        if position_size != 0:
            lv = self._current_level()
            if lv and lv.pending:
                lv.pending = False
                lv.filled = True

            if lv and lv.entry_px and lv.entry_px > 0:
                pnl = self._pnl_bps(lv.entry_px, last)

                # 扩展止盈检查 (部分止盈剩余仓位)
                if lv.partial_exited and lv.extended_tp_bps is not None:
                    dir_label = "L" if self._direction == "long" else "S"
                    remaining = lv.size * (1 - self.config.partial_tp_ratio)  # 剩余仓位
                    side = "sell" if self._direction == "long" else "buy"
                    # 止损 (剩余仓位也要有止损保护!)
                    if pnl - PNL_EPSILON <= -self._effective_sl_bps:
                        lv.closed = True
                        self._level_index += 1
                        self._consecutive_losses += 1
                        if self._direction == "long":
                            self._long_losses += 1
                        else:
                            self._short_losses += 1
                        if self._consecutive_losses >= self.config.max_consecutive_losses:
                            self._cooloff_remaining = self.config.cooloff_rounds
                        sl_px = last * (1 - self.config.maker_offset_bps / 10000) if self._direction == "long" else last * (1 + self.config.maker_offset_bps / 10000)
                        return StrategyDecision(
                            side, f"SL {dir_label}{self._level_index} p: {pnl:.1f}bps",
                            "grid", round(sl_px, 2), round(remaining, 8), reduce_only=True,
                        )
                    # 扩展止盈
                    if pnl + PNL_EPSILON >= lv.extended_tp_bps:
                        lv.closed = True
                        self._level_index += 1
                        tp_px = last * (1 + self.config.maker_offset_bps / 10000) if self._direction == "long" else last * (1 - self.config.maker_offset_bps / 10000)
                        return StrategyDecision(
                            side, f"TP-ext {dir_label}{self._level_index}: +{pnl:.1f}bps",
                            "grid", round(tp_px, 2), round(remaining, 8), reduce_only=True,
                        )
                    return StrategyDecision("hold",
                        f"hold {dir_label}{self._level_index+1} (partial): pnl {pnl:.2f} bps")

                # 常规止盈
                if pnl + PNL_EPSILON >= self._effective_tp_bps:
                    return self._tp_exit(lv, last, pnl)

                # 止损
                if pnl - PNL_EPSILON <= -self._effective_sl_bps:
                    return self._sl_exit(lv, last, pnl)

                # 持有中
                dir_label = "L" if self._direction == "long" else "S"
                label = f"{dir_label}{self._level_index + 1}"
                if lv.partial_exited:
                    label += "/partial"
                return StrategyDecision("hold",
                    f"hold {label}: pnl {pnl:.2f} bps")

            # 有仓位但无记录 → 紧急退出
            exit_side = "sell" if position_size > 0 else "buy"
            return StrategyDecision(
                exit_side, "untracked position - safe exit",
                "grid", round(last, 2),
                abs(position_size), reduce_only=True,
            )

        # ──────────────────────────────────────────────
        # 冷却状态 → 阻止新入场
        # ──────────────────────────────────────────────
        if self._cooloff_remaining > 0:
            self._cooloff_remaining -= 1
            return StrategyDecision("hold", f"cooloff {self._cooloff_remaining} rounds")

        # ──────────────────────────────────────────────
        # CASE 2: 无持仓 → 确定方向并开仓
        # ──────────────────────────────────────────────
        if position_size == 0:
            # 网格走完了或方向未定 → 重新评估方向
            if self._direction and self._level_index >= len(self._levels):
                self._direction = None

            if self._direction is None:
                # 判断是否应该做多或做空
                chosen_dir = None
                blocked_reason = None

                # 检查 long (价格偏低)
                if deviation_bps <= -self.config.entry_bps:
                    eff_entry, reason = self._compute_effective_entry_bps("long", vol_bps)
                    if deviation_bps <= -eff_entry:
                        chosen_dir = "long"
                    else:
                        blocked_reason = f"wait long: dev {deviation_bps:.2f}, eff entry {eff_entry:.1f} ({reason})"

                # 检查 short (价格偏高), 优先 long (保守偏向)
                if chosen_dir is None and deviation_bps >= self.config.entry_bps:
                    eff_entry, reason = self._compute_effective_entry_bps("short", vol_bps)
                    if deviation_bps >= eff_entry:
                        chosen_dir = "short"
                    else:
                        blocked_reason = f"wait short: dev {deviation_bps:.2f}, eff entry {eff_entry:.1f} ({reason})"

                if chosen_dir is None:
                    # 两个方向都不满足
                    if blocked_reason:
                        return StrategyDecision("hold", blocked_reason)
                    # 偏离太小, 哪个方向都没触发
                    return StrategyDecision("hold",
                        f"wait entry: dev {deviation_bps:.2f} bps (need ±{self.config.entry_bps})")

                # ── 趋势过滤: 强趋势方向阻塞逆势入场 ──
                slope = self._compute_slope(mids)
                if chosen_dir == "long" and slope < -self.config.trend_block_bps:
                    return StrategyDecision("hold",
                        f"downtrend {slope:.2f} bps/iter, skip long")
                if chosen_dir == "short" and slope > self.config.trend_block_bps:
                    return StrategyDecision("hold",
                        f"uptrend {slope:.2f} bps/iter, skip short")
                self._effective_entry_bps = self._compute_scaled_entry(chosen_dir, vol_bps)
                self._effective_tp_bps = self._compute_scaled_tpsl(vol_bps, self.config.take_profit_bps)
                self._effective_sl_bps = self._compute_scaled_tpsl(vol_bps, self.config.stop_loss_bps)
                self._effective_spacing_bps = self._compute_scaled_spacing(vol_bps)
                self._effective_extended_tp_bps = self._effective_tp_bps * self.config.extended_tp_multiplier
                self._build_grid(chosen_dir)

            lv = self._current_level()
            if lv is None:
                return StrategyDecision("hold", "no grid levels available")

            # pending 订单超时检查
            if lv.pending:
                self._pending_age += 1
                if self._pending_age >= self.config.order_timeout_rounds:
                    lv.pending = False
                    lv.closed = True
                    self._level_index += 1
                    self._pending_age = 0
                    return StrategyDecision("hold", f"order timeout L{self._level_index}, skip")
                return StrategyDecision("hold", f"pending entry L{self._level_index+1}, age {self._pending_age}")

            # 检查是否触发入场
            trigger = lv.trigger_bps
            if self._direction == "long":
                if deviation_bps <= trigger:
                    px = last * (1 - self.config.maker_offset_bps / 10000)
                    lv.pending = True
                    lv.entry_px = px
                    self._pending_age = 0
                    return StrategyDecision(
                        "buy", f"entry L{self._level_index+1}/{len(self._levels)}: dev {deviation_bps:.2f} <= {trigger:.2f}",
                        "grid", round(px, 2), self.config.base_size,
                    )
            else:
                if deviation_bps >= trigger:
                    px = last * (1 + self.config.maker_offset_bps / 10000)
                    lv.pending = True
                    lv.entry_px = px
                    self._pending_age = 0
                    return StrategyDecision(
                        "sell", f"entry S{self._level_index+1}/{len(self._levels)}: dev +{deviation_bps:.2f} >= {trigger:.2f}",
                        "grid", round(px, 2), self.config.base_size,
                    )

            dir_label = "L" if self._direction == "long" else "S"
            return StrategyDecision("hold",
                f"wait {dir_label}{self._level_index+1}: dev {deviation_bps:.2f}, trigger at {trigger:.2f} bps")

        return StrategyDecision("hold", f"dev {deviation_bps:.2f} bps, pos {position_size}")

    # ── 缩放辅助方法 ─────────────────────────────────────

    def _vol_scale(self, vol_bps: float) -> float:
        """计算波动率缩放因子"""
        if not self.config.volatility_scaling or self.config.vol_baseline_bps <= 0:
            return 1.0
        vol_ratio = vol_bps / self.config.vol_baseline_bps
        scale = 1.0 + self.config.vol_sensitivity * (vol_ratio - 1.0)
        return max(self.config.vol_scale_floor, min(self.config.vol_scale_cap, scale))

    def _compute_scaled_entry(self, direction: str, vol_bps: float) -> float:
        """计算入场阈值的缩放 (不含惩罚, 用于建网格)"""
        base = self.config.entry_bps * self._vol_scale(vol_bps)
        loss_count = self._long_losses if direction == "long" else self._short_losses
        penalty = 1.0
        if loss_count >= self.config.direction_suspend_threshold:
            return float("inf")
        if loss_count >= self.config.direction_penalty_threshold:
            penalty = self.config.entry_penalty_multiplier
        return base * penalty

    def _compute_scaled_tpsl(self, vol_bps: float, base_bps: float) -> float:
        """计算止盈/止损的缩放"""
        return base_bps * self._vol_scale(vol_bps)

    def _compute_scaled_spacing(self, vol_bps: float) -> float:
        """计算网格间距的缩放"""
        return self.config.grid_spacing_bps * self._vol_scale(vol_bps)
