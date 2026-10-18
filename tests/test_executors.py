"""Tests for Venue Execution Adapters and Router (Phase 1.3)."""
from __future__ import annotations
import asyncio
import tempfile
from pathlib import Path
from polyarb.executor import (
    OrderRequest, OrderResult, PolymarketExecutor, KalshiExecutor,
    ExecutionRouter
)
from polyarb.audit import AuditLog
from polyarb.credentials import KeyStore


def test_polymarket_executor_dry_run():
    audit = AuditLog(db_path=":memory:")
    executor = PolymarketExecutor(audit_log=audit, dry_run=True)

    req = OrderRequest(
        market_id="0xabc123",
        venue="polymarket",
        side="YES",
        price=0.48,
        size=15.0,
        order_type="IOC",
    )
    result = asyncio.run(executor.place_limit(req))

    assert result.status == "FILLED"
    assert result.filled_size == 15.0
    assert result.avg_fill_price == 0.48
    assert result.venue == "polymarket"
    assert len(result.fills) == 1

    # Check audit log
    events = audit.get_events()
    assert len(events) == 1
    assert events[0]["action"] == "ORDER_PLACED"
    assert events[0]["venue"] == "polymarket"
    assert events[0]["size"] == 15.0


def test_kalshi_executor_dry_run():
    audit = AuditLog(db_path=":memory:")
    executor = KalshiExecutor(audit_log=audit, dry_run=True)

    req = OrderRequest(
        market_id="KXFED-26DEC",
        venue="kalshi",
        side="NO",
        price=0.52,
        size=10.0,
    )
    result = asyncio.run(executor.place_limit(req))

    assert result.status == "FILLED"
    assert result.filled_size == 10.0
    assert result.avg_fill_price == 0.52
    assert result.venue == "kalshi"
    # Kalshi taker fee estimation > 0
    assert result.fee_paid > 0.0


def test_execution_router_multi_leg_structure():
    audit = AuditLog(db_path=":memory:")
    router = ExecutionRouter(audit_log=audit, dry_run=True)

    legs = [
        OrderRequest(market_id="poly_btc_100k", venue="polymarket", side="NO", price=0.44, size=12.0),
        OrderRequest(market_id="kalshi_btc_90k", venue="kalshi", side="YES", price=0.51, size=12.0),
    ]

    results = asyncio.run(router.execute_structure(legs))
    assert len(results) == 2
    assert results[0].venue == "polymarket"
    assert results[0].status == "FILLED"
    assert results[1].venue == "kalshi"
    assert results[1].status == "FILLED"

    # Both legs logged to audit log
    events = audit.get_events()
    assert len(events) == 2


if __name__ == "__main__":
    test_polymarket_executor_dry_run()
    print("ok test_polymarket_executor_dry_run")
    test_kalshi_executor_dry_run()
    print("ok test_kalshi_executor_dry_run")
    test_execution_router_multi_leg_structure()
    print("ok test_execution_router_multi_leg_structure")
