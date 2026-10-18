"""Unit tests for Kalshi WebSocket Feed & In-Memory Book Store (Phase 4.1)."""
from __future__ import annotations
from polyarb.kalshi_ws import TickerOrderbook, KalshiBookStore, KalshiWsClient
from polyarb.core import Level


def test_orderbook_snapshot_and_derivation():
    ob = TickerOrderbook(ticker="KXTEST")
    # Kalshi format: bids are [price_cents, count]
    # In Kalshi, YES bid at 55c implies NO ask at 45c (100 - 55).
    # NO bid at 40c implies YES ask at 60c (100 - 40).
    ob.apply_snapshot(
        yes_bids=[[55, 100], [53, 200]],
        no_bids=[[40, 150], [38, 300]],
    )

    asks = ob.get_asks()
    assert "YES" in asks and "NO" in asks
    # YES ask = 100 - 40 = 60c = 0.60
    assert len(asks["YES"]) >= 1
    assert asks["YES"][0].price == 0.60
    assert asks["YES"][0].size == 150.0

    # NO ask = 100 - 55 = 45c = 0.45
    assert len(asks["NO"]) >= 1
    assert asks["NO"][0].price == 0.45
    assert asks["NO"][0].size == 100.0


def test_orderbook_delta_updates():
    ob = TickerOrderbook(ticker="KXDELTA")
    ob.apply_snapshot(yes_bids=[[50, 100]])
    asks_orig = ob.get_asks()
    assert asks_orig["NO"][0].price == 0.50
    assert asks_orig["NO"][0].size == 100.0

    # Increase bid depth by 50
    ob.apply_delta(side="yes", price_cents=50, delta=50)
    asks_updated = ob.get_asks()
    assert asks_updated["NO"][0].size == 150.0

    # Cancel remaining depth
    ob.apply_delta(side="yes", price_cents=50, delta=-150)
    asks_empty = ob.get_asks()
    assert len(asks_empty["NO"]) == 0


def test_kalshi_book_store_cache():
    store = KalshiBookStore()
    assert not store.has_ticker("KXBTC")

    store.update_snapshot("KXBTC", yes_bids=[[60, 50]], no_bids=[[35, 75]])
    assert store.has_ticker("KXBTC")

    asks = store.get_asks("KXBTC", max_age_sec=5.0)
    assert asks is not None
    assert asks["YES"][0].price == 0.65  # 100 - 35 = 65c


def test_kalshi_ws_message_handling():
    ws = KalshiWsClient()
    snapshot_msg = {
        "type": "orderbook_snapshot",
        "msg": {
            "market_ticker": "KXFED",
            "yes": [[70, 200]],
            "no": [[25, 100]],
        }
    }
    ws.handle_message(snapshot_msg)
    asks = ws.store.get_asks("KXFED")
    assert asks is not None
    assert asks["YES"][0].price == 0.75  # 100 - 25 = 75c
    assert asks["NO"][0].price == 0.30   # 100 - 70 = 30c


if __name__ == "__main__":
    test_orderbook_snapshot_and_derivation()
    print("ok test_orderbook_snapshot_and_derivation")
    test_orderbook_delta_updates()
    print("ok test_orderbook_delta_updates")
    test_kalshi_book_store_cache()
    print("ok test_kalshi_book_store_cache")
    test_kalshi_ws_message_handling()
    print("ok test_kalshi_ws_message_handling")
