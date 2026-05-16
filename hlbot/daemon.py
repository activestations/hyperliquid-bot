from __future__ import annotations

import json
import os
import socket
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ._utils import extract_resting_oid, spread_bps as _sb
from .client import HyperliquidClient
from .config import AppConfig
from .logging import TradeLogger
from .notifier import Notifier, order_notification, cancel_notification, fill_notification
from .risk import RiskManager
from .strategy import HybridStrategy, StrategyConfig, position_size_from_user_state
from .strategy_v2 import PureGridStrategy, PureGridConfig
from .strategy_v3 import AdaptiveGridStrategy, AdaptiveGridConfig



# Network resilience: max consecutive errors before backoff kicks in
_MAX_CONSECUTIVE_ERRORS = 10
# Exponential backoff range (seconds)
_BACKOFF_MIN = 30
_BACKOFF_MAX = 300
# After this many consecutive errors, force a fresh client instance
_CLIENT_RESET_THRESHOLD = 20
# Keep enough fill IDs to dedupe Hyperliquid user_fills() across restarts without
# growing state files unbounded. user_fills() currently returns ~1k recent fills.
_MAX_SEEN_FILL_IDS = 5000


@dataclass(frozen=True)
class DaemonConfig:
    coin: str = "ETH"
    iterations: int = 0  # 0 = forever
    sleep_seconds: float = 60.0
    order_live_seconds: float = 5.0
    log_path: Path = Path("trades.jsonl")
    trade_log_path: Path = Path("trade_history.jsonl")
    heartbeat_path: Path = Path("daemon_state.json")
    starting_equity: float | None = None


def _extract_order_error(result: Any) -> str | None:
    try:
        statuses = result["response"]["data"]["statuses"]
    except (KeyError, TypeError):
        return None
    for status in statuses:
        if isinstance(status, dict) and "error" in status:
            return str(status["error"])
    return None


def _is_order_accepted(result: Any) -> bool:
    try:
        statuses = result["response"]["data"]["statuses"]
    except (KeyError, TypeError):
        return False
    for status in statuses:
        if isinstance(status, dict) and ("resting" in status or "filled" in status):
            return True
    return False


def _extract_resting_oid(result: Any) -> int | None:
    return extract_resting_oid(result)


def _spread_bps(book: dict[str, Any]) -> float | None:
    return _sb(book)


def _account_value(state: dict[str, Any]) -> float:
    return float(state.get("marginSummary", {}).get("accountValue", "0") or 0)


def _spot_usdc(client: HyperliquidClient) -> float:
    """Spot wallet USDC balance (reference only, not at risk in perp)."""
    try:
        spot_state = client.spot_user_state()
        for balance in spot_state.get("balances", []):
            if str(balance.get("coin", "")).upper() == "USDC":
                return float(balance.get("total", "0") or 0)
    except Exception:
        return 0.0
    return 0.0


def _perp_value(user_state: dict[str, Any]) -> float:
    """Perp account value = marginSummary.accountValue.

    This is what the Hyperliquid web UI shows. Do NOT add spot wallet:
    spot USDC is not at risk in perp trading.
    """
    return _account_value(user_state)


def _total_value(client: HyperliquidClient, user_state: dict[str, Any]) -> float:
    """Full account value = spot wallet total.

    In Hyperliquid's unified account model, spot wallet IS the master wallet.
    The perp trading wallet (marginSummary.accountValue) is a subset of it.
    Adding perp on top of spot would double-count.
    """
    return _spot_usdc(client)




def _position_entry_price(user_state: dict[str, Any], coin: str) -> float | None:
    coin = coin.upper()
    for item in user_state.get("assetPositions", []):
        pos = item.get("position", {})
        if str(pos.get("coin", "")).upper() != coin:
            continue
        try:
            return float(pos.get("entryPx") or 0) or None
        except (TypeError, ValueError):
            return None
    return None


