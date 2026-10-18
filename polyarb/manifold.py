"""Manifold Markets Venue Integration (Phase 4.2).
Provides market fetching, binary orderbook derivation, and fee functions for Manifold Markets.
Manifold operates transparent binary prediction markets with zero taker trading fees.
"""
from __future__ import annotations
import json
import urllib.request
from datetime import datetime, timezone
from typing import Any

from .core import Level
from .logger import get_logger

logger = get_logger("manifold")

MANIFOLD_API_BASE = "https://api.manifold.markets/v0"


def fetch_markets(limit: int = 50, term: str = "") -> list[dict]:
    """Fetches public binary markets from Manifold Markets REST API."""
    url = f"{MANIFOLD_API_BASE}/markets?limit={limit}"
    if term:
        url += f"&term={urllib.parse.quote(term)}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "PolyArb/1.0"})
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                return [m for m in data if m.get("outcomeType") == "BINARY" and not m.get("isResolved")]
    except Exception as e:
        logger.debug(f"Manifold market fetch failed: {e}")
    return []


def load_markets(raw_markets: list[dict]) -> dict[str, dict]:
    """Transforms raw Manifold API market payloads into polyarb normalized market dicts."""
    out = {}
    for m in raw_markets:
        mid = f"manifold_{m.get('id')}"
        close_time = m.get("closeTime")
        end_iso = ""
        if close_time:
            end_iso = datetime.fromtimestamp(close_time / 1000.0, tz=timezone.utc).isoformat()

        prob = float(m.get("probability", 0.5))
        # Derives ask with a standard 1% liquidity spread
        yes_ask = min(0.99, round(prob + 0.01, 3))
        no_ask = min(0.99, round((1.0 - prob) + 0.01, 3))

        volume = float(m.get("volume", 500.0))
        depth = max(50.0, min(volume * 0.1, 1000.0))

        out[mid] = {
            "id": mid,
            "venue": "manifold",
            "question": m.get("question", ""),
            "end": end_iso,
            "probability": prob,
            "yes_ask": yes_ask,
            "no_ask": no_ask,
            "depth": depth,
            "resolution_source": "manifold_community",
            "raw": m,
        }
    return out


def fetch_asks(market: dict) -> dict[str, list[Level]]:
    """Returns derived asks for Manifold binary market."""
    yes_p = market.get("yes_ask", 0.50)
    no_p = market.get("no_ask", 0.50)
    depth = market.get("depth", 100.0)
    return {
        "YES": [Level(price=yes_p, size=depth)],
        "NO": [Level(price=no_p, size=depth)],
    }


def fetch_book(market: dict) -> dict[str, dict[str, list[Level]]]:
    """Returns derived orderbook for Manifold binary market."""
    yes_p = market.get("yes_ask", 0.50)
    no_p = market.get("no_ask", 0.50)
    depth = market.get("depth", 100.0)
    return {
        "YES": {
            "bids": [Level(price=max(0.01, round(yes_p - 0.02, 3)), size=depth)],
            "asks": [Level(price=yes_p, size=depth)],
        },
        "NO": {
            "bids": [Level(price=max(0.01, round(no_p - 0.02, 3)), size=depth)],
            "asks": [Level(price=no_p, size=depth)],
        },
    }


def manifold_fee(price: float) -> float:
    """Manifold charges 0% taker fee for cross-market participants."""
    return 0.0
