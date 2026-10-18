"""Fill Reconciliation and Slippage Tracking (Phase 2.3).
Monitors execution quality by comparing expected quotes vs actual fills,
tracks cumulative execution slippage across venues, and reconciles internal balances
with venue exchange ledgers.
"""
from __future__ import annotations
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .audit import AuditLog
from .alerts import AlertManager
from .executor import ExecutionRouter, VenueBalance
from .logger import get_logger

logger = get_logger("reconciliation")


@dataclass
class FillReconciliation:
    reconciliation_id: str
    order_id: str
    venue: str
    market_id: str
    side: str
    expected_price: float
    actual_fill_price: float
    slippage_bps: float
    expected_size: float
    filled_size: float
    size_discrepancy: float
    fee_paid: float
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class BalanceReconciliation:
    venue: str
    internal_balance: float
    exchange_balance: float
    difference: float
    status: str                    # 'MATCH' | 'DISCREPANCY'
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class ReconciliationManager:
    """Tracks fill slippage, detects execution discrepancies, and verifies venue balances."""
    def __init__(self, audit_log: AuditLog | None = None,
                 alert_mgr: AlertManager | None = None,
                 max_slippage_bps: float = 50.0,
                 max_cumulative_slippage_bps: float = 250.0):
        self.audit_log = audit_log or AuditLog()
        self.alert_mgr = alert_mgr or AlertManager.get_instance()
        self.max_slippage_bps = max_slippage_bps
        self.max_cumulative_slippage_bps = max_cumulative_slippage_bps

        self.fill_records: list[FillReconciliation] = []
        self.balance_records: list[BalanceReconciliation] = []
        self.cumulative_slippage_bps: float = 0.0

    def reconcile_fill(self, order_id: str, venue: str, market_id: str, side: str,
                       expected_price: float, actual_price: float,
                       expected_size: float, filled_size: float,
                       fee_paid: float = 0.0) -> FillReconciliation:
        """Computes execution slippage and verifies fill consistency."""
        # For buying (asks), positive slippage = paid more than expected
        if expected_price > 1e-6:
            slippage_bps = ((actual_price - expected_price) / expected_price) * 10000.0
        else:
            slippage_bps = 0.0

        size_diff = expected_size - filled_size
        rec_id = f"rec_{int(time.time() * 1000)}"

        rec = FillReconciliation(
            reconciliation_id=rec_id,
            order_id=order_id,
            venue=venue,
            market_id=market_id,
            side=side,
            expected_price=expected_price,
            actual_fill_price=actual_price,
            slippage_bps=round(slippage_bps, 2),
            expected_size=expected_size,
            filled_size=filled_size,
            size_discrepancy=round(size_diff, 4),
            fee_paid=fee_paid,
        )
        self.fill_records.append(rec)
        self.cumulative_slippage_bps = round(self.cumulative_slippage_bps + max(0.0, slippage_bps), 2)

        # Audit logging
        self.audit_log.log(
            action="FILL_RECONCILED",
            venue=venue,
            market_id=market_id,
            side=side,
            price=actual_price,
            size=filled_size,
            order_id=order_id,
            metadata={
                "expected_price": expected_price,
                "slippage_bps": slippage_bps,
                "fee_paid": fee_paid,
                "cumulative_slippage_bps": self.cumulative_slippage_bps,
            },
        )

        # Single trade slippage threshold check
        if slippage_bps > self.max_slippage_bps:
            msg = f"Order {order_id} ({venue}) slippage {slippage_bps:.1f} bps exceeded tolerance ({self.max_slippage_bps} bps)"
            logger.warning(f"[SLIPPAGE ALERT] {msg}")
            self.alert_mgr.notify("WARNING", "SLIPPAGE", "High Slippage Detected", msg, metadata={"slippage_bps": slippage_bps})

        # Cumulative slippage threshold check
        if self.cumulative_slippage_bps > self.max_cumulative_slippage_bps:
            msg = f"Cumulative slippage {self.cumulative_slippage_bps:.1f} bps exceeded threshold ({self.max_cumulative_slippage_bps} bps)"
            logger.critical(f"[CUMULATIVE SLIPPAGE BREACH] {msg}")
            self.alert_mgr.notify("CRITICAL", "SLIPPAGE", "Cumulative Slippage Cap Breached", msg)

        return rec

    async def reconcile_venue_balances(self, router: ExecutionRouter,
                                       internal_poly_usd: float,
                                       internal_kalshi_usd: float) -> list[BalanceReconciliation]:
        """Queries live/dry-run balances from venue executors and reconciles against internal ledger."""
        recs = []

        # 1. Polymarket
        try:
            poly_bal = await router.get_executor("polymarket").get_balance()
            diff_poly = round(abs(internal_poly_usd - poly_bal.available), 2)
            status_poly = "MATCH" if diff_poly < 1.0 else "DISCREPANCY"
            r_poly = BalanceReconciliation(
                venue="polymarket",
                internal_balance=internal_poly_usd,
                exchange_balance=poly_bal.available,
                difference=diff_poly,
                status=status_poly,
            )
            recs.append(r_poly)
            self.balance_records.append(r_poly)
            if status_poly == "DISCREPANCY":
                logger.warning(f"Polymarket balance discrepancy: internal ${internal_poly_usd:.2f} vs venue ${poly_bal.available:.2f}")
        except Exception as e:
            logger.error(f"Failed to reconcile Polymarket balance: {e}")

        # 2. Kalshi
        try:
            kalshi_bal = await router.get_executor("kalshi").get_balance()
            diff_kalshi = round(abs(internal_kalshi_usd - kalshi_bal.available), 2)
            status_kalshi = "MATCH" if diff_kalshi < 1.0 else "DISCREPANCY"
            r_kalshi = BalanceReconciliation(
                venue="kalshi",
                internal_balance=internal_kalshi_usd,
                exchange_balance=kalshi_bal.available,
                difference=diff_kalshi,
                status=status_kalshi,
            )
            recs.append(r_kalshi)
            self.balance_records.append(r_kalshi)
            if status_kalshi == "DISCREPANCY":
                logger.warning(f"Kalshi balance discrepancy: internal ${internal_kalshi_usd:.2f} vs venue ${kalshi_bal.available:.2f}")
        except Exception as e:
            logger.error(f"Failed to reconcile Kalshi balance: {e}")

        return recs

    def get_summary(self) -> dict[str, Any]:
        """Summary for UI dashboard."""
        return {
            "total_reconciled_fills": len(self.fill_records),
            "cumulative_slippage_bps": round(self.cumulative_slippage_bps, 2),
            "recent_fills": [
                {
                    "order_id": r.order_id,
                    "venue": r.venue,
                    "slippage_bps": r.slippage_bps,
                    "actual_price": r.actual_fill_price,
                    "expected_price": r.expected_price,
                    "timestamp": r.timestamp,
                }
                for r in reversed(self.fill_records[-20:])
            ],
            "recent_balance_checks": [
                {
                    "venue": r.venue,
                    "internal": r.internal_balance,
                    "exchange": r.exchange_balance,
                    "difference": r.difference,
                    "status": r.status,
                    "timestamp": r.timestamp,
                }
                for r in reversed(self.balance_records[-10:])
            ]
        }
