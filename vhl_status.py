#!/usr/bin/env python3
"""VHL 状态查看器 — 实时看三个币的模拟交易结果"""
import json
from pathlib import Path

BASE = Path(__file__).resolve().parent
COINS = ["BTC", "ETH", "SOL"]

for coin in COINS:
    state_file = BASE / f"paper_state_{coin}.json"
    if not state_file.exists():
        print(f"╔═══ {coin} ═══ NO DATA (still warming up)")
        continue

    s = json.loads(state_file.read_text())
    mid = s["mid"]
    pos = s["position"]
    entry = s["entry_price"]
    bal = s["balance"]
    rpnl = s["realized_pnl"]
    upnl = s["unrealized_pnl"]
    fees = s["total_fees"]
    volume = s["total_volume"]
    mid_count = s["mids_count"]
    dir_ = s.get("direction") or "-"
    cooloff = s.get("cooloff_remaining") or 0
    level = s.get("level_index") or 0

    net = rpnl + upnl
    roi = (net / bal) * 100 if bal > 0 else 0

    print(f"╔═══ {coin} ════════════════════════════════════")
    print(f"║ 行情: ${mid:>10s}" if isinstance(mid, str) else f"║ 行情: ${mid:>10.2f}")
    print(f"║ 持仓: {pos:+.6f} (入场 {entry or '-':>10})")
    print(f"║ 余额: ${bal:<10.2f}  |  已实现 PnL: ${rpnl:<+8.4f}")
    print(f"║ 未实现: ${upnl:<+8.4f}  |  净 PnL:    ${net:<+8.4f}")
    print(f"║ 总手续费: ${fees:<8.4f}  |  总交易额: ${volume:<.1f}")
    print(f"║ ROI: {roi:<+.2f}%  | 样本点: {mid_count}  |  方向: {dir_}")
    print(f"║ 级别: L{level} 冷确剩余: {cooloff}")
    print("╚═══════════════════════════════════════════════")
    print()
