"""Tests for Execution Orchestrator, Leg Sequencing, and Unwind Engine (Phase 2.1)."""
from __future__ import annotations
import asyncio
import tempfile
from pathlib import Path

from polyarb.orchestrator import (
    ExecutionOrchestrator, OrchestratedLeg, OrchestrationPlan
)
from polyarb.executor import ExecutionRouter, OrderRequest, OrderResult
from polyarb.audit import AuditLog
from polyarb.config import Config, is_kill_switch_active, clear_kill_switch
from polyarb.alerts import AlertManager


def test_orchestrator_sequencing_thinnest_first():
    """Verify legs are sequenced so that the thinnest liquidity leg is executed first."""
    orchestrator = ExecutionOrchestrator(leg_ordering_policy="thinnest_first")

    legs = [
        OrchestratedLeg("mkt_deep", "polymarket", "YES", 0.50, 10.0, available_depth=500.0),
        OrchestratedLeg("mkt_thin", "kalshi", "NO", 0.48, 10.0, available_depth=15.0),
        OrchestratedLeg("mkt_med", "polymarket", "NO", 0.02, 10.0, available_depth=85.0),
    ]

    ordered = orchestrator.sequence_legs(legs)
    assert len(ordered) == 3
    assert ordered[0].market_id == "mkt_thin"   # 15.0 depth
    assert ordered[1].market_id == "mkt_med"    # 85.0 depth
    assert ordered[2].market_id == "mkt_deep"   # 500.0 depth


def test_orchestrator_successful_execution():
    """Verify multi-leg plan completes when all legs fill in dry-run mode."""
    with tempfile.TemporaryDirectory() as tmp:
        audit = AuditLog(db_path=Path(tmp) / "audit.sqlite")
        router = ExecutionRouter(audit_log=audit, dry_run=True)
        orchestrator = ExecutionOrchestrator(router=router, audit_log=audit)

        plan = OrchestrationPlan(
            structure_id="STR-TEST-SUCCESS",
            relation_id="rel_success",
            kind="implies",
            legs=[
                OrchestratedLeg("poly_1", "polymarket", "NO", 0.45, 10.0, available_depth=50.0),
                OrchestratedLeg("kalshi_1", "kalshi", "YES", 0.45, 10.0, available_depth=20.0),
            ],
            expected_edge=0.10,
            expected_payoff=1.0,
            expected_cost=0.90,
        )

        res = asyncio.run(orchestrator.execute_plan(plan))
        assert res.status == "COMPLETED"
        assert len(res.leg_records) == 2
        assert res.leg_records[0].status == "FILLED"
        assert res.leg_records[1].status == "FILLED"
        assert res.total_cost_usd > 0.0
        assert res.legging_loss_usd == 0.0

        # Audit events logged
        events = audit.get_events()
        actions = [e["action"] for e in events]
        assert "ORCHESTRATION_START" in actions
        assert "ORCHESTRATION_COMPLETED" in actions


def test_orchestrator_emergency_unwind_on_failure():
    """Verify that when Leg 2 fails, Leg 1 is automatically unwound at best bid."""
    with tempfile.TemporaryDirectory() as tmp:
        audit = AuditLog(db_path=Path(tmp) / "audit.sqlite")
        router = ExecutionRouter(audit_log=audit, dry_run=True)

        # Mock executor where first leg fills, but second leg fails
        orig_execute = router.execute_leg
        call_count = 0

        async def mock_execute(req: OrderRequest) -> OrderResult:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Leg 1 fills
                return OrderResult(
                    order_id="leg1_fill",
                    client_order_id=req.client_order_id,
                    venue=req.venue,
                    status="FILLED",
                    filled_size=req.size,
                    avg_fill_price=req.price,
                    fee_paid=0.01,
                )
            elif call_count == 2:
                # Leg 2 fails
                return OrderResult(
                    order_id="leg2_fail",
                    client_order_id=req.client_order_id,
                    venue=req.venue,
                    status="REJECTED",
                    filled_size=0.0,
                    avg_fill_price=0.0,
                    fee_paid=0.0,
                    error="Venue liquidity vanished",
                )
            else:
                # Unwind execution
                return OrderResult(
                    order_id="unwind_fill",
                    client_order_id=req.client_order_id,
                    venue=req.venue,
                    status="FILLED",
                    filled_size=req.size,
                    avg_fill_price=req.price,
                    fee_paid=0.01,
                )

        router.execute_leg = mock_execute
        orchestrator = ExecutionOrchestrator(router=router, audit_log=audit)

        plan = OrchestrationPlan(
            structure_id="STR-TEST-UNWIND",
            relation_id="rel_unwind",
            kind="implies",
            legs=[
                OrchestratedLeg("poly_leg1", "polymarket", "NO", 0.40, 10.0, available_depth=10.0),
                OrchestratedLeg("kalshi_leg2", "kalshi", "YES", 0.50, 10.0, available_depth=20.0),
            ],
            expected_edge=0.10,
            expected_payoff=1.0,
            expected_cost=0.90,
        )

        res = asyncio.run(orchestrator.execute_plan(plan))
        assert res.status == "UNWOUND"
        assert res.legging_loss_usd >= 0.0
        assert res.leg_records[0].unwound is True
        assert res.leg_records[0].unwind_order_id == "unwind_fill"

        # Check audit log for unwind event
        events = audit.get_events()
        actions = [e["action"] for e in events]
        assert "LEGGING_UNWIND" in actions


def test_orchestrator_legging_loss_circuit_breaker():
    """Verify that exceeding cumulative legging loss trips the kill switch."""
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config(data_dir=tmp)
        clear_kill_switch(tmp)

        audit = AuditLog(db_path=Path(tmp) / "audit.sqlite")
        router = ExecutionRouter(audit_log=audit, dry_run=True)
        orchestrator = ExecutionOrchestrator(
            router=router, audit_log=audit, cfg=cfg,
            max_daily_legging_loss_usd=25.0
        )

        # Set cumulative legging loss past limit
        orchestrator.cumulative_legging_loss_usd = 30.0

        plan = OrchestrationPlan(
            structure_id="STR-HALT-TEST",
            relation_id="rel_halt",
            kind="implies",
            legs=[
                OrchestratedLeg("poly_1", "polymarket", "YES", 0.50, 10.0),
                OrchestratedLeg("kalshi_1", "kalshi", "NO", 0.45, 10.0),
            ],
            expected_edge=0.05,
            expected_payoff=1.0,
            expected_cost=0.95,
        )

        res = asyncio.run(orchestrator.execute_plan(plan))
        assert res.status == "CIRCUIT_BREAKER_HALTED"
        assert is_kill_switch_active(tmp)
        clear_kill_switch(tmp)


if __name__ == "__main__":
    test_orchestrator_sequencing_thinnest_first()
    print("ok test_orchestrator_sequencing_thinnest_first")
    test_orchestrator_successful_execution()
    print("ok test_orchestrator_successful_execution")
    test_orchestrator_emergency_unwind_on_failure()
    print("ok test_orchestrator_emergency_unwind_on_failure")
    test_orchestrator_legging_loss_circuit_breaker()
    print("ok test_orchestrator_legging_loss_circuit_breaker")