def _estimated_reduce_pnl_usd(*, side: str, price: float, size: float, position_size: float, entry_price: float | None) -> float | None:
    if entry_price is None or size <= 0 or position_size == 0:
        return None
    close_size = min(abs(position_size), size)
    if close_size <= 0:
        return None
    # Positive position: selling closes longs. Negative position: buying closes shorts.
    if position_size > 0 and side == "sell":
        return (price - entry_price) * close_size
    if position_size < 0 and side == "buy":
        return (entry_price - price) * close_size
    return None

def _same_side_open_order(open_orders: list[dict[str, Any]], coin: str, side: str) -> dict[str, Any] | None:
    want_buy = side == "buy"
    coin = coin.upper()
    for order in open_orders:
        if str(order.get("coin", "")).upper() != coin:
            continue
        is_buy = order.get("side") == "B" or order.get("isBuy") is True
        if is_buy == want_buy:
            return order
    return None


def _write_state(path: Path, payload: dict[str, Any]) -> None:
    """Atomically persist daemon heartbeat/state.

    The status API and daemon can read this file while it is being updated;
    writing through a temp file avoids occasional truncated/partial JSON after
    crash, power loss, or concurrent reads.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _fill_id(fill: dict[str, Any]) -> str:
    """Stable fill identity for dedupe.

    Hyperliquid fill tids are unique but not time-monotonic, so a simple
    last_seen_tid watermark replays or skips fills. Persist a bounded set
    instead.
    """
    tid = fill.get("tid")
    if tid is not None:
        return str(tid)
    return ":".join(str(fill.get(k, "")) for k in ("time", "coin", "oid", "px", "sz", "side", "dir"))


def _trim_seen_fill_ids(seen: set[str]) -> list[str]:
    # Deterministic bounded representation; membership, not order, matters.
    return sorted(seen)[-_MAX_SEEN_FILL_IDS:]


def _dns_healthy(host: str) -> bool:
    """Quick DNS pre-check; returns False when name resolution fails."""
    try:
        socket.getaddrinfo(host, 443)
        return True
    except OSError:
        return False


def run_daemon(config: AppConfig, strategy_config: StrategyConfig | PureGridConfig | AdaptiveGridConfig, daemon_config: DaemonConfig) -> dict[str, Any]:
    if config.environment != "testnet":
        raise RuntimeError("daemon v1 is only allowed on testnet")

    client = HyperliquidClient(config)
    risk = RiskManager(config)
    logger = TradeLogger(daemon_config.log_path)
    trade_logger = TradeLogger(daemon_config.trade_log_path)
    notifier = Notifier.from_env()
    if isinstance(strategy_config, PureGridConfig):
        strategy = PureGridStrategy(strategy_config)
    elif isinstance(strategy_config, AdaptiveGridConfig):
        strategy = AdaptiveGridStrategy(strategy_config)
    else:
        strategy = HybridStrategy(strategy_config)
    summary: dict[str, Any] = {"iterations": [], "dry_run": config.dry_run, "environment": config.environment}

    # Persist mids buffer across restarts so the warmup (48 mids ≈ 24 min) is skipped
    # when the process recycles due to the daily preemptive restart or watchdog.
    state_path = daemon_config.heartbeat_path
    try:
        prev_state = json.loads(state_path.read_text()) if state_path.exists() and state_path.stat().st_size else {}
    except json.JSONDecodeError:
        prev_state = {}
    mids: list[float] = prev_state.get("mids_buffer", [])
    last_seen_tid: int = int(prev_state.get("last_seen_tid", 0) or 0)
    seen_fill_ids: set[str] = {str(x) for x in prev_state.get("seen_fill_tids", [])}
    fill_tracker_initialized = bool(seen_fill_ids)
    # Capture daemon start time in epoch ms to filter out fills before this session.
    # This prevents cross-coin contamination from the shared daemon_state.json file.
    daemon_start_ms = int(prev_state.get("daemon_start_ms", 0))

    def state_payload(last_item: dict[str, Any]) -> dict[str, Any]:
        return {
            "last": last_item,
            "summary_count": len(summary["iterations"]),
            "mids_buffer": mids,
            "last_seen_tid": last_seen_tid,
            "seen_fill_tids": _trim_seen_fill_ids(seen_fill_ids),
            "daemon_start_ms": daemon_start_ms,
            "daemon_start_ts": summary.get("daemon_start_ts", ""),
        }

    consecutive_errors = 0
    _last_alerted_error_level = 0  # for escalation notification dedup

    start_state = client.user_state()
    start_equity = daemon_config.starting_equity if daemon_config.starting_equity is not None else _perp_value(start_state)
    start_total = _total_value(client, start_state)
    if not config.dry_run and start_total <= 0:
        raise RuntimeError("account balance is empty (total_equity=0), daemon refused to start")
    summary["starting_equity"] = start_equity
    now_utc = datetime.now(timezone.utc)
    summary["daemon_start_ts"] = now_utc.isoformat()
    daemon_start_ms = int(now_utc.timestamp() * 1000)
    # Write initial state immediately so status server has daemon_start_ts from the start
    _write_state(daemon_config.heartbeat_path, state_payload({}))

    i = 0
    while daemon_config.iterations <= 0 or i < daemon_config.iterations:
        item: dict[str, Any] = {"i": i}

        # Quick DNS pre-check before attempting any API call.
        # Prevents noisy "Temporary failure in name resolution" errors from
        # flooding the log during local router/modem hiccups (e.g. 04-09 CST).
        if consecutive_errors > 0 and not _dns_healthy("api.hyperliquid-testnet.xyz"):
            item["error"] = "DNS resolution failed; skipping iteration"
            item["type"] = "DNSError"
            logger.write("daemon_error", item)
            i += 1
            consecutive_errors += 1
            item["consecutive_errors"] = consecutive_errors
            _write_state(daemon_config.heartbeat_path, state_payload(item))
            backoff = min(_BACKOFF_MIN * (2 ** (consecutive_errors - _MAX_CONSECUTIVE_ERRORS)), _BACKOFF_MAX)
            time.sleep(backoff)
            continue

        try:
            user_state = client.user_state()
            perp_equity = _perp_value(user_state)
            total_equity = _total_value(client, user_state)
            # 统一账户下 spot 是母钱包, perp 子账户余额可能为 0 但只要有 spot 就能开单
            # 所以检查 total_equity (spot) 而不是 perp_equity
            if not config.dry_run and total_equity <= 0:
                raise RuntimeError(f"account balance is empty (spot={total_equity}), refusing to trade")
            drawdown = max(0.0, start_equity - total_equity)
            item["equity"] = perp_equity
            item["total_equity"] = total_equity
            item["drawdown"] = drawdown
            if drawdown >= config.risk.max_drawdown_usd:
                item["stopped"] = f"max_drawdown_usd reached: {drawdown:.4f} >= {config.risk.max_drawdown_usd}"
                logger.write("daemon_stop", item)
                summary["iterations"].append(item)
                break

            # Check for new fills since last iteration (real PnL from exchange).
            # Each coin daemon only reports fills for its own coin. Fill tids are not
            # time-monotonic, so we persist a bounded set instead of using tid > X.
            if not config.dry_run:
                try:
                    coin_up = strategy_config.coin.upper()
                    all_fills = [f for f in client.user_fills() if str(f.get("coin", "")).upper() == coin_up]
                    current_fill_ids = {_fill_id(f) for f in all_fills}

                    if not fill_tracker_initialized:
                        # Cold start / migrated state: mark current history as seen to
                        # prevent replaying hundreds of historical fills.
                        seen_fill_ids.update(current_fill_ids)
                        fill_tracker_initialized = True
                        item["fill_tracker_initialized"] = len(current_fill_ids)
                    else:
                        new_fills = [
                            f for f in all_fills
                            if _fill_id(f) not in seen_fill_ids
                            and int(f.get("time", 0) or 0) >= daemon_start_ms
                        ]
                        if new_fills:
                            item["new_fills_count"] = len(new_fills)
                            item["new_fills"] = [
                                {"coin": f.get("coin"), "dir": f.get("dir"), "px": f.get("px"),
                                 "sz": f.get("sz"), "closedPnl": f.get("closedPnl"),
                                 "fee": f.get("fee"), "tid": f.get("tid"), "time": f.get("time")}
                                for f in new_fills
                            ]
                            # Batch into one webhook message per iteration to avoid
                            # Feishu flood/backlog if several fills settle together.
                            fill_lines: list[str] = []
                            for fill in new_fills[:5]:
                                fill_msg = fill_notification(fill, equity=perp_equity, total_equity=total_equity)
                                if fill_msg:
                                    fill_lines.append(fill_msg)
                            if len(new_fills) > 5:
                                fill_lines.append(f"……还有 {len(new_fills) - 5} 笔成交（共计 {len(new_fills)} 笔）")
                            if fill_lines:
                                try:
                                    notifier.send_text("\n---\n".join(fill_lines))
                                except Exception:
                                    pass
                        seen_fill_ids.update(current_fill_ids)

                    tids = [int(f.get("tid", 0) or 0) for f in all_fills]
                    if tids:
                        last_seen_tid = max(last_seen_tid, max(tids))
                except Exception as fill_exc:
                    item["fill_check_error"] = str(fill_exc)

            mid = float(client.all_mids()[strategy_config.coin.upper()])
            mids.append(mid)
            # 策略只看最近 lookback 个中间价 (默认 48), 定期裁剪防无限增长
            max_mids = max(strategy_config.lookback * 3, 100)
            if len(mids) > max_mids:
                mids[: len(mids) - max_mids] = []
            pos = position_size_from_user_state(user_state, strategy_config.coin)
            entry_px = _position_entry_price(user_state, strategy_config.coin)
            # Some Hyperliquid testnet position snapshots intermittently omit entryPx
            # for partially-filled/just-updated positions. For reduce-only sell/buy
            # notifications, fall back to the strategy's last accepted/planned entry
            # anchor so trade alerts still include an estimated PnL.
            entry_px_for_pnl = entry_px if entry_px is not None else getattr(strategy, "_last_entry_price", None)
            if isinstance(strategy, PureGridStrategy) or isinstance(strategy, AdaptiveGridStrategy):
                strategy.sync_position(entry_px=entry_px, position_size=pos)
            open_orders = client.open_orders()
            item["open_orders_count"] = len(open_orders)
            spread = _spread_bps(client.l2_snapshot(strategy_config.coin))
            decision = strategy.decide(mids=mids, position_size=pos)
            item.update({"mid": mid, "position": pos, "entry_px": entry_px_for_pnl, "account_entry_px": entry_px, "spread_bps": spread, "decision": decision.__dict__})

            if spread is None or spread > config.risk.max_spread_bps:
                item["skipped"] = f"spread {spread} exceeds max_spread_bps {config.risk.max_spread_bps}"
            elif decision.side == "hold":
                item["skipped"] = decision.reason
            else:
                risk_decision = risk.validate_order(
                    coin=strategy_config.coin,
                    side=decision.side,
                    size=decision.size,
                    price=float(decision.price or mid),
                    reduce_only=decision.reduce_only,
                    current_position_size=pos,
                )
                existing = _same_side_open_order(open_orders, strategy_config.coin, decision.side)
                if existing:
                    # Stale order detection: cancel same-side orders whose price differs
                    # significantly from the new decision price, so a fresh order can be placed.
                    existing_px = float(existing.get("limitPx", 0) or 0)
                    new_px = float(decision.price or mid or 0)
                    stale = False
                    if existing_px > 0 and new_px > 0:
                        px_diff_bps = abs(new_px - existing_px) / existing_px * 10000
                        stale = px_diff_bps >= strategy_config.grid_spacing_bps * 0.5
                        if stale:
                            cancel_result = client.cancel(strategy_config.coin, int(existing["oid"]))
                            cancel_ok = cancel_result.get("status") == "ok"
                            item["cancel_stale"] = {
                                "oid": int(existing["oid"]),
                                "old_px": existing_px,
                                "new_px": new_px,
                                "diff_bps": round(px_diff_bps, 2),
                                "cancelled": cancel_ok,
                            }
                    if stale:
                        existing = None  # fall through to place new order
                    else:
                        item["skipped"] = f"existing same-side open order: oid={existing.get('oid')}"
                elif not risk_decision.ok:
                    item["skipped"] = f"risk: {risk_decision.reason}"
                    if isinstance(strategy, (PureGridStrategy, AdaptiveGridStrategy)):
                        strategy.clear_pending_order()
                else:
                    # Gtc 挂单进盘口, 允许部分成交
                    # 出场 reduce_only 用 mid 确保卖出能成交, entry 用策略价格
                    order_tif = "Gtc"
                    order_price = mid if decision.reduce_only else (decision.price or mid)
                    plan = client.build_limit_order(
                        coin=strategy_config.coin,
                        side=decision.side,
                        size=decision.size,
                        price=order_price,
                        reduce_only=decision.reduce_only,
                        tif=order_tif,
                    )
                    if config.dry_run:
                        item["sent"] = False
                        item["would_send"] = plan.to_wire()
                        if isinstance(strategy, (PureGridStrategy, AdaptiveGridStrategy)):
                            strategy.clear_pending_order()
                    else:
                        live = risk.validate_live_enabled()
                        if not live.ok:
                            raise RuntimeError(live.reason)
                        if decision.reduce_only:
                            pnl = _estimated_reduce_pnl_usd(
                                side=decision.side,
                                price=float(decision.price or mid),
                                size=decision.size,
                                position_size=pos,
                                entry_price=entry_px_for_pnl,
                            )
                            if pnl is not None:
                                item["estimated_pnl_usd"] = pnl
                        result = client.place_limit_order(plan)
                        oid = _extract_resting_oid(result)
                        accepted = _is_order_accepted(result)
                        if accepted:
                            risk.record_order()
                        elif isinstance(strategy, (PureGridStrategy, AdaptiveGridStrategy)):
                            strategy.clear_pending_order()
                        item.update({"sent": accepted, "submitted": True, "oid": oid, "result": result})
                        order_error = _extract_order_error(result)
                        if order_error:
                            item["order_error"] = order_error
                        msg = order_notification(item, strategy_config.coin) if accepted else None
                        if msg:
                            try:
                                item["notified"] = notifier.send_text(msg)
                            except Exception as notify_exc:  # noqa: BLE001 - notification must not stop trading loop
                                item["notify_error"] = str(notify_exc)
                        if accepted:
                            trade_logger.write("trade", {
                                "coin": strategy_config.coin,
                                "side": decision.side,
                                "size": decision.size,
                                "price": float(decision.price or mid),
                                "entry_px": entry_px_for_pnl,
                                "oid": oid,
                                "position_before": pos,
                                "equity": perp_equity,
                                "reduce_only": decision.reduce_only,
                                "estimated_pnl_usd": item.get("estimated_pnl_usd"),
                            })
                        # 全 Ioc 模式: 订单立即成交/取消, 无需等待+手动 cancel
                        # 全 Ioc 模式跳过超时取消; 保留这个条件以防以后切回 Alo
                        if oid and daemon_config.order_live_seconds > 0 and order_tif == "Alo":
                            time.sleep(daemon_config.order_live_seconds)
                            item["cancel"] = client.cancel(strategy_config.coin, oid)

                            # After cancel, notify if order was not filled
                            # (fill notifications are handled by the tid-based check at iteration start)
                            try:
                                recent_fills = client.user_fills()
                                matching_fills = [f for f in recent_fills if int(f.get("oid", 0)) == oid]
                                if matching_fills:
                                    item["filled_before_cancel"] = len(matching_fills)
                                else:
                                    item["cancelled_empty"] = True
                                    cancel_msg = cancel_notification(
                                        coin=strategy_config.coin,
                                        oid=oid,
                                        reason=decision.reason,
                                        equity=item.get("equity"),
                                        total_equity=item.get("total_equity"),
                                    )
                                    if cancel_msg:
                                        try:
                                            item["cancel_notified"] = notifier.send_text(cancel_msg)
                                        except Exception as notify_exc:
                                            item["notify_error"] = str(notify_exc)
                            except Exception as fill_exc:
                                item["fill_query_error"] = str(fill_exc)

            logger.write("daemon_iteration", item)
            summary["iterations"].append(item)
            _write_state(daemon_config.heartbeat_path, state_payload(item))
            i += 1
            # Recovery notification when we come back from an error streak
            if consecutive_errors >= 3 and _last_alerted_error_level > 0:
                _last_alerted_error_level = 0
                try:
                    notifier.send_text(f"✅ {daemon_config.coin} daemon recovered after {consecutive_errors} errors")
                except Exception:
                    pass
            consecutive_errors = 0  # reset on any successful iteration
            if daemon_config.iterations <= 0 or i < daemon_config.iterations:
                time.sleep(daemon_config.sleep_seconds)
        except KeyboardInterrupt:
            item["stopped"] = "keyboard_interrupt"
            if not config.dry_run and config.risk.emergency_cancel_on_error:
                item["emergency_cancel"] = client.cancel_all()
            logger.write("daemon_stop", item)
            summary["iterations"].append(item)
            break
        except Exception as exc:  # noqa: BLE001 - keep daemon alive, but fail safe on errors/timeouts
            consecutive_errors += 1
            item.update({"error": str(exc), "type": exc.__class__.__name__})
            item["consecutive_errors"] = consecutive_errors
            # Proactive escalation notification at key error milestones
            coin_name = daemon_config.coin
            _alert_levels = {5, 15, 30, 60, 120, 250, 500}
            if consecutive_errors in _alert_levels and consecutive_errors > _last_alerted_error_level:
                _last_alerted_error_level = consecutive_errors
                try:
                    exc_head = str(exc)[:120]
                    notifier.send_text(f"⚠️ {coin_name} daemon alert: {consecutive_errors} errors in a row. Last: {exc_head}")
                except Exception:
                    pass
            if not config.dry_run and config.risk.emergency_cancel_on_error:
                try:
                    item["emergency_cancel"] = client.cancel_all()
                except Exception as cancel_exc:  # noqa: BLE001
                    item["emergency_cancel_error"] = str(cancel_exc)
            logger.write("daemon_error", item)
            summary["iterations"].append(item)
            _write_state(daemon_config.heartbeat_path, state_payload(item))
            i += 1

            # Reset the HTTP client after sustained failures (e.g. overnight router reboot)
            if consecutive_errors >= _CLIENT_RESET_THRESHOLD:
                item["reset_client"] = True
                try:
                    client.close()
                except Exception:
                    pass
                client = HyperliquidClient(config)
                if mids:
                    # Clear stale mid data collected before the outage so the strategy
                    # warms up with fresh prices instead of mixing stale + new data.
                    mids.clear()

            # Exponential backoff: 30s, 60s, 120s, 240s, max 300s
            if consecutive_errors > _MAX_CONSECUTIVE_ERRORS:
                backoff = min(_BACKOFF_MIN * (2 ** (consecutive_errors - _MAX_CONSECUTIVE_ERRORS)), _BACKOFF_MAX)
                item["backoff_seconds"] = backoff
                _write_state(daemon_config.heartbeat_path, state_payload(item))
                time.sleep(backoff)
            else:
                _write_state(daemon_config.heartbeat_path, state_payload(item))
                time.sleep(daemon_config.sleep_seconds)

    summary["final_open_orders"] = client.open_orders()
    summary["final_account"] = client.user_state()
    return summary
