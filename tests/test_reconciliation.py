"""Tests for Fill Reconciliation and Slippage Tracking (Phase 2.3)."""
from __future__ import annotations
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")

from polyarb.reconciliation import ReconciliationManager
from polyarb.executor import ExecutionRouter
from polyarb.audit import AuditLog
from polyarb.alerts import AlertManager


def test_reconciliation_exact_slippage_calculation():
    with tempfile.TemporaryDirectory() as tmp:
        audit = AuditLog(db_path=Path(tmp) / "audit.sqlite")
        recon = ReconciliationManager(audit_log=audit)

        # Expected: 0.50, Filled: 0.51 -> +2.0% = +200 bps
        rec = recon.reconcile_fill(
            order_id="ord_123",
            venue="polymarket",
            market_id="0xabc",
            side="YES",
            expected_price=0.50,
            actual_price=0.51,
            expected_size=10.0,
            filled_size=10.0,
            fee_paid=0.02,
        )

        assert abs(rec.slippage_bps - 200.0) < 1e-4
        assert rec.size_discrepancy == 0.0
        assert abs(recon.cumulative_slippage_bps - 200.0) < 1e-4


def test_reconciliation_slippage_alert():
    with tempfile.TemporaryDirectory() as tmp:
        audit = AuditLog(db_path=Path(tmp) / "audit.sqlite")
        alert_mgr = AlertManager()
        recon = ReconciliationManager(audit_log=audit, alert_mgr=alert_mgr, max_slippage_bps=50.0)

        # 100 bps slippage > 50 bps threshold
        rec = recon.reconcile_fill(
            order_id="ord_high_slip",
            venue="kalshi",
            market_id="KX-SLIP",
            side="NO",
            expected_price=0.40,
            actual_price=0.404,  # +1% = 100 bps
            expected_size=5.0,
            filled_size=5.0,
        )

        assert rec.slippage_bps == 100.0
        assert len(alert_mgr.history) == 1
        assert alert_mgr.history[0].category == "SLIPPAGE"
        assert alert_mgr.history[0].level == "WARNING"


def test_reconciliation_venue_balance():
    with tempfile.TemporaryDirectory() as tmp:
        audit = AuditLog(db_path=Path(tmp) / "audit.sqlite")
        router = ExecutionRouter(audit_log=audit, dry_run=True)
        recon = ReconciliationManager(audit_log=audit)

        # In dry run, Polymarket balance returns 50.0 and Kalshi returns 50.0
        # If internal is 50.0 and 50.0 -> MATCH
        recs = asyncio.run(recon.reconcile_venue_balances(router, internal_poly_usd=50.0, internal_kalshi_usd=50.0))
        assert len(recs) == 2
        assert recs[0].status == "MATCH"
        assert recs[1].status == "MATCH"

        # Discrepancy test: internal claims 150.0, exchange has 50.0
        recs_disc = asyncio.run(recon.reconcile_venue_balances(router, internal_poly_usd=50.0, internal_kalshi_usd=150.0))
        assert recs_disc[1].status == "DISCREPANCY"
        assert recs_disc[1].difference == 100.0


if __name__ == "__main__":
    test_reconciliation_exact_slippage_calculation()
    print("ok test_reconciliation_exact_slippage_calculation")
    test_reconciliation_slippage_alert()
    print("ok test_reconciliation_slippage_alert")
    test_reconciliation_venue_balance()
    print("ok test_reconciliation_venue_balance")
