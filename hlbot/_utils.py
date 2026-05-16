"""Shared helpers used across multiple hlbot modules."""

from __future__ import annotations

from typing import Any


def extract_resting_oid(result: Any) -> int | None:
    """Extract the resting order ID from an exchange order result."""
    try:
        statuses = result["response"]["data"]["statuses"]
    except (KeyError, TypeError):
        return None
    for status in statuses:
        if isinstance(status, dict) and "resting" in status:
            return int(status["resting"]["oid"])
    return None


def best_bid_ask(book: dict[str, Any]) -> tuple[float | None, float | None]:
    """Return (bid, ask) from an L2 snapshot, or (None, None)."""
    levels = book.get("levels", [])
    if len(levels) < 2 or not levels[0] or not levels[1]:
        return None, None
    return float(levels[0][0]["px"]), float(levels[1][0]["px"])


def spread_bps(book: dict[str, Any]) -> float | None:
    """Compute the best bid-ask spread in basis points."""
    bid, ask = best_bid_ask(book)
    if bid is None or ask is None or bid <= 0 or ask <= 0:
        return None
    mid = (bid + ask) / 2
    return (ask - bid) / mid * 10000
