"""Unit tests for Manifold Markets Venue Adapter (Phase 4.2)."""
from __future__ import annotations
import asyncio
from polyarb import manifold
from polyarb.executor import ManifoldExecutor, ExecutionRouter, OrderRequest
from polyarb.audit import AuditLog


def test_manifold_market_loading():
    raw_markets = [
        {
            "id": "mf_market_1",
            "question": "Will SpaceX launch Starship Flight 6 in 2026?",
            "outcomeType": "BINARY",
            "probability": 0.62,
            "volume": 2500.0,
            "closeTime": 1798761600000,
            "isResolved": False,
        }
    ]

    loaded = manifold.load_markets(raw_markets)
    assert len(loaded) == 1
    m = loaded["manifold_mf_market_1"]
    assert m["venue"] == "manifold"
    assert m["probability"] == 0.62
    # Probability + 1% spread
    assert m["yes_ask"] == 0.63
    assert m["no_ask"] == 0.39
    assert m["depth"] > 0


def test_manifold_asks_and_book():
    market = {
        "id": "manifold_test",
        "venue": "manifold",
        "yes_ask": 0.55,
        "no_ask": 0.47,
        "depth": 250.0,
    }

    asks = manifold.fetch_asks(market)
    assert asks["YES"][0].price == 0.55
    assert asks["YES"][0].size == 250.0
    assert asks["NO"][0].price == 0.47

    book = manifold.fetch_book(market)
    assert "YES" in book and "NO" in book
    assert len(book["YES"]["asks"]) == 1
    assert len(book["YES"]["bids"]) == 1
    assert manifold.manifold_fee(0.55) == 0.0


def test_manifold_executor_dry_run():
    executor = ManifoldExecutor(dry_run=True)
    req = OrderRequest(
        market_id="manifold_starship",
        venue="manifold",
        side="YES",
        price=0.60,
        size=10.0,
    )

    loop = asyncio.new_event_loop()
    try:
        res = loop.run_until_complete(executor.place_limit(req))
        assert res.status == "FILLED"
        assert res.filled_size == 10.0
        assert res.avg_fill_price == 0.60
        assert res.venue == "manifold"

        cancelled = loop.run_until_complete(executor.cancel(res.order_id))
        assert cancelled is True

        balance = loop.run_until_complete(executor.get_balance())
        assert balance.venue == "manifold"
        assert balance.available > 0
    finally:
        loop.close()


def test_router_manifold_dispatch():
    router = ExecutionRouter(dry_run=True)
    exec_mf = router.get_executor("manifold")
    assert isinstance(exec_mf, ManifoldExecutor)


if __name__ == "__main__":
    test_manifold_market_loading()
    print("ok test_manifold_market_loading")
    test_manifold_asks_and_book()
    print("ok test_manifold_asks_and_book")
    test_manifold_executor_dry_run()
    print("ok test_manifold_executor_dry_run")
    test_router_manifold_dispatch()
    print("ok test_router_manifold_dispatch")
