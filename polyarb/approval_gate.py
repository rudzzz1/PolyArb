"""Human-in-the-Loop Approval Gate (Phase 2.2).
Provides safety queue where candidate trades must be explicitly reviewed and approved
by an operator before real orders are dispatched to live venues.
Supports auto-expiration of stale quotes and gradual transition to full automation after N verified trades.
"""
from __future__ import annotations
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .audit import AuditLog
from .logger import get_logger

logger = get_logger("approval_gate")


@dataclass
class ApprovalItem:
    id: str
    relation_id: str
    kind: str
    event_title: str
    legs: list[dict[str, Any]]
    unit_cost: float
    expected_payoff: float
    net_edge: float
    shares: float
    total_cost_usd: float
    apy: float | None
    risk_flags: list[str]
    created_at: str
    expires_at: str
    status: str = "PENDING"        # 'PENDING' | 'APPROVED' | 'REJECTED' | 'EXPIRED' | 'EXECUTED'
    approved_by: str | None = None
    approved_at: str | None = None
    rejection_reason: str | None = None
    operator_notes: str | None = None
    execution_result: dict[str, Any] | None = None


class ApprovalGate:
    """Manages the human-in-the-loop trade review workflow."""
    def __init__(self, audit_log: AuditLog | None = None,
                 mode: str = "HUMAN_APPROVAL",
                 quote_ttl_sec: float = 60.0,
                 auto_mode_threshold: int = 50):
        self.audit_log = audit_log or AuditLog()
        self.mode = mode.upper()     # 'PAPER' | 'HUMAN_APPROVAL' | 'AUTO'
        self.quote_ttl_sec = quote_ttl_sec
        self.auto_mode_threshold = auto_mode_threshold
        self.manual_approved_count: int = 0
        self.items: dict[str, ApprovalItem] = {}

    def is_auto_allowed(self) -> bool:
        """Auto execution is unlocked only after required manual trades are reviewed."""
        return self.manual_approved_count >= self.auto_mode_threshold

    def set_mode(self, new_mode: str) -> tuple[bool, str]:
        m = new_mode.upper()
        if m not in ("PAPER", "HUMAN_APPROVAL", "AUTO"):
            return False, f"Invalid mode: {new_mode}"
        if m == "AUTO" and not self.is_auto_allowed():
            return False, f"Cannot switch to AUTO: requires {self.auto_mode_threshold} manual approvals (current: {self.manual_approved_count})"
        self.mode = m
        self.audit_log.log(action="APPROVAL_MODE_CHANGED", venue="system", status=self.mode)
        logger.info(f"Execution mode switched to: {self.mode}")
        return True, f"Execution mode set to {self.mode}"

    def submit_for_approval(self, relation_id: str, kind: str, event_title: str,
                            legs: list[dict[str, Any]], unit_cost: float,
                            expected_payoff: float, net_edge: float, shares: float,
                            apy: float | None = None,
                            risk_flags: list[str] | None = None) -> ApprovalItem:
        """Queues a candidate trade for human review."""
        now = time.time()
        now_iso = datetime.now(timezone.utc).isoformat()
        exp_iso = datetime.fromtimestamp(now + self.quote_ttl_sec, tz=timezone.utc).isoformat()
        item_id = f"app_{uuid.uuid4().hex[:10]}"

        item = ApprovalItem(
            id=item_id,
            relation_id=relation_id,
            kind=kind,
            event_title=event_title,
            legs=legs,
            unit_cost=round(unit_cost, 4),
            expected_payoff=round(expected_payoff, 4),
            net_edge=round(net_edge, 4),
            shares=round(shares, 2),
            total_cost_usd=round(unit_cost * shares, 2),
            apy=round(apy, 4) if apy else None,
            risk_flags=risk_flags or [],
            created_at=now_iso,
            expires_at=exp_iso,
            status="PENDING",
        )
        self.items[item_id] = item
        logger.info(f"Trade candidate queued for approval [{item_id}]: {kind} {event_title} (edge: ${net_edge:.3f})")

        self.audit_log.log(
            action="APPROVAL_REQUEST_CREATED",
            venue="queue",
            order_id=item_id,
            price=item.unit_cost,
            size=item.shares,
            metadata={"relation_id": relation_id, "kind": kind, "edge": net_edge, "flags": risk_flags},
        )
        return item

    def approve(self, item_id: str, approved_by: str = "human_operator", notes: str | None = None) -> tuple[bool, str, ApprovalItem | None]:
        """Approves a pending candidate."""
        item = self.items.get(item_id)
        if not item:
            return False, "Approval item not found", None

        # Check expiration
        now_iso = datetime.now(timezone.utc).isoformat()
        if now_iso > item.expires_at and item.status == "PENDING":
            item.status = "EXPIRED"
            self.audit_log.log(action="APPROVAL_EXPIRED", venue="queue", order_id=item_id, status="EXPIRED")
            return False, "Approval request expired (quotes became stale)", item

        if item.status != "PENDING":
            return False, f"Item cannot be approved: current status is {item.status}", item

        item.status = "APPROVED"
        item.approved_by = approved_by
        item.approved_at = now_iso
        item.operator_notes = notes
        self.manual_approved_count += 1

        self.audit_log.log(
            action="APPROVAL_GRANTED",
            venue="queue",
            order_id=item_id,
            status="APPROVED",
            metadata={"approved_by": approved_by, "notes": notes, "approved_count": self.manual_approved_count},
        )
        logger.info(f"Trade {item_id} APPROVED by {approved_by} (Total approved: {self.manual_approved_count})")
        return True, "Approved successfully", item

    def reject(self, item_id: str, reason: str = "user_declined", notes: str | None = None) -> tuple[bool, str, ApprovalItem | None]:
        """Rejects a pending candidate."""
        item = self.items.get(item_id)
        if not item:
            return False, "Approval item not found", None

        if item.status != "PENDING":
            return False, f"Item cannot be rejected: current status is {item.status}", item

        item.status = "REJECTED"
        item.rejection_reason = reason
        item.operator_notes = notes
        self.audit_log.log(
            action="APPROVAL_REJECTED",
            venue="queue",
            order_id=item_id,
            status="REJECTED",
            metadata={"reason": reason, "notes": notes},
        )
        logger.info(f"Trade {item_id} REJECTED: {reason}")
        return True, f"Rejected: {reason}", item

    def get_pending_items(self) -> list[dict[str, Any]]:
        """Returns all currently active pending approval requests."""
        now_iso = datetime.now(timezone.utc).isoformat()
        res = []
        for item in self.items.values():
            if item.status == "PENDING":
                if now_iso > item.expires_at:
                    item.status = "EXPIRED"
                else:
                    res.append(self._item_dict(item))
        return res

    def get_all_items(self, limit: int = 50) -> list[dict[str, Any]]:
        """Returns all approval history sorted newest first."""
        sorted_items = sorted(self.items.values(), key=lambda i: i.created_at, reverse=True)
        return [self._item_dict(i) for i in sorted_items[:limit]]

    def _item_dict(self, item: ApprovalItem) -> dict[str, Any]:
        ttl_remaining = 0.0
        try:
            exp_dt = datetime.fromisoformat(item.expires_at)
            ttl_remaining = max(0.0, round((exp_dt - datetime.now(timezone.utc)).total_seconds(), 1))
        except Exception:
            pass

        return {
            "id": item.id,
            "relation_id": item.relation_id,
            "kind": item.kind,
            "event_title": item.event_title,
            "legs": item.legs,
            "unit_cost": item.unit_cost,
            "expected_payoff": item.expected_payoff,
            "net_edge": item.net_edge,
            "shares": item.shares,
            "total_cost_usd": item.total_cost_usd,
            "apy": item.apy,
            "risk_flags": item.risk_flags,
            "created_at": item.created_at,
            "expires_at": item.expires_at,
            "ttl_remaining_sec": ttl_remaining,
            "status": item.status,
            "approved_by": item.approved_by,
            "approved_at": item.approved_at,
            "rejection_reason": item.rejection_reason,
            "operator_notes": item.operator_notes,
        }
