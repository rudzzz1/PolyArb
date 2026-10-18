"""Unit tests for Phase 6 (Hardening & Scale).
Tests SQLite schema migrations, automated timestamped portfolio backups with retention policy,
transport hardening with fallback DNS, FIFO cost basis accounting, tax CSV export,
and jurisdiction compliance verification.
"""
from __future__ import annotations
import csv
import io
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")
from polyarb.migrations import SchemaMigrator, MIGRATIONS
from polyarb.simulator import PortfolioManager
from polyarb.transport import _get_fallback_ips, _resolve_host
from polyarb.compliance import FifoCostBasisTracker, JurisdictionCompliance


def test_schema_migrations():
    conn = sqlite3.connect(":memory:")
    try:
        # Check starting version is 0
        assert SchemaMigrator.get_current_version(conn) == 0

        # Apply migrations
        applied = SchemaMigrator.apply_migrations(conn)
        assert applied == len(MIGRATIONS)
        assert SchemaMigrator.get_current_version(conn) == len(MIGRATIONS)

        # Applying again applies 0 new migrations
        assert SchemaMigrator.apply_migrations(conn) == 0

        # Verify audit_log table exists and triggers work
        with conn:
            conn.execute("INSERT INTO audit_log (ts, action, venue) VALUES ('2026-10-18T12:00:00Z', 'TEST', 'poly')")

        # Test immutability: UPDATE should fail
        update_blocked = False
        try:
            with conn:
                conn.execute("UPDATE audit_log SET action = 'MODIFIED' WHERE id = 1")
        except sqlite3.IntegrityError:
            update_blocked = True
        assert update_blocked is True

        # Test immutability: DELETE should fail
        delete_blocked = False
        try:
            with conn:
                conn.execute("DELETE FROM audit_log WHERE id = 1")
        except sqlite3.IntegrityError:
            delete_blocked = True
        assert delete_blocked is True

        print("ok test_schema_migrations")
    finally:
        conn.close()


def test_portfolio_backup_and_retention():
    temp_dir = tempfile.mkdtemp()
    try:
        pm = PortfolioManager(initial_capital_inr=10000.0, data_dir=temp_dir)
        pm.save()

        # Create multiple backups with retention limit of 3
        backup_files = []
        for i in range(5):
            b = pm.backup(max_backups=3)
            if b:
                backup_files.append(b)

        backups_dir = Path(temp_dir) / "backups"
        remaining = list(backups_dir.glob("portfolio_*.json"))
        assert len(remaining) <= 3  # Pruned according to retention policy
        print("ok test_portfolio_backup_and_retention")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_transport_fallback_dns():
    old_env = os.environ.get("POLYARB_FALLBACK_IPS")
    try:
        os.environ["POLYARB_FALLBACK_IPS"] = "custom.venue.com:192.168.1.100"
        ips = _get_fallback_ips()
        assert ips["custom.venue.com"] == "192.168.1.100"
        assert "gamma-api.polymarket.com" in ips
        print("ok test_transport_fallback_dns")
    finally:
        if old_env is not None:
            os.environ["POLYARB_FALLBACK_IPS"] = old_env
        else:
            os.environ.pop("POLYARB_FALLBACK_IPS", None)


def test_fifo_cost_basis_and_tax_export():
    tracker = FifoCostBasisTracker()

    # Buy Lot 1: 10 shares @ $0.40 on 2026-01-15
    tracker.record_buy(
        lot_id="LOT-1",
        market_id="poly_cpi",
        side="YES",
        shares=10.0,
        unit_price_usd=0.40,
        fee_paid_usd=0.05,
        acquired_at="2026-01-15T10:00:00Z",
    )

    # Buy Lot 2: 10 shares @ $0.60 on 2026-02-20
    tracker.record_buy(
        lot_id="LOT-2",
        market_id="poly_cpi",
        side="YES",
        shares=10.0,
        unit_price_usd=0.60,
        fee_paid_usd=0.05,
        acquired_at="2026-02-20T10:00:00Z",
    )

    # Dispose 15 shares @ $1.00 (settlement win) on 2026-04-01
    events = tracker.record_disposition(
        market_id="poly_cpi",
        side="YES",
        shares=15.0,
        unit_proceeds_usd=1.00,
        venue="polymarket",
        disposed_at="2026-04-01T12:00:00Z",
        description="Polymarket CPI YES Settlement",
    )

    assert len(events) == 2
    # First match: 10 shares from LOT-1
    assert events[0].lot_id == "LOT-1"
    assert events[0].shares == 10.0
    assert abs(events[0].proceeds_usd - 10.00) < 0.01
    assert abs(events[0].cost_basis_usd - 4.05) < 0.01
    assert abs(events[0].gain_loss_usd - 5.95) < 0.01
    assert events[0].is_short_term is True

    # Second match: 5 shares from LOT-2
    assert events[1].lot_id == "LOT-2"
    assert events[1].shares == 5.0
    assert abs(events[1].proceeds_usd - 5.00) < 0.01
    assert abs(events[1].cost_basis_usd - 3.025) < 0.01

    # Remaining in queue: LOT-2 has 5 shares left
    remaining_lot = tracker.open_lots[("poly_cpi", "YES")][0]
    assert remaining_lot.shares == 5.0

    # Test CSV export
    csv_str = tracker.export_csv()
    assert "Description of Property" in csv_str
    assert "Polymarket CPI YES Settlement" in csv_str
    assert "Short-Term" in csv_str
    print("ok test_fifo_cost_basis_and_tax_export")


def test_jurisdiction_compliance():
    # Compliant region
    r1 = JurisdictionCompliance.check_jurisdiction("IN")
    assert r1["compliant"] is True
    assert "compliant" in r1["warning"].lower()

    # Restricted region (US)
    r2 = JurisdictionCompliance.check_jurisdiction("US")
    assert r2["compliant"] is False
    assert "CFTC" in r2["warning"]
    assert "proxy" in r2["recommended_action"].lower()
    print("ok test_jurisdiction_compliance")


if __name__ == "__main__":
    test_schema_migrations()
    test_portfolio_backup_and_retention()
    test_transport_fallback_dns()
    test_fifo_cost_basis_and_tax_export()
    test_jurisdiction_compliance()
    print("\nALL PHASE 6 HARDENING & SCALE TESTS PASSED!")
