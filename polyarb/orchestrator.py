"""Execution Orchestrator and Unwind Engine (Phase 2.1).
Safely sequences multi-leg arbitrage trades:
1. Orders legs by liquidity (thinnest leg first) to minimize adverse selection risk.
2. Submits Leg 1 and monitors fill confirmation with timeout.
3. Submits Leg 2 only upon successful fill of Leg 1.
4. If Leg 2 fails/times out, immediately executes emergency unwind on Leg 1 at best bid.
5. Tracks cumulative legging loss and trips circuit breaker if loss limit is breached.
6. Writes every step to the immutable SQLite audit log.
"""
from __future__ import annotations
import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .audit import AuditLog
from .config import Config, is_kill_switch_active, trigger_kill_switch
from .executor import ExecutionRouter, OrderRequest, OrderResult
from .alerts import AlertManager
from .logger import get_logger

logger = get_logger("orchestrator")


@dataclass
class OrchestratedLeg:
    market_id: str
    venue: str                     # 'polymarket' | 'kalshi'
    side: str                      # 'YES' | 'NO'
    expected_price: float
    size: float
    available_depth: float = 100.0 # depth at top of book
    expiry: str = ""
    order_type: str = "IOC"


@dataclass
class OrchestrationPlan:
    structure_id: str
    relation_id: str
    kind: str
    legs: list[OrchestratedLeg]
    expected_edge: float
    expected_payoff: float
    expected_cost: float
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class LegExecutionRecord:
    leg_index: int
    market_id: str
    venue: str
    side: str
    order_id: str
    status: str                    # 'FILLED' | 'PARTIAL' | 'FAILED' | 'UNWOUND'
    requested_size: float
    filled_size: float
    avg_price: float
    fee_paid: float
    unwound: bool = False
    unwind_order_id: str | None = None
    unwind_price: float = 0.0
    unwind_loss: float = 0.0


@dataclass
class OrchestrationResult:
    structure_id: str
    status: str                    # 'COMPLETED' | 'UNWOUND' | 'FAILED' | 'CIRCUIT_BREAKER_HALTED'
    total_cost_usd: float
    total_payoff_usd: float
    total_fees_usd: float
    realized_edge: float
    legging_loss_usd: float
    leg_records: list[LegExecutionRecord] = field(default_factory=list)
    error_message: str | None = None


