from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from ._utils import extract_resting_oid, spread_bps as _sb
from .backtest import optimize_strategy, run_backtest
from .client import HyperliquidClient
from .config import load_config
from .daemon import DaemonConfig, run_daemon
from .logging import TradeLogger
from .risk import RiskManager
from .strategy import HybridStrategy, StrategyConfig, position_size_from_user_state
from .strategy_v2 import PureGridConfig
from .strategy_v3 import AdaptiveGridConfig


def print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False))


def _perp_usdc(state: dict[str, Any]) -> float:
    return float(state.get("marginSummary", {}).get("accountValue", "0") or 0)


def _spot_usdc(state: dict[str, Any]) -> float:
    for balance in state.get("balances", []):
        if str(balance.get("coin", "")).upper() == "USDC":
            return float(balance.get("total", "0") or 0)
    return 0.0


def _extract_resting_oid(result: Any) -> int | None:
    return extract_resting_oid(result)


def _require_live(risk: RiskManager) -> None:
    live = risk.validate_live_enabled()
    if not live.ok:
        raise RuntimeError(live.reason)


def _spread_bps(book: dict[str, Any]) -> float | None:
    return _sb(book)


def _cancel_all_best_effort(client: HyperliquidClient) -> list[Any]:
    try:
        return client.cancel_all()
    except Exception as exc:  # noqa: BLE001 - best effort emergency cleanup
        return [{"error": str(exc), "type": exc.__class__.__name__}]


