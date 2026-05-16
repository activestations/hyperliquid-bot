from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """HTTPServer that handles requests in separate threads."""
    daemon_threads = True
    allow_reuse_address = True
    block_on_close = False

from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .client import HyperliquidClient
from .config import load_config

COINS = ("BTC", "ETH", "SOL")
ROOT = Path.cwd()
START_TS = time.time()


def _json_file(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "type": exc.__class__.__name__}


def _tail_jsonl(path: Path, limit: int = 200) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    lines = path.read_text(errors="replace").splitlines()[-limit:]
    out: list[dict[str, Any]] = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


# ── VHL (paper) helpers ──────────────────────────────────────────

VHL_COINS = ("BTC", "ETH", "SOL")


def _vhl_state(coin: str) -> dict[str, Any] | None:
    return _json_file(ROOT / f"paper_state_{coin}.json")


def _vhl_trades(coin: str, limit: int = 20) -> list[dict[str, Any]]:
    return _tail_jsonl(ROOT / f"paper_{coin}.jsonl", limit)


def _vhl_pid(coin: str) -> dict[str, Any]:
    pid_path = ROOT / "pids" / f"paper_{coin}.pid"
    try:
        pid = int(pid_path.read_text().strip())
    except Exception:
        return {"pid": None, "alive": False}
    return {"pid": pid, "alive": _pid_alive(pid)}


def collect_vhl_status() -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": True,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "coins": {},
        "aggregate": {},
    }
    total_realized = 0.0
    total_unrealized = 0.0
    total_fees = 0.0
    total_balance = 0.0
    initial_total = 3000.0
    for coin in VHL_COINS:
        s = _vhl_state(coin)
        tr = _vhl_trades(coin, 5)
        pid = _vhl_pid(coin)
        if s is None:
            payload["coins"][coin] = {"status": "warming_up", "process": pid}
            continue
        mid = s.get("mid")
        pos = s.get("position", 0)
        entry = s.get("entry_price")
        bal = s.get("balance", 0)
        rpnl = s.get("realized_pnl", 0)
        upnl = s.get("unrealized_pnl", 0)
        fees = s.get("total_fees", 0)
        vol = s.get("total_volume", 0)
        net = rpnl + upnl
        total_realized += rpnl
        total_unrealized += upnl
        total_fees += fees
        total_balance += bal
        payload["coins"][coin] = {
            "process": pid,
            "mid": mid,
            "position": round(pos, 6),
            "entry_price": entry,
            "balance": round(bal, 2),
            "realized_pnl": round(rpnl, 4),
            "unrealized_pnl": round(upnl, 4),
            "net_pnl": round(net, 4),
            "total_fees": round(fees, 4),
            "total_volume": round(vol, 1),
            "direction": s.get("direction"),
            "level_index": s.get("level_index"),
            "mids_count": s.get("mids_count"),
            "cooloff_remaining": s.get("cooloff_remaining"),
            "recent_trades": len(tr),
        }
        # last trade
        if tr:
            payload["coins"][coin]["last_event"] = tr[-1].get("event") or tr[-1].get("decision_side")
    total_net = total_realized + total_unrealized
    initial_total = 3000.0  # 1000 per coin
    payload["aggregate"] = {
        "initial_balance": initial_total,
        "current_balance": round(total_balance, 2),
        "total_realized_pnl": round(total_realized, 4),
        "total_unrealized_pnl": round(total_unrealized, 4),
        "total_net_pnl": round(total_net, 4),
        "total_fees": round(total_fees, 4),
        "roi_percent": round(total_net / initial_total * 100, 4) if initial_total else 0,
    }
    return payload


def _systemctl(*args: str) -> str:
    try:
        return subprocess.check_output(["systemctl", "--user", *args], text=True, stderr=subprocess.STDOUT, timeout=5).strip()
    except Exception as exc:  # noqa: BLE001
        return f"error: {exc}"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _coin_process(coin: str) -> dict[str, Any]:
    pid_path = ROOT / "pids" / f"{coin}.pid"
    try:
        pid = int(pid_path.read_text().strip())
    except Exception:
        return {"pid": None, "alive": False}
    return {"pid": pid, "alive": _pid_alive(pid)}


