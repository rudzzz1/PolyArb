"""Venue dispatch: one interface for books, bids, and fees."""
from __future__ import annotations
from typing import Callable
from . import gamma, kalshi
from .core import Level


def fetch_market_asks(m: dict) -> dict[str, list[Level]]:
    """-> {"YES": [Level...], "NO": [Level...]} ascending asks."""
    if m.get("venue") == "kalshi":
        return kalshi.fetch_asks(m["ticker"])
    return {"YES": gamma.fetch_asks(m["yes"]), "NO": gamma.fetch_asks(m["no"])}


def fetch_market_book(m: dict) -> dict[str, dict[str, list[Level]]]:
    """-> {"YES": {"bids": [...], "asks": [...]}, "NO": {"bids": [...], "asks": [...]}}"""
    if m.get("venue") == "kalshi":
        return kalshi.fetch_book(m["ticker"])
    return {
        "YES": gamma.fetch_book(m["yes"]),
        "NO": gamma.fetch_book(m["no"]),
    }


def fee_fn(m: dict, polymarket_fee_bps: float = 0.0) -> Callable[[float], float] | None:
    """Per-share fee as a function of price.
    Kalshi: variable per contract fee based on p*(1-p).
    Polymarket: 0 by default, or category-specific bps if configured."""
    if m.get("venue") == "kalshi":
        return kalshi.kalshi_fee
    if polymarket_fee_bps > 0:
        return lambda p: p * (polymarket_fee_bps / 10000.0)
    return None