def cmd_status(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    print_json({
        "environment": cfg.environment,
        "base_url": cfg.base_url,
        "ws_url": cfg.ws_url,
        "dry_run": cfg.dry_run,
        "wallet_address_set": bool(cfg.wallet.address),
        "private_key_set": bool(cfg.wallet.private_key),
        "allowed_coins": cfg.risk.allowed_coins,
        "max_order_notional_usd": cfg.risk.max_order_notional_usd,
        "max_daily_orders": cfg.risk.max_daily_orders,
        "max_position_size": cfg.risk.max_position_size,
        "max_position_notional_usd": cfg.risk.max_position_notional_usd,
    })
    return 0


def cmd_mids(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    client = HyperliquidClient(cfg)
    mids = client.all_mids()
    if args.coins:
        wanted = {c.upper() for c in args.coins}
        mids = {k: v for k, v in mids.items() if k.upper() in wanted}
    print_json(mids)
    return 0


def cmd_meta(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    client = HyperliquidClient(cfg)
    meta = client.meta()
    if args.coins_only:
        print_json([item.get("name") for item in meta.get("universe", [])])
    else:
        print_json(meta)
    return 0


def cmd_account(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    client = HyperliquidClient(cfg)
    print_json(client.user_state(args.address))
    return 0


def cmd_spot_account(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    client = HyperliquidClient(cfg)
    print_json(client.spot_user_state(args.address))
    return 0


def cmd_open_orders(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    client = HyperliquidClient(cfg)
    print_json(client.open_orders(args.address))
    return 0


def _plan_order(args: argparse.Namespace):
    cfg = load_config(args.config)
    client = HyperliquidClient(cfg)
    risk = RiskManager(cfg)
    decision = risk.validate_order(
        coin=args.coin,
        side=args.side,
        size=args.size,
        price=args.price,
        reduce_only=args.reduce_only,
    )
    if not decision.ok:
        raise SystemExit(f"risk rejected order: {decision.reason}")
    plan = client.build_limit_order(
        coin=args.coin,
        side=args.side,
        size=args.size,
        price=args.price,
        reduce_only=args.reduce_only,
        tif=args.tif,
        cloid=args.cloid,
    )
    return cfg, client, risk, plan


def cmd_plan_order(args: argparse.Namespace) -> int:
    cfg, _client, _risk, plan = _plan_order(args)
    print_json({
        "environment": cfg.environment,
        "dry_run": cfg.dry_run,
        "action": {"type": "order", "orders": [plan.to_wire()], "grouping": "na"},
        "risk": "accepted",
    })
    return 0


def cmd_place_order(args: argparse.Namespace) -> int:
    cfg, client, risk, plan = _plan_order(args)
    live = risk.validate_live_enabled()
    if not live.ok:
        print_json({
            "sent": False,
            "reason": live.reason,
            "would_send": {"type": "order", "orders": [plan.to_wire()], "grouping": "na"},
        })
        return 2
    result = client.place_limit_order(plan)
    risk.record_order()
    print_json({"sent": True, "result": result})
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    risk = RiskManager(cfg)
    live = risk.validate_live_enabled()
    if not live.ok:
        print_json({"sent": False, "reason": live.reason, "would_cancel": {"coin": args.coin.upper(), "oid": args.oid}})
        return 2
    client = HyperliquidClient(cfg)
    print_json({"sent": True, "result": client.cancel(args.coin, args.oid)})
    return 0


def cmd_cancel_all(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    risk = RiskManager(cfg)
    live = risk.validate_live_enabled()
    client = HyperliquidClient(cfg)
    orders = client.open_orders()
    if not live.ok:
        print_json({"sent": False, "reason": live.reason, "would_cancel": orders})
        return 2
    print_json({"sent": True, "result": client.cancel_all()})
    return 0


def cmd_usd_class_transfer(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    risk = RiskManager(cfg)
    live = risk.validate_live_enabled()
    to_perp = args.to == "perp"
    if args.amount <= 0:
        raise SystemExit("amount must be positive")
    if not live.ok:
        print_json({
            "sent": False,
            "reason": live.reason,
            "would_transfer": {"amount": args.amount, "toPerp": to_perp},
        })
        return 2
    client = HyperliquidClient(cfg)
    print_json({"sent": True, "result": client.usd_class_transfer(args.amount, to_perp)})
    return 0


def cmd_smoke_test(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    client = HyperliquidClient(cfg)
    risk = RiskManager(cfg)
    summary: dict[str, Any] = {
        "environment": cfg.environment,
        "dry_run": cfg.dry_run,
        "coin": args.coin.upper(),
        "steps": [],
    }

    if cfg.environment != "testnet":
        raise RuntimeError("smoke-test is only allowed on testnet")

    spot_before = client.spot_user_state()
    perp_before = client.user_state()
    spot_usdc_before = _spot_usdc(spot_before)
    perp_usdc_before = _perp_usdc(perp_before)
    summary["spot_usdc_before"] = spot_usdc_before
    summary["perp_usdc_before"] = perp_usdc_before

    if args.transfer_amount > 0 and perp_usdc_before < args.min_perp_usdc:
        if spot_usdc_before < args.transfer_amount:
            raise RuntimeError(f"not enough spot USDC to transfer: {spot_usdc_before} < {args.transfer_amount}")
        if cfg.dry_run:
            summary["steps"].append({"sent": False, "action": "usdClassTransfer", "amount": args.transfer_amount, "toPerp": True, "reason": "dry_run"})
        else:
            _require_live(risk)
            result = client.usd_class_transfer(args.transfer_amount, True)
            summary["steps"].append({"sent": True, "action": "usdClassTransfer", "result": result})
            time.sleep(args.settle_seconds)

    mids = client.all_mids()
    mid = float(mids[args.coin.upper()])
    price = args.price if args.price is not None else round(mid * (1 - args.distance_bps / 10000), 2)
    decision = risk.validate_order(coin=args.coin, side="buy", size=args.size, price=price, reduce_only=False)
    if not decision.ok:
        raise RuntimeError(f"risk rejected smoke order: {decision.reason}")
    plan = client.build_limit_order(coin=args.coin, side="buy", size=args.size, price=price, tif="Alo")
    summary["planned_order"] = {"mid": mid, "price": price, "wire": plan.to_wire()}

    oid = None
    if cfg.dry_run:
        summary["steps"].append({"sent": False, "action": "order", "reason": "dry_run", "would_send": plan.to_wire()})
    else:
        _require_live(risk)
        result = client.place_limit_order(plan)
        risk.record_order()
        oid = _extract_resting_oid(result)
        summary["steps"].append({"sent": True, "action": "order", "oid": oid, "result": result})
        time.sleep(args.settle_seconds)

    if oid is not None:
        try:
            cancel_result = client.cancel(args.coin, oid)
            summary["steps"].append({"sent": True, "action": "cancel", "oid": oid, "result": cancel_result})
        finally:
            time.sleep(args.settle_seconds)

    summary["open_orders_after"] = client.open_orders()
    summary["perp_after"] = client.user_state()
    summary["spot_after"] = client.spot_user_state()
    print_json(summary)
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    client = HyperliquidClient(cfg)
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - args.hours * 60 * 60 * 1000
    candles = client.candles_snapshot(args.coin, args.interval, start_ms, now_ms)
    closes = [float(c["c"]) for c in candles]
    strat_cfg = StrategyConfig(
        coin=args.coin,
        size=args.size,
        lookback=args.lookback,
        entry_bps=args.entry_bps,
        exit_bps=args.exit_bps,
        maker_offset_bps=args.maker_offset_bps,
        fast_lookback=args.fast_lookback,
        slow_lookback=args.slow_lookback,
        trend_entry_bps=args.trend_entry_bps,
        trend_exit_bps=args.trend_exit_bps,
        min_vol_bps=args.min_vol_bps,
        max_vol_bps=args.max_vol_bps,
        mode=args.mode,
        grid_levels=args.grid_levels,
        grid_spacing_bps=args.grid_spacing_bps,
        grid_take_profit_bps=args.grid_take_profit_bps,
        grid_stop_bps=args.grid_stop_bps,
        inventory_skew_bps=args.inventory_skew_bps,
    )
    result = run_backtest(closes, strat_cfg)
    print_json({**result.__dict__, "note": "rough signal sanity check only; not a profitability guarantee"})
    return 0


def cmd_optimize(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    client = HyperliquidClient(cfg)
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - args.hours * 60 * 60 * 1000
    candles = client.candles_snapshot(args.coin, args.interval, start_ms, now_ms)
    closes = [float(c["c"]) for c in candles]
    results = optimize_strategy(closes, args.coin.upper(), args.size)
    top = [r.__dict__ for r in results[: args.top]]
    out = {
        "coin": args.coin.upper(),
        "interval": args.interval,
        "hours": args.hours,
        "candidates": len(results),
        "top": top,
        "recommendation": top[0]["config"] if top else None,
        "note": "Use only as parameter screening. Forward test on testnet before real funds.",
    }
    if args.write and top:
        Path(args.write).write_text(json.dumps(out, indent=2, ensure_ascii=False, sort_keys=True))
    print_json(out)
    return 0


def cmd_run_strategy(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    client = HyperliquidClient(cfg)
    risk = RiskManager(cfg)
    logger = TradeLogger(Path(args.log))
    strat = HybridStrategy(StrategyConfig(
        coin=args.coin,
        size=args.size,
        lookback=args.lookback,
        entry_bps=args.entry_bps,
        exit_bps=args.exit_bps,
        maker_offset_bps=args.maker_offset_bps,
        fast_lookback=args.fast_lookback,
        slow_lookback=args.slow_lookback,
        trend_entry_bps=args.trend_entry_bps,
        trend_exit_bps=args.trend_exit_bps,
        min_vol_bps=args.min_vol_bps,
        max_vol_bps=args.max_vol_bps,
        mode=args.mode,
        grid_levels=args.grid_levels,
        grid_spacing_bps=args.grid_spacing_bps,
        grid_take_profit_bps=args.grid_take_profit_bps,
        grid_stop_bps=args.grid_stop_bps,
        inventory_skew_bps=args.inventory_skew_bps,
    ))
    summary: dict[str, Any] = {"environment": cfg.environment, "dry_run": cfg.dry_run, "coin": args.coin.upper(), "iterations": []}
    if cfg.environment != "testnet":
        raise RuntimeError("run-strategy v1 is only allowed on testnet")

    mids: list[float] = []
    for i in range(args.iterations):
        try:
            mid_map = client.all_mids()
            mid = float(mid_map[args.coin.upper()])
            mids.append(mid)
            user_state = client.user_state()
            pos = position_size_from_user_state(user_state, args.coin)
            spread = _spread_bps(client.l2_snapshot(args.coin))
            decision = strat.decide(mids=mids, position_size=pos)
            item: dict[str, Any] = {"i": i, "mid": mid, "position": pos, "spread_bps": spread, "decision": decision.__dict__}

            if spread is None or spread > cfg.risk.max_spread_bps:
                item["skipped"] = f"spread {spread} exceeds max_spread_bps {cfg.risk.max_spread_bps}"
            elif decision.side == "hold":
                item["skipped"] = decision.reason
            else:
                risk_decision = risk.validate_order(
                    coin=args.coin,
                    side=decision.side,
                    size=decision.size,
                    price=float(decision.price or mid),
                    reduce_only=decision.reduce_only,
                    current_position_size=pos,
                )
                if not risk_decision.ok:
                    item["skipped"] = f"risk: {risk_decision.reason}"
                else:
                    plan = client.build_limit_order(
                        coin=args.coin,
                        side=decision.side,
                        size=decision.size,
                        price=decision.price or mid,
                        reduce_only=decision.reduce_only,
                        tif="Alo",
                    )
                    if cfg.dry_run:
                        item["sent"] = False
                        item["would_send"] = plan.to_wire()
                    else:
                        _require_live(risk)
                        result = client.place_limit_order(plan)
                        risk.record_order()
                        item["sent"] = True
                        item["result"] = result
                        # v1 is signal/execution validation; do not leave unattended maker orders around.
                        oid = _extract_resting_oid(result)
                        if oid:
                            time.sleep(args.order_live_seconds)
                            item["cancel"] = client.cancel(args.coin, oid)
            logger.write("strategy_iteration", item)
            summary["iterations"].append(item)
            if i + 1 < args.iterations:
                time.sleep(args.sleep_seconds)
        except Exception as exc:  # noqa: BLE001 - command must fail safe
            err = {"i": i, "error": str(exc), "type": exc.__class__.__name__}
            if cfg.risk.emergency_cancel_on_error and not cfg.dry_run:
                err["emergency_cancel"] = _cancel_all_best_effort(client)
            logger.write("strategy_error", err)
            summary["iterations"].append(err)
            raise

    summary["final_open_orders"] = client.open_orders()
    summary["final_account"] = client.user_state()
    print_json(summary)
    return 0

def cmd_daemon(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if getattr(args, 'mode', None) == 'pure_grid':
        strategy_config = PureGridConfig(
            coin=args.coin,
            base_size=args.size,
            lookback=args.lookback,
            grid_levels=args.grid_levels,
            grid_spacing_bps=args.grid_spacing_bps,
            first_entry_bps=args.first_entry_bps,
            take_profit_bps=args.take_profit_bps,
            stop_loss_bps=args.stop_loss_bps,
            min_vol_bps=args.min_vol_bps,
            max_vol_bps=args.max_vol_bps,
            maker_offset_bps=args.maker_offset_bps,
            max_consecutive_losses=args.max_consecutive_losses,
            cooloff_rounds=args.cooloff_rounds,
            trend_slope_bps=args.trend_slope_bps,
            order_timeout_rounds=args.order_timeout_rounds,
        )
    elif getattr(args, 'mode', None) == 'adaptive_grid':
        strategy_config = AdaptiveGridConfig(
            coin=args.coin,
            base_size=args.size,
            lookback=args.lookback,
            grid_levels=args.grid_levels,
            grid_spacing_bps=args.grid_spacing_bps,
            entry_bps=args.entry_bps,
            take_profit_bps=args.take_profit_bps,
            stop_loss_bps=args.stop_loss_bps,
            min_vol_bps=args.min_vol_bps,
            max_vol_bps=args.max_vol_bps,
            maker_offset_bps=args.maker_offset_bps,
            max_consecutive_losses=args.max_consecutive_losses,
            cooloff_rounds=args.cooloff_rounds,
            order_timeout_rounds=args.order_timeout_rounds,
            trend_block_bps=args.trend_block_bps,
        )
    else:
        strategy_config = StrategyConfig(
            coin=args.coin,
            size=args.size,
            lookback=args.lookback,
            fast_lookback=args.fast_lookback,
            slow_lookback=args.slow_lookback,
            entry_bps=args.entry_bps,
            exit_bps=args.exit_bps,
            trend_entry_bps=args.trend_entry_bps,
            trend_exit_bps=args.trend_exit_bps,
            maker_offset_bps=args.maker_offset_bps,
            min_vol_bps=args.min_vol_bps,
            max_vol_bps=args.max_vol_bps,
            mode=args.mode,
            grid_levels=args.grid_levels,
            grid_spacing_bps=args.grid_spacing_bps,
            grid_take_profit_bps=args.grid_take_profit_bps,
            grid_stop_bps=args.grid_stop_bps,
            inventory_skew_bps=args.inventory_skew_bps,
        )
    log_path = Path(args.log)
    trade_log_path = Path(args.trade_log) if args.trade_log else Path(f"{log_path.stem}_history{log_path.suffix}")
    daemon_config = DaemonConfig(
        coin=args.coin,
        iterations=args.iterations,
        sleep_seconds=args.sleep_seconds,
        order_live_seconds=args.order_live_seconds,
        log_path=log_path,
        trade_log_path=trade_log_path,
        heartbeat_path=Path(args.state),
    )
    print_json(run_daemon(cfg, strategy_config, daemon_config))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hlbot", description="Safe Hyperliquid automation CLI")
    parser.add_argument("--config", default="config.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("status")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("mids")
    p.add_argument("--coins", nargs="*")
    p.set_defaults(func=cmd_mids)

    p = sub.add_parser("meta")
    p.add_argument("--coins-only", action="store_true")
    p.set_defaults(func=cmd_meta)

    p = sub.add_parser("account")
    p.add_argument("--address")
    p.set_defaults(func=cmd_account)

    p = sub.add_parser("spot-account")
    p.add_argument("--address")
    p.set_defaults(func=cmd_spot_account)

    p = sub.add_parser("open-orders")
    p.add_argument("--address")
    p.set_defaults(func=cmd_open_orders)

    for name, func in [("plan-order", cmd_plan_order), ("place-order", cmd_place_order)]:
        p = sub.add_parser(name)
        p.add_argument("--coin", required=True)
        p.add_argument("--side", choices=["buy", "sell"], required=True)
        p.add_argument("--size", type=float, required=True)
        p.add_argument("--price", type=float, required=True)
        p.add_argument("--reduce-only", action="store_true")
        p.add_argument("--tif", choices=["Alo", "Ioc", "Gtc"])
        p.add_argument("--cloid")
        p.set_defaults(func=func)

    p = sub.add_parser("cancel")
    p.add_argument("--coin", required=True)
    p.add_argument("--oid", type=int, required=True)
    p.set_defaults(func=cmd_cancel)

    p = sub.add_parser("cancel-all")
    p.set_defaults(func=cmd_cancel_all)

    p = sub.add_parser("usd-class-transfer")
    p.add_argument("--amount", type=float, required=True)
    p.add_argument("--to", choices=["perp", "spot"], required=True)
    p.set_defaults(func=cmd_usd_class_transfer)

    p = sub.add_parser("smoke-test")
    p.add_argument("--coin", default="ETH")
    p.add_argument("--size", type=float, default=0.005)
    p.add_argument("--price", type=float)
    p.add_argument("--distance-bps", type=float, default=500)
    p.add_argument("--transfer-amount", type=float, default=15)
    p.add_argument("--min-perp-usdc", type=float, default=10)
    p.add_argument("--settle-seconds", type=float, default=2)
    p.set_defaults(func=cmd_smoke_test)

    p = sub.add_parser("backtest")
    p.add_argument("--coin", default="ETH")
    p.add_argument("--interval", default="15m")
    p.add_argument("--hours", type=int, default=72)
    p.add_argument("--size", type=float, default=0.005)
    p.add_argument("--lookback", type=int, default=20)
    p.add_argument("--fast-lookback", type=int, default=8)
    p.add_argument("--slow-lookback", type=int, default=32)
    p.add_argument("--entry-bps", type=float, default=8)
    p.add_argument("--exit-bps", type=float, default=3)
    p.add_argument("--trend-entry-bps", type=float, default=6)
    p.add_argument("--trend-exit-bps", type=float, default=2)
    p.add_argument("--maker-offset-bps", type=float, default=2)
    p.add_argument("--min-vol-bps", type=float, default=2)
    p.add_argument("--max-vol-bps", type=float, default=80)
    p.add_argument("--grid-levels", type=int, default=3)
    p.add_argument("--grid-spacing-bps", type=float, default=12)
    p.add_argument("--grid-take-profit-bps", type=float, default=10)
    p.add_argument("--grid-stop-bps", type=float, default=55)
    p.add_argument("--inventory-skew-bps", type=float, default=4)
    p.add_argument("--mode", choices=["mean_reversion", "trend", "hybrid", "grid"], default="hybrid")
    p.set_defaults(func=cmd_backtest)

    p = sub.add_parser("optimize")
    p.add_argument("--coin", default="ETH")
    p.add_argument("--interval", default="15m")
    p.add_argument("--hours", type=int, default=168)
    p.add_argument("--size", type=float, default=0.005)
    p.add_argument("--top", type=int, default=5)
    p.add_argument("--write", default="strategy_report.json")
    p.set_defaults(func=cmd_optimize)

    p = sub.add_parser("run-strategy")
    p.add_argument("--coin", default="ETH")
    p.add_argument("--size", type=float, default=0.005)
    p.add_argument("--lookback", type=int, default=20)
    p.add_argument("--fast-lookback", type=int, default=8)
    p.add_argument("--slow-lookback", type=int, default=32)
    p.add_argument("--entry-bps", type=float, default=8)
    p.add_argument("--exit-bps", type=float, default=3)
    p.add_argument("--trend-entry-bps", type=float, default=6)
    p.add_argument("--trend-exit-bps", type=float, default=2)
    p.add_argument("--maker-offset-bps", type=float, default=2)
    p.add_argument("--min-vol-bps", type=float, default=2)
    p.add_argument("--max-vol-bps", type=float, default=80)
    p.add_argument("--grid-levels", type=int, default=3)
    p.add_argument("--grid-spacing-bps", type=float, default=12)
    p.add_argument("--grid-take-profit-bps", type=float, default=10)
    p.add_argument("--grid-stop-bps", type=float, default=55)
    p.add_argument("--inventory-skew-bps", type=float, default=4)
    p.add_argument("--mode", choices=["mean_reversion", "trend", "hybrid", "grid"], default="hybrid")
    p.add_argument("--iterations", type=int, default=5)
    p.add_argument("--sleep-seconds", type=float, default=10)
    p.add_argument("--order-live-seconds", type=float, default=2)
    p.add_argument("--log", default="trades.jsonl")
    p.set_defaults(func=cmd_run_strategy)

    p = sub.add_parser("daemon")
    p.add_argument("--coin", default="ETH")
    p.add_argument("--size", type=float, default=0.005)
    p.add_argument("--lookback", type=int, default=20)
    p.add_argument("--fast-lookback", type=int, default=8)
    p.add_argument("--slow-lookback", type=int, default=32)
    p.add_argument("--entry-bps", type=float, default=8)
    p.add_argument("--exit-bps", type=float, default=3)
    p.add_argument("--trend-entry-bps", type=float, default=6)
    p.add_argument("--trend-exit-bps", type=float, default=2)
    p.add_argument("--maker-offset-bps", type=float, default=2)
    p.add_argument("--min-vol-bps", type=float, default=2)
    p.add_argument("--max-vol-bps", type=float, default=80)
    p.add_argument("--grid-levels", type=int, default=3)
    p.add_argument("--grid-spacing-bps", type=float, default=12)
    p.add_argument("--grid-take-profit-bps", type=float, default=10)
    p.add_argument("--grid-stop-bps", type=float, default=55)
    p.add_argument("--inventory-skew-bps", type=float, default=4)
    p.add_argument("--mode", choices=["mean_reversion", "trend", "hybrid", "grid", "pure_grid", "adaptive_grid"], default="hybrid")
    # pure_grid 专属参数
    p.add_argument("--first-entry-bps", type=float, default=15, help="pure_grid: first grid level entry deviation")
    p.add_argument("--take-profit-bps", type=float, default=28, help="pure_grid: per-level take profit")
    p.add_argument("--stop-loss-bps", type=float, default=40, help="pure_grid: per-level stop loss")
    p.add_argument("--max-consecutive-losses", type=int, default=3, help="pure_grid: cool-off after N losses")
    p.add_argument("--cooloff-rounds", type=int, default=30, help="pure_grid: cool-off duration in iterations")
    p.add_argument("--order-timeout-rounds", type=int, default=12, help="pure_grid: cancel unfilled orders after N checks")
    p.add_argument("--trend-block-bps", type=float, default=0.5, help="adaptive_grid: block counter-trend if slope exceeds this (bps/iter)")
    p.add_argument("--trend-slope-bps", type=float, default=20, help="pure_grid: downtrend filter threshold (bps)")
    p.add_argument("--iterations", type=int, default=0, help="0 means run forever")
    p.add_argument("--sleep-seconds", type=float, default=60)
    p.add_argument("--order-live-seconds", type=float, default=5)
    p.add_argument("--log", default="trades.jsonl")
    p.add_argument("--trade-log", default=None, help="separate file for trade-only records (default: <log>_history.jsonl)")
    p.add_argument("--state", default="daemon_state.json")
    p.set_defaults(func=cmd_daemon)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as exc:
        print_json({"error": str(exc), "type": exc.__class__.__name__})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