def _extract_perp_equity(user_state: dict[str, Any] | None) -> float | None:
    if not user_state:
        return None
    try:
        return float(user_state.get("marginSummary", {}).get("accountValue", "0") or 0)
    except Exception:
        return None


def _extract_spot_usdc(spot_state: dict[str, Any] | None) -> float | None:
    if not spot_state:
        return None
    try:
        for balance in spot_state.get("balances", []):
            if str(balance.get("coin", "")).upper() == "USDC":
                return float(balance.get("total", "0") or 0)
    except Exception:
        return None
    return 0.0


def _extract_positions(user_state: dict[str, Any] | None) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if not user_state:
        return result
    for item in user_state.get("assetPositions", []):
        pos = item.get("position", {})
        coin = str(pos.get("coin", "")).upper()
        if coin:
            result[coin] = {
                "size": float(pos.get("szi", "0") or 0),
                "entry_px": pos.get("entryPx"),
                "unrealized_pnl": pos.get("unrealizedPnl"),
            }
    return result


# ── Kill-switch helpers ────────────────────────────────────────

SENTINEL_ALL = ROOT / ".stop.ALL"


def _sentinel_path(coin: str) -> Path:
    return ROOT / f".stop.{coin}"


def _write_sentinels() -> None:
    """Write .stop sentinel files for all coins + ALL."""
    SENTINEL_ALL.write_text("stopped")
    for coin in COINS:
        _sentinel_path(coin).write_text("stopped")


def _remove_sentinels() -> None:
    """Remove all .stop sentinel files."""
    SENTINEL_ALL.unlink(missing_ok=True)
    for coin in COINS:
        _sentinel_path(coin).unlink(missing_ok=True)


def _kill_by_pidfile(pid_path: Path) -> dict[str, Any]:
    """Kill a process by PID file. Return kill result."""
    try:
        pid = int(pid_path.read_text().strip())
    except (FileNotFoundError, ValueError, OSError):
        return {"pid": None, "killed": False, "reason": "no pid file"}
    try:
        # SIGTERM first
        os.kill(pid, 15)
        # Give it a moment, then SIGKILL if still alive
        for _ in range(5):
            time.sleep(0.2)
            try:
                os.kill(pid, 0)
            except OSError:
                return {"pid": pid, "killed": True, "signal": 15}
        os.kill(pid, 9)
        return {"pid": pid, "killed": True, "signal": 9}
    except OSError as exc:
        return {"pid": pid, "killed": False, "error": str(exc)}


def _kill_supervised_processes() -> list[dict[str, Any]]:
    """Kill run_live_supervised.sh and python daemon processes.
    Uses PID files first, then falls back to process name matching.
    """
    results: list[dict[str, Any]] = []
    # 1. Kill by PID files
    for coin in COINS:
        pid_path = ROOT / "pids" / f"{coin}.pid"
        results.append({coin: _kill_by_pidfile(pid_path)})
    # 2. Kill any hlbot daemon processes (regardless of PID files)
    killed = []
    try:
        import signal as _sig
        lines = subprocess.check_output(
            ["pgrep", "-af", "hlbot.cli.*daemon"],
            text=True, timeout=5, stderr=subprocess.STDOUT,
        ).strip().splitlines()
        for line in lines:
            pid_str = line.split()[0] if line else ""
            if pid_str and pid_str.isdigit():
                pid = int(pid_str)
                os.kill(pid, _sig.SIGTERM)
                killed.append(pid)
    except (subprocess.CalledProcessError, OSError, ValueError):
        pass
    # 3. Also kill run_live_supervised.sh processes
    try:
        lines = subprocess.check_output(
            ["pgrep", "-af", "run_live_supervised"],
            text=True, timeout=5, stderr=subprocess.STDOUT,
        ).strip().splitlines()
        for line in lines:
            pid_str = line.split()[0] if line else ""
            if pid_str and pid_str.isdigit():
                pid = int(pid_str)
                os.kill(pid, _sig.SIGTERM)
                killed.append(pid)
    except (subprocess.CalledProcessError, OSError, ValueError):
        pass
    if killed:
        results.append({"pgrep_fallback_killed": killed})
    return results


