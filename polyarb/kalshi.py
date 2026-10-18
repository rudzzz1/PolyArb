"""Kalshi public market data (no auth needed for market data per Kalshi docs).
Kalshi books contain BIDS only: a YES bid at p == a NO ask at 1-p, and vice versa.
Verify field names against live responses on first run."""
from __future__ import annotations
from typing import Any
from .core import Level
from .gamma import http_get

BASE = "https://external-api.kalshi.com/trade-api/v2"


def kalshi_fee(p: float) -> float:
    """ESTIMATE of taker fee per contract: 0.07*p*(1-p). Kalshi rounds per order and
    has schedule exceptions -- check their current fee schedule before trusting."""
    return 0.07 * p * (1.0 - p)


def fetch_events(max_pages: int = 20) -> list[dict]:
    out, cursor = [], None
    for _ in range(max_pages):
        params: dict[str, Any] = {"status": "open", "with_nested_markets": "true", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        d = http_get(f"{BASE}/events", params)
        out += d.get("events", [])
        cursor = d.get("cursor")
        if not cursor:
            break
    return out


def parse_market(m: dict, ev: dict) -> dict | None:
    if m.get("mve_collection_ticker"):          # skip multivariate/parlay markets
        return None
    if m.get("status") not in (None, "active", "open"):
        return None
    title = m.get("title") or ev.get("title", "")
    sub = m.get("yes_sub_title") or ""
    q = title if sub.lower() in title.lower() else f"{title} {sub}".strip()

    rules = m.get("rules_primary") or ev.get("rules_primary") or ""
    sources = ev.get("settlement_sources") or m.get("settlement_sources") or ""
    if isinstance(sources, list):
        sources = ", ".join(str(s) for s in sources)

    return {
        "venue": "kalshi",
        "id": "kalshi:" + m["ticker"],
        "ticker": m["ticker"],
        "question": q,
        "end": m.get("close_time") or m.get("expiration_time"),
        "neg_risk": bool(ev.get("mutually_exclusive")),
        "event": "kalshi:" + str(ev.get("event_ticker")),
        "event_title": ev.get("title"),
        "resolution_rules": rules,
        "resolution_source": str(sources),
        "cutoff_time": m.get("close_time") or m.get("expiration_time") or "",
    }


def load_markets(events: list[dict]) -> dict:
    out = {}
    for ev in events:
        for m in ev.get("markets", []):
            pm = parse_market(m, ev)
            if pm:
                out[pm["id"]] = pm
    return out


def _levels(arr: list | None) -> list[tuple[float, float]]:
    out = []
    for e in arr or []:
        p, sz = float(e[0]), float(e[1])
        out.append((p / 100 if p > 1 else p, sz))   # tolerate legacy cents
    return out


def asks_from_bids(yes_bids: list[tuple[float, float]], no_bids: list[tuple[float, float]]) -> dict[str, list[Level]]:
    """YES ask = 1 - NO bid; NO ask = 1 - YES bid. Returns ascending ask ladders."""
    mk = lambda bids: sorted((Level(round(1 - p, 4), s) for p, s in bids), key=lambda l: l.price)
    return {"YES": mk(no_bids), "NO": mk(yes_bids)}


def book_from_bids(yes_bids: list[tuple[float, float]], no_bids: list[tuple[float, float]]) -> dict[str, dict[str, list[Level]]]:
    """Returns both bids and derived asks for depth and unwind analysis."""
    mk_ask = lambda bids: sorted((Level(round(1 - p, 4), s) for p, s in bids), key=lambda l: l.price)
    mk_bid = lambda bids: sorted((Level(round(p, 4), s) for p, s in bids), key=lambda l: l.price, reverse=True)
    return {
        "YES": {"bids": mk_bid(yes_bids), "asks": mk_ask(no_bids)},
        "NO": {"bids": mk_bid(no_bids), "asks": mk_ask(yes_bids)},
    }


def fetch_asks(ticker: str) -> dict[str, list[Level]]:
    d = http_get(f"{BASE}/markets/{ticker}/orderbook")["orderbook_fp"]
    return asks_from_bids(_levels(d.get("yes_dollars")), _levels(d.get("no_dollars")))


def fetch_book(ticker: str) -> dict[str, dict[str, list[Level]]]:
    d = http_get(f"{BASE}/markets/{ticker}/orderbook")["orderbook_fp"]
    return book_from_bids(_levels(d.get("yes_dollars")), _levels(d.get("no_dollars")))
