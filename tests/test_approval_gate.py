"""Tests for Human-in-the-Loop Approval Gate (Phase 2.2)."""
from __future__ import annotations
import tempfile
import time
from pathlib import Path

from polyarb.approval_gate import ApprovalGate
from polyarb.audit import AuditLog


def test_queue_candidate_for_approval():
    with tempfile.TemporaryDirectory() as tmp:
        audit = AuditLog(db_path=Path(tmp) / "audit.sqlite")
        gate = ApprovalGate(audit_log=audit, mode="HUMAN_APPROVAL", quote_ttl_sec=30.0)

        legs = [
            {"market_id": "poly_btc", "venue": "polymarket", "side": "NO", "entry_price_usd": 0.44},
            {"market_id": "kalshi_btc", "venue": "kalshi", "side": "YES", "entry_price_usd": 0.50},
        ]

        item = gate.submit_for_approval(
            relation_id="rel_btc_ladder",
            kind="implies",
            event_title="BTC Threshold Match",
            legs=legs,
            unit_cost=0.94,
            expected_payoff=1.0,
            net_edge=0.06,
            shares=20.0,
            apy=1.85,
            risk_flags=["CROSS_VENUE"],
        )

        assert item.id.startswith("app_")
        assert item.status == "PENDING"
        assert item.net_edge == 0.06
        assert len(gate.get_pending_items()) == 1


def test_approve_candidate():
    with tempfile.TemporaryDirectory() as tmp:
        audit = AuditLog(db_path=Path(tmp) / "audit.sqlite")
        gate = ApprovalGate(audit_log=audit, mode="HUMAN_APPROVAL")

        item = gate.submit_for_approval(
            relation_id="rel_fed_cut",
            kind="partition",
            event_title="Fed Rate Cut",
            legs=[],
            unit_cost=0.92,
            expected_payoff=1.0,
            net_edge=0.08,
            shares=15.0,
        )

        ok, msg, approved_item = gate.approve(item.id, approved_by="senior_trader")
        assert ok is True
        assert approved_item.status == "APPROVED"
        assert approved_item.approved_by == "senior_trader"
        assert gate.manual_approved_count == 1
        assert len(gate.get_pending_items()) == 0


def test_reject_candidate():
    with tempfile.TemporaryDirectory() as tmp:
        audit = AuditLog(db_path=Path(tmp) / "audit.sqlite")
        gate = ApprovalGate(audit_log=audit, mode="HUMAN_APPROVAL")

        item = gate.submit_for_approval(
            relation_id="rel_doubtful",
            kind="implies",
            event_title="Doubtful Opportunity",
            legs=[],
            unit_cost=0.98,
            expected_payoff=1.0,
            net_edge=0.02,
            shares=10.0,
        )

        ok, msg, rejected_item = gate.reject(item.id, reason="rule_resolution_ambiguous")
        assert ok is True
        assert rejected_item.status == "REJECTED"
        assert rejected_item.rejection_reason == "rule_resolution_ambiguous"
        assert len(gate.get_pending_items()) == 0


def test_expired_quote_cannot_be_approved():
    with tempfile.TemporaryDirectory() as tmp:
        audit = AuditLog(db_path=Path(tmp) / "audit.sqlite")
        # Very short TTL
        gate = ApprovalGate(audit_log=audit, quote_ttl_sec=0.01)

        item = gate.submit_for_approval(
            relation_id="rel_stale",
            kind="implies",
            event_title="Stale Quote",
            legs=[],
            unit_cost=0.90,
            expected_payoff=1.0,
            net_edge=0.10,
            shares=10.0,
        )

        time.sleep(0.05)
        ok, msg, _ = gate.approve(item.id)
        assert ok is False
        assert "expired" in msg.lower()
        assert item.status == "EXPIRED"


def test_mode_switching_guard():
    with tempfile.TemporaryDirectory() as tmp:
        audit = AuditLog(db_path=Path(tmp) / "audit.sqlite")
        gate = ApprovalGate(audit_log=audit, auto_mode_threshold=5)

        # Cannot switch to AUTO before 5 manual approvals
        ok, msg = gate.set_mode("AUTO")
        assert ok is False
        assert "requires 5 manual approvals" in msg

        # Simulate 5 manual approvals
        gate.manual_approved_count = 5
        ok, msg = gate.set_mode("AUTO")
        assert ok is True
        assert gate.mode == "AUTO"


if __name__ == "__main__":
    test_queue_candidate_for_approval()
    print("ok test_queue_candidate_for_approval")
    test_approve_candidate()
    print("ok test_approve_candidate")
    test_reject_candidate()
    print("ok test_reject_candidate")
    test_expired_quote_cannot_be_approved()
    print("ok test_expired_quote_cannot_be_approved")
    test_mode_switching_guard()
    print("ok test_mode_switching_guard")