def _start_daemons() -> list[dict[str, Any]]:
    """Start the multi-coin daemon process via run_live_multi.sh."""
    results: list[dict[str, Any]] = []
    try:
        proc = subprocess.Popen(
            ["/usr/bin/env", "bash", str(ROOT / "run_live_multi.sh")],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        results.append({"started": True, "pid": proc.pid, "script": "run_live_multi.sh"})
    except OSError as exc:
        results.append({"started": False, "error": str(exc)})
    return results


def _stopped() -> bool:
    """Check if .stop.ALL sentinel exists (service is intentionally stopped)."""
    return SENTINEL_ALL.exists()


def collect_status(config_path: str, include_remote: bool = True) -> dict[str, Any]:
    now = time.time()
    stopped = _stopped()
    cfg = load_config(config_path)
    payload: dict[str, Any] = {
        "ok": True,
        "stopped": stopped,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "server_uptime_seconds": round(now - START_TS, 1),
        "service": {
            "name": "hyperliquid-bot.service",
            "active": _systemctl("is-active", "hyperliquid-bot.service"),
            "enabled": _systemctl("is-enabled", "hyperliquid-bot.service"),
        },
        "config": {
            "environment": cfg.environment,
            "dry_run": cfg.dry_run,
            "allowed_coins": cfg.risk.allowed_coins,
            "max_order_notional_usd": cfg.risk.max_order_notional_usd,
            "max_daily_orders": cfg.risk.max_daily_orders,
            "max_position_notional_usd": cfg.risk.max_position_notional_usd,
            "max_drawdown_usd": cfg.risk.max_drawdown_usd,
            "max_spread_bps": cfg.risk.max_spread_bps,
        },
        "coins": {},
    }

    open_orders: list[dict[str, Any]] = []
    positions: dict[str, Any] = {}
    user_state: dict[str, Any] | None = None
    if include_remote:
        try:
            client = HyperliquidClient(cfg, timeout=8.0)
            user_state = client.user_state()
            spot_state = client.spot_user_state()
            open_orders = client.open_orders()
            positions = _extract_positions(user_state)
            perp_equity = _extract_perp_equity(user_state) or 0.0
            spot_usdc = _extract_spot_usdc(spot_state) or 0.0
            payload["account"] = {
                "equity": perp_equity,
                "perp_equity": perp_equity,
                "total_equity": spot_usdc,
                "spot_usdc": spot_usdc,
                "open_orders_count": len(open_orders),
            }
        except Exception as exc:  # noqa: BLE001
            payload["ok"] = False
            payload["remote_error"] = {"type": exc.__class__.__name__, "message": str(exc)}

    for coin in COINS:
        state = _json_file(ROOT / f"daemon_state_{coin}.json") or {}
        last = state.get("last", {}) if isinstance(state, dict) else {}
        recent = _tail_jsonl(ROOT / f"trades_{coin}.jsonl", 300)
        # Only show errors from the *current* daemon process (stale errors from previous
        # daemon runs with the same coin are misleading). We match by comparing the error's
        # 'ts' timestamp against the daemon start timestamp recorded in state.
        daemon_start_ts = state.get("daemon_start_ts", "") if isinstance(state, dict) else ""
        last_error = None
        for r in reversed(recent):
            if r.get("event") == "daemon_error" or r.get("error"):
                # If the state has a start timestamp, skip entries before that time
                if daemon_start_ts:
                    err_ts = r.get("ts", "") or r.get("timestamp", "")
                    if err_ts and err_ts < daemon_start_ts:
                        continue  # stale error from a previous daemon lifecycle
                last_error = r
                break
        sent_count = sum(1 for r in recent if r.get("sent") or r.get("submitted") or r.get("oid"))
        coin_orders = [o for o in open_orders if str(o.get("coin", "")).upper() == coin]
        payload["coins"][coin] = {
            "process": _coin_process(coin),
            "sentinel_exists": _sentinel_path(coin).exists(),
            "daemon_start_ts": state.get("daemon_start_ts", "") if isinstance(state, dict) else "",
            "last_iteration": last.get("i"),
            "last_ts": last.get("ts"),
            "position": positions.get(coin, {}).get("size", last.get("position")),
            "entry_px": positions.get(coin, {}).get("entry_px", last.get("entry_px")),
            "unrealized_pnl": positions.get(coin, {}).get("unrealized_pnl"),
            "open_orders_count": len(coin_orders) if include_remote and not payload.get("remote_error") else last.get("open_orders_count"),
            "spread_bps": last.get("spread_bps"),
            "decision": last.get("decision"),
            "skipped": last.get("skipped"),
            "last_error": last_error,
            "recent_log_rows_sampled": len(recent),
            "recent_sent_or_submitted_sampled": sent_count,
        }
    return payload


class Handler(BaseHTTPRequestHandler):
    config_path = "config.live-testnet.yaml"
    token = ""

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.client_address[0]} - {fmt % args}")

    def _send(self, code: int, data: Any) -> None:
        body = json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "X-HLBOT-Token, Content-Type")
        self.send_header("Access-Control-Expose-Headers", "*")
        self.send_header("Connection", "keep-alive")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._send(204, {})

    def _authorized(self) -> bool:
        if not self.token:
            return True
        got = self.headers.get("X-HLBOT-Token") or ""
        if got == self.token:
            return True
        parsed = urlparse(self.path)
        return f"token={self.token}" in parsed.query.split("&")

    def _handle_action(self, action: str) -> None:
        """Handle a kill-switch action: stop or start."""
        if not self._authorized():
            self._send(401, {"error": "unauthorized", "hint": "send X-HLBOT-Token header or ?token="})
            return

        if action == "stop":
            # 1. Write sentinel files to prevent supervised scripts from restarting
            _write_sentinels()
            # 2. Kill all processes
            kill_results = _kill_supervised_processes()
            # 3. Record result
            self._send(200, {
                "action": "stop",
                "ok": True,
                "message": "HL service stopped. Sentinels set; auto-restart disabled.",
                "kill_results": kill_results,
                "sentinels": f"{SENTINEL_ALL.name} + {len(COINS)} per-coin files",
                "restart": "call POST /start or delete .stop.* files to re-enable",
            })
        elif action == "start":
            # 1. Remove sentinel files
            _remove_sentinels()
            # 2. Start daemons
            start_results = _start_daemons()
            self._send(200, {
                "action": "start",
                "ok": True,
                "message": "HL service starting.",
                "start_results": start_results,
            })
        else:
            self._send(400, {"error": f"unknown action: {action}"})

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/healthz":
            self._send(200, {"ok": True})
            return
        if path == "/vhl":
            self._send(200, collect_vhl_status())
            return
        if path == "/stop":
            self._handle_action("stop")
            return
        if path == "/start":
            self._handle_action("start")
            return
        if path not in {"/", "/status"}:
            self._send(404, {"error": "not_found", "paths": ["/healthz", "/vhl", "/stop", "/start", "/status"]})
            return
        if not self._authorized():
            self._send(401, {"error": "unauthorized", "hint": "send X-HLBOT-Token header or ?token="})
            return
        self._send(200, collect_status(self.config_path, include_remote=True))

    def do_POST(self) -> None:  # noqa: N802
        # Accept POST too (body is ignored; action is in the URL path)
        self.do_GET()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only LAN status API for hlbot")
    parser.add_argument("--config", default="config.live-testnet.yaml")
    parser.add_argument("--host", default=os.getenv("HLBOT_STATUS_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("HLBOT_STATUS_PORT", "8787")))
    parser.add_argument("--token", default=os.getenv("HLBOT_STATUS_TOKEN", ""))
    args = parser.parse_args(argv)
    Handler.config_path = args.config
    Handler.token = args.token
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.timeout = 0.5
    print(f"hlbot status server listening on http://{args.host}:{args.port}/status")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
