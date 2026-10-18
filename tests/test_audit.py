"""Tests for Immutable Audit Log and Trigger Enforcement (Phase 1.4)."""
from __future__ import annotations
import csv
import json
import sqlite3
import tempfile
from pathlib import Path
from polyarb.audit import AuditLog


def test_audit_log_insertion_and_query():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test_audit.sqlite"
        audit = AuditLog(db_path=db_path)

        rec_id = audit.log(
            action="ORDER_PLACED",
            venue="polymarket",
            market_id="0xabc",
            side="YES",
            price=0.45,
            size=10.0,
            order_id="poly_12345",
            fill_qty=10.0,
            fill_price=0.45,
            status="FILLED",
            raw_response={"status": "ok"},
            metadata={"source": "poller"},
        )
        assert rec_id >= 1

        events = audit.get_events()
        assert len(events) == 1
        ev = events[0]
        assert ev["action"] == "ORDER_PLACED"
        assert ev["venue"] == "polymarket"
        assert ev["price"] == 0.45
        assert ev["size"] == 10.0
        assert ev["order_id"] == "poly_12345"
        assert len(ev["raw_response_hash"]) == 64  # SHA-256


def test_audit_log_database_level_immutability():
    """Verify SQLite triggers block any attempt to UPDATE or DELETE audit records."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test_audit.sqlite"
        audit = AuditLog(db_path=db_path)

        rec_id = audit.log(
            action="STRUCTURE_BOOKED",
            venue="kalshi",
            order_id="STR-001",
            price=0.50,
            size=20.0,
        )

        conn = sqlite3.connect(str(db_path))

        # 1. Attempt UPDATE -> MUST FAIL via trigger
        try:
            conn.execute("UPDATE audit_log SET price = 0.99 WHERE id = ?", (rec_id,))
            assert False, "Database trigger failed to block UPDATE on audit_log!"
        except sqlite3.DatabaseError as e:
            assert "IMMUTABILITY VIOLATION" in str(e) or "prohibited" in str(e)

        # 2. Attempt DELETE -> MUST FAIL via trigger
        try:
            conn.execute("DELETE FROM audit_log WHERE id = ?", (rec_id,))
            assert False, "Database trigger failed to block DELETE on audit_log!"
        except sqlite3.DatabaseError as e:
            assert "IMMUTABILITY VIOLATION" in str(e) or "prohibited" in str(e)

        conn.close()


def test_audit_export_csv_and_json():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "audit.sqlite"
        audit = AuditLog(db_path=db_path)

        audit.log(action="ORDER_PLACED", venue="polymarket", order_id="ord_1", price=0.40, size=5.0)
        audit.log(action="ORDER_FILLED", venue="polymarket", order_id="ord_1", price=0.40, size=5.0, status="FILLED")

        # Export CSV
        csv_file = Path(tmp) / "audit_export.csv"
        audit.export_csv(csv_file)
        assert csv_file.exists()
        with open(csv_file, "r", encoding="utf-8") as f:
            reader = list(csv.reader(f))
            assert len(reader) == 3  # header + 2 rows
            assert reader[0][2] == "action"

        # Export JSON
        json_file = Path(tmp) / "audit_export.json"
        audit.export_json(json_file)
        assert json_file.exists()
        parsed = json.loads(json_file.read_text(encoding="utf-8"))
        assert len(parsed) == 2
        assert parsed[0]["action"] == "ORDER_FILLED"  # get_events returns desc


if __name__ == "__main__":
    test_audit_log_insertion_and_query()
    print("ok test_audit_log_insertion_and_query")
    test_audit_log_database_level_immutability()
    print("ok test_audit_log_database_level_immutability")
    test_audit_export_csv_and_json()
    print("ok test_audit_export_csv_and_json")
