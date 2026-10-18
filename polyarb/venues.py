from __future__ import annotations
from typing import Callable
from . import gamma, kalshi, manifold
from .core import Level
from .kalshi_ws import KalshiWsClient


def fetch_market_asks(m: dict) -> dict[str, list[Level]]:
    """-> {"YES": [Level...], "NO": [Level...]} ascending asks with sub-second WebSocket priority."""
    v = m.get("venue", "polymarket")
    if v == "kalshi":
        ticker = m.get("ticker", "")
        # Sub-second WebSocket orderbook check (Phase 4.1)
        ws_asks = KalshiWsClient.get_instance().store.get_asks(ticker)
        if ws_asks and len(ws_asks.get("YES", [])) > 0:
            return ws_asks
        return kalshi.fetch_asks(ticker)
    elif v == "manifold":
        return manifold.fetch_asks(m)
    return {"YES": gamma.fetch_asks(m["yes"]), "NO": gamma.fetch_asks(m["no"])}


def fetch_market_book(m: dict) -> dict[str, dict[str, list[Level]]]:
    """-> {"YES": {"bids": [...], "asks": [...]}, "NO": {"bids": [...], "asks": [...]}}"""
    v = m.get("venue", "polymarket")
    if v == "kalshi":
        return kalshi.fetch_book(m.get("ticker", ""))
    elif v == "manifold":
        return manifold.fetch_book(m)
    return {
        "YES": gamma.fetch_book(m["yes"]),
        "NO": gamma.fetch_book(m["no"]),
    }


def fee_fn(m: dict, polymarket_fee_bps: float = 0.0) -> Callable[[float], float] | None:
    """Per-share fee as a function of price.
    Kalshi: variable per contract fee based on p*(1-p).
    Manifold: 0% taker fee.
    Polymarket: 0 by default, or category-specific bps if configured."""
    v = m.get("venue", "polymarket")
    if v == "kalshi":
        return kalshi.kalshi_fee
    elif v == "manifold":
        return manifold.manifold_fee
    if polymarket_fee_bps > 0:
        return lambda p: p * (polymarket_fee_bps / 10000.0)
    return None