class ExecutionOrchestrator:
    """Manages sequential leg execution, fill timeouts, and emergency unwind protection."""
    def __init__(self, router: ExecutionRouter | None = None,
                 audit_log: AuditLog | None = None,
                 cfg: Config | None = None,
                 alert_mgr: AlertManager | None = None,
                 leg_ordering_policy: str = "thinnest_first",
                 leg_timeout_sec: float = 5.0,
                 max_daily_legging_loss_usd: float = 50.0):
        self.router = router or ExecutionRouter(audit_log=audit_log, dry_run=True)
        self.audit_log = audit_log or getattr(self.router, "audit_log", AuditLog())
        self.cfg = cfg or Config()
        self.alert_mgr = alert_mgr or AlertManager.get_instance()
        self.leg_ordering_policy = leg_ordering_policy
        self.leg_timeout_sec = leg_timeout_sec
        self.max_daily_legging_loss_usd = max_daily_legging_loss_usd

        self.cumulative_legging_loss_usd: float = 0.0
        self.total_orchestrations: int = 0
        self.successful_orchestrations: int = 0
        self.unwound_orchestrations: int = 0

    def sequence_legs(self, legs: list[OrchestratedLeg]) -> list[OrchestratedLeg]:
        """Sequence legs according to configured execution policy.
        'thinnest_first': execute leg with the smallest depth first to minimize adverse selection.
        """
        if self.leg_ordering_policy == "thinnest_first":
            # Sort ascending by depth (thinnest first)
            return sorted(legs, key=lambda l: l.available_depth)
        elif self.leg_ordering_policy == "polymarket_first":
            return sorted(legs, key=lambda l: 0 if l.venue == "polymarket" else 1)
        elif self.leg_ordering_policy == "kalshi_first":
            return sorted(legs, key=lambda l: 0 if l.venue == "kalshi" else 1)
        return list(legs)

    async def execute_plan(self, plan: OrchestrationPlan) -> OrchestrationResult:
        """Executes multi-leg plan with strict fill confirmation and atomic unwind."""
        self.total_orchestrations += 1

        # 1. Kill Switch Check
        if is_kill_switch_active(self.cfg.data_dir):
            msg = "Execution halted: Kill switch active"
            logger.warning(f"[{plan.structure_id}] {msg}")
            return OrchestrationResult(
                structure_id=plan.structure_id,
                status="CIRCUIT_BREAKER_HALTED",
                total_cost_usd=0.0,
                total_payoff_usd=0.0,
                total_fees_usd=0.0,
                realized_edge=0.0,
                legging_loss_usd=0.0,
                error_message=msg,
            )

        # 2. Cumulative Legging Loss Circuit Breaker Check
        if self.cumulative_legging_loss_usd >= self.max_daily_legging_loss_usd:
            msg = f"Cumulative legging loss ${self.cumulative_legging_loss_usd:.2f} exceeded cap ${self.max_daily_legging_loss_usd:.2f}"
            logger.critical(f"[{plan.structure_id}] {msg}")
            trigger_kill_switch(msg, data_dir=self.cfg.data_dir)
            self.alert_mgr.notify("CRITICAL", "CIRCUIT_BREAKER", "Legging Circuit Breaker Tripped", msg)
            return OrchestrationResult(
                structure_id=plan.structure_id,
                status="CIRCUIT_BREAKER_HALTED",
                total_cost_usd=0.0,
                total_payoff_usd=0.0,
                total_fees_usd=0.0,
                realized_edge=0.0,
                legging_loss_usd=0.0,
                error_message=msg,
            )

        ordered_legs = self.sequence_legs(plan.legs)
        records: list[LegExecutionRecord] = []
        filled_legs_for_unwind: list[tuple[OrchestratedLeg, LegExecutionRecord]] = []

        logger.info(f"[{plan.structure_id}] Starting orchestration ({len(ordered_legs)} legs, policy: {self.leg_ordering_policy})")
        self.audit_log.log(
            action="ORCHESTRATION_START",
            venue="multi",
            order_id=plan.structure_id,
            price=plan.expected_cost,
            size=ordered_legs[0].size if ordered_legs else 0.0,
            metadata={"relation_id": plan.relation_id, "kind": plan.kind, "leg_count": len(ordered_legs)},
        )

        overall_failed = False
        failure_reason = ""

        for idx, leg in enumerate(ordered_legs):
            order_req = OrderRequest(
                market_id=leg.market_id,
                venue=leg.venue,
                side=leg.side,
                price=leg.expected_price,
                size=leg.size,
                order_type=leg.order_type,
            )

            try:
                # Execute with strict timeout
                res = await asyncio.wait_for(
                    self.router.execute_leg(order_req),
                    timeout=self.leg_timeout_sec,
                )
            except asyncio.TimeoutError:
                overall_failed = True
                failure_reason = f"Leg #{idx+1} ({leg.venue} {leg.market_id}) timed out after {self.leg_timeout_sec}s"
                logger.error(f"[{plan.structure_id}] {failure_reason}")
                rec = LegExecutionRecord(
                    leg_index=idx,
                    market_id=leg.market_id,
                    venue=leg.venue,
                    side=leg.side,
                    order_id=order_req.client_order_id,
                    status="FAILED",
                    requested_size=leg.size,
                    filled_size=0.0,
                    avg_price=0.0,
                    fee_paid=0.0,
                )
                records.append(rec)
                break
            except Exception as e:
                overall_failed = True
                failure_reason = f"Leg #{idx+1} exception: {e}"
                logger.error(f"[{plan.structure_id}] {failure_reason}")
                rec = LegExecutionRecord(
                    leg_index=idx,
                    market_id=leg.market_id,
                    venue=leg.venue,
                    side=leg.side,
                    order_id=order_req.client_order_id,
                    status="FAILED",
                    requested_size=leg.size,
                    filled_size=0.0,
                    avg_price=0.0,
                    fee_paid=0.0,
                )
                records.append(rec)
                break

            rec = LegExecutionRecord(
                leg_index=idx,
                market_id=leg.market_id,
                venue=leg.venue,
                side=leg.side,
                order_id=res.order_id,
                status=res.status,
                requested_size=leg.size,
                filled_size=res.filled_size,
                avg_price=res.avg_fill_price,
                fee_paid=res.fee_paid,
            )
            records.append(rec)

            if res.status == "FILLED":
                filled_legs_for_unwind.append((leg, rec))
                logger.info(f"[{plan.structure_id}] Leg #{idx+1} FILLED ({leg.venue} {leg.side} {res.filled_size}@{res.avg_fill_price})")
            else:
                overall_failed = True
                failure_reason = f"Leg #{idx+1} ({leg.venue}) did not fill (status: {res.status})"
                logger.warning(f"[{plan.structure_id}] {failure_reason}")
                break

        # 3. Handle Failure: Emergency Unwind
        if overall_failed:
            total_unwind_loss = 0.0
            if filled_legs_for_unwind:
                logger.warning(f"[{plan.structure_id}] Triggering emergency unwind for {len(filled_legs_for_unwind)} filled leg(s)")
                total_unwind_loss = await self._unwind_filled_legs(plan.structure_id, filled_legs_for_unwind)
                self.cumulative_legging_loss_usd += total_unwind_loss
                self.unwound_orchestrations += 1

                self.alert_mgr.notify(
                    "WARNING",
                    "LEGGING",
                    f"Legging Unwind for {plan.structure_id}",
                    f"Orchestration failed: {failure_reason}. Unwound filled legs with total loss: ${total_unwind_loss:.4f} USD.",
                    metadata={"structure_id": plan.structure_id, "loss_usd": total_unwind_loss},
                )

                # Check if this unwind breached the daily legging limit
                if self.cumulative_legging_loss_usd >= self.max_daily_legging_loss_usd:
                    halt_msg = f"Cumulative legging loss ${self.cumulative_legging_loss_usd:.2f} >= ${self.max_daily_legging_loss_usd:.2f}"
                    trigger_kill_switch(halt_msg, data_dir=self.cfg.data_dir)
                    self.alert_mgr.notify("CRITICAL", "CIRCUIT_BREAKER", "Legging Limit Reached", halt_msg)

                return OrchestrationResult(
                    structure_id=plan.structure_id,
                    status="UNWOUND",
                    total_cost_usd=0.0,
                    total_payoff_usd=0.0,
                    total_fees_usd=sum(r.fee_paid for r in records),
                    realized_edge=-total_unwind_loss,
                    legging_loss_usd=total_unwind_loss,
                    leg_records=records,
                    error_message=failure_reason,
                )
            else:
                # Failed on Leg 1 with zero fills -> clean zero-risk exit
                logger.info(f"[{plan.structure_id}] First leg failed without filling. Zero capital risk, no unwind needed.")
                return OrchestrationResult(
                    structure_id=plan.structure_id,
                    status="FAILED",
                    total_cost_usd=0.0,
                    total_payoff_usd=0.0,
                    total_fees_usd=0.0,
                    realized_edge=0.0,
                    legging_loss_usd=0.0,
                    leg_records=records,
                    error_message=failure_reason,
                )

        # 4. Successful Completion
        total_cost = sum(r.filled_size * r.avg_price for r in records)
        total_fees = sum(r.fee_paid for r in records)
        total_payoff = plan.expected_payoff * (records[0].filled_size if records else 0.0)
        realized_edge = total_payoff - (total_cost + total_fees)

        self.successful_orchestrations += 1
        logger.info(f"[{plan.structure_id}] Orchestration fully COMPLETED! Total cost: ${total_cost:.2f}, Edge: ${realized_edge:.2f}")

        self.audit_log.log(
            action="ORCHESTRATION_COMPLETED",
            venue="multi",
            order_id=plan.structure_id,
            price=round(total_cost / max(records[0].filled_size, 1e-6), 4),
            size=records[0].filled_size,
            status="FILLED",
            metadata={
                "total_cost_usd": total_cost,
                "total_fees_usd": total_fees,
                "realized_edge_usd": realized_edge,
            },
        )

        return OrchestrationResult(
            structure_id=plan.structure_id,
            status="COMPLETED",
            total_cost_usd=total_cost,
            total_payoff_usd=total_payoff,
            total_fees_usd=total_fees,
            realized_edge=realized_edge,
            legging_loss_usd=0.0,
            leg_records=records,
        )

    async def _unwind_filled_legs(self, structure_id: str,
                                  filled_legs: list[tuple[OrchestratedLeg, LegExecutionRecord]]) -> float:
        """Unwinds previously filled legs in reverse order by selling them at best bid."""
        total_loss = 0.0
        for leg, rec in reversed(filled_legs):
            # In prediction markets, opposite side of buying YES is selling YES or buying NO
            # In our IOC limit model, we place an unwind order to sell back the position
            unwind_side = "NO" if leg.side == "YES" else "YES"
            # Sell back with slight conservative discount (unwind haircut)
            unwind_price = max(0.01, round(rec.avg_price * 0.95, 3))
            unwind_req = OrderRequest(
                market_id=leg.market_id,
                venue=leg.venue,
                side=unwind_side,
                price=unwind_price,
                size=rec.filled_size,
                order_type="IOC",
            )

            try:
                unwind_res = await self.router.execute_leg(unwind_req)
                entry_spend = rec.filled_size * rec.avg_price
                unwind_proceeds = unwind_res.filled_size * unwind_res.avg_fill_price
                leg_loss = max(0.0, entry_spend - unwind_proceeds) + rec.fee_paid + unwind_res.fee_paid
                total_loss += leg_loss

                rec.unwound = True
                rec.unwind_order_id = unwind_res.order_id
                rec.unwind_price = unwind_res.avg_fill_price
                rec.unwind_loss = leg_loss

                self.audit_log.log(
                    action="LEGGING_UNWIND",
                    venue=leg.venue,
                    market_id=leg.market_id,
                    side=unwind_side,
                    price=unwind_res.avg_fill_price,
                    size=unwind_res.filled_size,
                    order_id=unwind_res.order_id,
                    status="UNWOUND",
                    metadata={
                        "structure_id": structure_id,
                        "original_order_id": rec.order_id,
                        "entry_spend": entry_spend,
                        "unwind_proceeds": unwind_proceeds,
                        "leg_loss_usd": leg_loss,
                    },
                )
            except Exception as e:
                logger.critical(f"[{structure_id}] Failed to unwind leg {leg.market_id}: {e}")
                # Full entry spend is at risk
                total_loss += rec.filled_size * rec.avg_price
        return total_loss
