from __future__ import annotations

import json
import os
import re
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any


@dataclass(frozen=True)
class Notifier:
    webhook_url: str = ""

    @classmethod
    def from_env(cls) -> "Notifier":
        return cls(webhook_url=(os.getenv("HLBOT_FEISHU_WEBHOOK") or "").strip())

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_url)

    def send_text(self, text: str) -> bool:
        if not self.enabled:
            return False
        payload = json.dumps({"msg_type": "text", "content": {"text": text}}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.webhook_url,
            data=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 - user-provided webhook endpoint
            resp.read()
        return True


# ── 辅助方法 ──────────────────────────────────────────────

def _dir_label(side: str, reduce_only: bool) -> str:
    """根据 side + reduce_only 判断动作含义"""
    # side: buy | sell
    # reduce_only: True = 平仓, False = 开仓
    # buy to open → 开多 (买入做多, 做的是长仓)
    # sell to open → 开空 (卖出做空, 做的是短仓)
    # buy reduce → 平空 (买入平掉短仓)
    # sell reduce → 平多 (卖出平掉长仓)
    if side == "buy":
        if reduce_only:
            return "平空 📈"
        return "开多 📈"
    else:  # sell
        if reduce_only:
            return "平多 📉"
        return "开空 📉"


def _pnl_str(pnl: float | None) -> str:
    if pnl is None:
        return "—"
    if pnl > 0:
        return f"+{pnl:.2f}"
    return f"{pnl:.2f}"


def _equity_line(perp: float | None, total: float | None) -> str:
    if perp is None and total is None:
        return ""
    parts = []
    if perp is not None:
        parts.append(f"永续: {perp:.2f}")
    if total is not None:
        parts.append(f"总: {total:.2f}")
    return "💰 " + " / ".join(parts) + " USDC"


def _simplify_reason(reason: str | None) -> str:
    """将策略 reason 缩短为中文信号描述"""
    if not reason:
        return "—"

    # ── v3 双向网格 ──
    # entry L1/3: dev -40.44 <= -3.83  /  entry S1/3: dev +42 >= 3
    if m := re.search(r"entry ([LS])(\d+)/(\d+)", reason):
        dir_zh = "多" if m.group(1) == "L" else "空"
        emoji = "📈" if m.group(1) == "L" else "📉"
        return f"{dir_zh}{emoji}层{m.group(2)}入场"
    # TP L1: +30.0bps  /  TP S2: +25.0bps
    if m := re.search(r"TP ([LS])(\d+)", reason):
        direction = "📗" if m.group(1) == "L" else "💚"
        return f"止盈{direction}层{m.group(2)}"
    # SL L1: -30.0bps  /  SL S2: -25.0bps
    if m := re.search(r"SL ([LS])(\d+)", reason):
        direction = "📕" if m.group(1) == "L" else "💔"
        return f"止损{direction}层{m.group(2)}"
    # TP-partial / TP-ext
    if "TP-partial" in reason:
        return f"分批止盈"
    if "TP-ext" in reason:
        return f"延展止盈"
    # hold L1/partial: pnl ...
    if "partial" in reason:
        return f"部分持有中"
    # pending entry L1 / S1
    if m := re.search(r"pending entry ([LS])(\d+)", reason):
        dir_zh = "多" if m.group(1) == "L" else "空"
        return f"{dir_zh}{m.group(2)}层等待成交"
    # order timeout
    if "order timeout" in reason:
        return "订单超时"

    # ── v2 纯多网格 ──
    if m := re.search(r"grid (buy|sell) level (\d)", reason):
        side_zh = "买入" if m.group(1) == "buy" else "卖出"
        return f"网格{side_zh} 第{m.group(2)}层"
    if m := re.search(r"grid take-profit pnl ([-]?[\d.]+) bps", reason):
        return f"止盈 {m.group(1)} bps"
    if m := re.search(r"grid stop pnl ([-]?[\d.]+) bps", reason):
        return f"止损 {m.group(1)} bps"
    if "grid no level" in reason:
        return "观望"

    # cooloff X rounds
    if m := re.search(r"cooloff (\d+) rounds", reason):
        return f"冷却中 ({m.group(1)})"
    # wait entry / wait long / wait short
    if m := re.search(r"wait (entry|long|short)", reason):
        return "等待入场"
    # suspended
    if "suspended" in reason:
        return "方向已禁用"
    # uptrend X bps/iter, skip short
    if m := re.search(r"uptrend ([-\d.]+) bps/iter, skip short", reason):
        return f"上升趋势 {m.group(1)}bps/it, 跳过做空"
    if m := re.search(r"downtrend ([-\d.]+) bps/iter, skip long", reason):
        return f"下降趋势 {m.group(1)}bps/it, 跳过做多"
    # hold L1: pnl ... / hold S1: pnl ...
    if m := re.search(r"hold ([LS])(\d+)", reason):
        dir_zh = "多" if m.group(1) == "L" else "空"
        return f"持{dir_zh}层{m.group(2)}"

    # fallback
    return reason[:40]


# ── 通知函数 ─────────────────────────────────────────────

def order_notification(item: dict[str, Any], coin: str) -> str | None:
    """订单通知 (挂单) — 支持双向"""
    decision_data = item.get("decision")
    if not isinstance(decision_data, dict):
        return None
    side = str(decision_data.get("side") or "").lower()
    reduce_only = bool(decision_data.get("reduce_only", False))
    if side not in ("buy", "sell") or item.get("sent") is not True:
        return None

    coin_up = coin.upper()
    price = decision_data.get("price") or item.get("mid")
    size = decision_data.get("size")
    reason = _simplify_reason(decision_data.get("reason"))
    action_label = _dir_label(side, reduce_only)

    # 估计盈亏 (仅平仓时有)
    pnl_val = item.get("estimated_pnl_usd")

    if not reduce_only:
        # ── 开仓 ──
        lines = [
            f"🕐 {coin_up} {action_label}",
            f"价格: {price}  数量: {size}",
            f"信号: {reason}",
        ]
        pos = item.get("position", 0)
        if pos:
            lines.append(f"仓位: {pos}  {coin_up}")
    else:
        # ── 平仓 ──
        pnl_display = _pnl_str(pnl_val)
        pnl_tag = f" {pnl_display} USDC" if pnl_val is not None else ""
        entry_px = item.get("entry_px")
        lines = [
            f"🕐 {coin_up} {action_label}{pnl_tag}",
            f"价格: {price}  数量: {size}",
        ]
        if entry_px:
            lines.append(f"均价: {entry_px}")
        lines.append(f"信号: {reason}")

    eq_line = _equity_line(
        float(item["equity"]) if item.get("equity") is not None else None,
        float(item["total_equity"]) if item.get("total_equity") is not None else None,
    )
    if eq_line:
        lines.append(eq_line)

    return "\n".join(lines)


def cancel_notification(coin: str, oid: int | None, reason: str | None,
                        equity: float | None, total_equity: float | None = None) -> str:
    """取消通知"""
    lines = [f"🕐 {coin.upper()} 订单超时取消"]
    if oid:
        lines.append(f"订单ID: {oid}")
    if reason:
        lines.append(f"信号: {_simplify_reason(reason)}")
    eq_line = _equity_line(equity, total_equity)
    if eq_line:
        lines.append(eq_line)
    return "\n".join(lines)


def _fill_time(fill: dict[str, Any]) -> str:
    """Convert Hyperliquid epoch-ms time field to readable CST time.
    Returns empty string if time field is missing or invalid."""
    raw = fill.get("time")
    if not raw:
        return ""
    try:
        ms = int(raw)
        dt = datetime.fromtimestamp(ms / 1000, tz=timezone(timedelta(hours=8)))
        return dt.strftime("🕐 %m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return ""

def fill_notification(fill: dict[str, Any],
                      equity: float | None = None,
                      total_equity: float | None = None) -> str | None:
    """成交通知 — 支持双向"""
    coin = str(fill.get("coin", "")).upper()
    side = str(fill.get("side", ""))  # B=buy, A=sell
    dir_ = str(fill.get("dir", ""))
    px = fill.get("px", "—")
    sz = fill.get("sz", "—")
    fee = fill.get("fee", "0")
    closed_pnl = fill.get("closedPnl", "0")
    ts_str = _fill_time(fill)

    is_buy = side == "B"
    is_reduce = dir_ in {"ReduceOnly", "Close"} or "Reduce" in dir_
    is_open = "Open" in dir_

    try:
        pnl_val = float(closed_pnl)
    except (TypeError, ValueError):
        pnl_val = 0.0

    lines: list[str] = []

    if pnl_val != 0:
        # ── 有盈亏的平仓 ──
        emoji = "🟢" if pnl_val > 0 else "🔴"
        action_label = "平多" if is_buy else "平空"
        # 注意: 在 HL 的命名中:
        #   buy (B) 平仓 = 买入平空 (short 止盈/止损)
        #   sell (A) 平仓 = 卖出平多 (long 止盈/止损)
        if is_buy:
            action_label = "平空 📈"
        else:
            action_label = "平多 📉"
        lines.append(f"{emoji} {coin} {action_label}  {pnl_val:+.2f} USDC")
        lines.append(f"价格: {px}  数量: {sz}  |  手续费: {fee}")
    elif is_reduce and not is_buy:
        # ── 卖出平仓 (无盈亏, e.g. 测试网) ──
        lines.append(f"📉 {coin} 平多")
        lines.append(f"价格: {px}  数量: {sz}")
        if fee != "0":
            lines[-1] += f"  |  手续费: {fee}"
    elif is_reduce and is_buy:
        # ── 买入平仓 ──
        lines.append(f"💰 {coin} 平空")
        lines.append(f"价格: {px}  数量: {sz}")
        if fee != "0":
            lines[-1] += f"  |  手续费: {fee}"
    elif is_buy and (is_open or not is_reduce):
        # ── 买入开仓 (long 入场) ──
        lines.append(f"✅ {coin} 开多")
        lines.append(f"价格: {px}  数量: {sz}")
        if fee != "0":
            lines[-1] += f"  |  手续费: {fee}"
    elif not is_buy and not is_reduce:
        # ── 卖出开仓 (short 入场) ──
        lines.append(f"🔽 {coin} 开空")
        lines.append(f"价格: {px}  数量: {sz}")
        if fee != "0":
            lines[-1] += f"  |  手续费: {fee}"
    else:
        # ── 回退 ──
        lines.append(f"📝 {coin} 成交")
        lines.append(f"价格: {px}  数量: {sz}")

    if ts_str:
        lines.append(ts_str)

    # 余额: 仅有实际盈亏时展示
    eq_line = _equity_line(equity, total_equity)
    if eq_line and pnl_val != 0:
        lines.append(eq_line)

    return "\n".join(lines)
