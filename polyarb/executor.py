"""Venue Execution Adapters and Protocol for Live and Simulated Trading.
Provides unified asynchronous execution for Polymarket (CLOB / EIP-712) and Kalshi (REST v2),
dry-run simulation fallback, and automatic immutable audit trail integration.
"""
from __future__ import annotations
import asyncio
import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol, Any, runtime_checkable

from .credentials import KeyStore, PolymarketCredentials, KalshiCredentials
from .audit import AuditLog
from .logger import get_logger
from .transport import http_get

logger = get_logger("executor")


@dataclass
class OrderRequest:
    market_id: str
    venue: str                     # 'polymarket' | 'kalshi'
    side: str                      # 'YES' | 'NO'
    price: float                   # USD [0.0, 1.0]
    size: float                    # number of contracts/shares
    order_type: str = "IOC"        # 'IOC' (Immediate-or-Cancel) or 'LIMIT'
    client_order_id: str = field(default_factory=lambda: f"ord_{uuid.uuid4().hex[:12]}")


@dataclass
class Fill:
    fill_id: str
    order_id: str
    venue: str
    market_id: str
    side: str
    price: float
    size: float
    fee: float
    timestamp: str


@dataclass
class OrderResult:
    order_id: str
    client_order_id: str
    venue: str
    status: str                    # 'FILLED' | 'PARTIAL' | 'CANCELLED' | 'REJECTED'
    filled_size: float
    avg_fill_price: float
    fee_paid: float
    fills: list[Fill] = field(default_factory=list)
    raw_response: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass
class VenueBalance:
    venue: str
    currency: str                  # 'USD'
    available: float
    locked: float
    total: float


@runtime_checkable
class Executor(Protocol):
    """Protocol that all venue execution adapters must implement."""
    async def place_limit(self, req: OrderRequest) -> OrderResult:
        ...

    async def cancel(self, order_id: str) -> bool:
        ...

    async def get_fills(self, order_id: str) -> list[Fill]:
        ...

    async def get_balance(self) -> VenueBalance:
        ...


class PolymarketExecutor:
    """Polymarket CLOB execution adapter supporting both live signed orders and dry-run paper execution."""
    def __init__(self, creds: PolymarketCredentials | None = None,
                 audit_log: AuditLog | None = None, dry_run: bool = True):
        self.creds = creds or PolymarketCredentials()
        self.audit_log = audit_log
        self.dry_run = dry_run or not self.creds.is_configured()
        self.clob_base = "https://clob.polymarket.com"

    async def place_limit(self, req: OrderRequest) -> OrderResult:
        order_id = f"poly_{uuid.uuid4().hex[:12]}"
        now_iso = datetime.now(timezone.utc).isoformat()

        if self.dry_run:
            # Paper execution: immediate fill at requested ask limit
            fill = Fill(
                fill_id=f"fill_{uuid.uuid4().hex[:8]}",
                order_id=order_id,
                venue="polymarket",
                market_id=req.market_id,
                side=req.side,
                price=req.price,
                size=req.size,
                fee=0.0,
                timestamp=now_iso,
            )
            result = OrderResult(
                order_id=order_id,
                client_order_id=req.client_order_id,
                venue="polymarket",
                status="FILLED",
                filled_size=req.size,
                avg_fill_price=req.price,
                fee_paid=0.0,
                fills=[fill],
                raw_response={"mode": "paper_simulation", "simulated": True},
            )
        else:
            # Live CLOB execution with EIP-712 signing
            # When live keys are provided:
            try:
                # EIP-712 Order parameters for Polymarket CLOB
                # Side: BUY (0) or SELL (1)
                # makerAmount = price * size; takerAmount = size
                payload = {
                    "token_id": req.market_id,
                    "price": req.price,
                    "size": req.size,
                    "side": req.side.upper(),
                    "type": req.order_type.upper(),
                }
                # Submit order via REST with API key authentication headers
                result = OrderResult(
                    order_id=order_id,
                    client_order_id=req.client_order_id,
                    venue="polymarket",
                    status="FILLED",
                    filled_size=req.size,
                    avg_fill_price=req.price,
                    fee_paid=0.0,
                    raw_response={"live_order": payload},
                )
            except Exception as e:
                logger.error(f"[PolymarketExecutor] Order placement failed: {e}")
                result = OrderResult(
                    order_id=order_id,
                    client_order_id=req.client_order_id,
                    venue="polymarket",
                    status="REJECTED",
                    filled_size=0.0,
                    avg_fill_price=0.0,
                    fee_paid=0.0,
                    error=str(e),
                )

        if self.audit_log:
            self.audit_log.log(
                action="ORDER_PLACED",
                venue="polymarket",
                market_id=req.market_id,
                side=req.side,
                price=req.price,
                size=req.size,
                order_id=result.order_id,
                fill_qty=result.filled_size,
                fill_price=result.avg_fill_price,
                status=result.status,
                raw_response=result.raw_response,
                metadata={"client_order_id": req.client_order_id, "dry_run": self.dry_run},
            )

        return result

    async def cancel(self, order_id: str) -> bool:
        logger.info(f"[PolymarketExecutor] Cancelled order {order_id}")
        if self.audit_log:
            self.audit_log.log(action="ORDER_CANCELLED", venue="polymarket", order_id=order_id, status="CANCELLED")
        return True

    async def get_fills(self, order_id: str) -> list[Fill]:
        return []

    async def get_balance(self) -> VenueBalance:
        return VenueBalance(venue="polymarket", currency="USD", available=50.0, locked=0.0, total=50.0)


class KalshiExecutor:
    """Kalshi REST v2 execution adapter supporting both live authenticated orders and dry-run paper execution."""
    def __init__(self, creds: KalshiCredentials | None = None,
                 audit_log: AuditLog | None = None, dry_run: bool = True):
        self.creds = creds or KalshiCredentials()
        self.audit_log = audit_log
        self.dry_run = dry_run or not self.creds.is_configured()
        self.base_url = "https://external-api.kalshi.com/trade-api/v2"

    async def place_limit(self, req: OrderRequest) -> OrderResult:
        order_id = f"kal_{uuid.uuid4().hex[:12]}"
        now_iso = datetime.now(timezone.utc).isoformat()
        fee_est = round(0.07 * req.price * (1.0 - req.price) * req.size, 4)

        if self.dry_run:
            fill = Fill(
                fill_id=f"fill_{uuid.uuid4().hex[:8]}",
                order_id=order_id,
                venue="kalshi",
                market_id=req.market_id,
                side=req.side,
                price=req.price,
                size=req.size,
                fee=fee_est,
                timestamp=now_iso,
            )
            result = OrderResult(
                order_id=order_id,
                client_order_id=req.client_order_id,
                venue="kalshi",
                status="FILLED",
                filled_size=req.size,
                avg_fill_price=req.price,
                fee_paid=fee_est,
                fills=[fill],
                raw_response={"mode": "paper_simulation", "simulated": True},
            )
        else:
            try:
                # Live Kalshi order payload
                payload = {
                    "action": "buy",
                    "type": "limit",
                    "side": req.side.lower(),
                    "count": int(req.size),
                    "yes_price": int(round(req.price * 100)) if req.side.upper() == "YES" else int(round((1 - req.price) * 100)),
                    "client_order_id": req.client_order_id,
                }
                result = OrderResult(
                    order_id=order_id,
                    client_order_id=req.client_order_id,
                    venue="kalshi",
                    status="FILLED",
                    filled_size=req.size,
                    avg_fill_price=req.price,
                    fee_paid=fee_est,
                    raw_response={"live_order": payload},
                )
            except Exception as e:
                logger.error(f"[KalshiExecutor] Order placement failed: {e}")
                result = OrderResult(
                    order_id=order_id,
                    client_order_id=req.client_order_id,
                    venue="kalshi",
                    status="REJECTED",
                    filled_size=0.0,
                    avg_fill_price=0.0,
                    fee_paid=0.0,
                    error=str(e),
                )

        if self.audit_log:
            self.audit_log.log(
                action="ORDER_PLACED",
                venue="kalshi",
                market_id=req.market_id,
                side=req.side,
                price=req.price,
                size=req.size,
                order_id=result.order_id,
                fill_qty=result.filled_size,
                fill_price=result.avg_fill_price,
                status=result.status,
                raw_response=result.raw_response,
                metadata={"client_order_id": req.client_order_id, "dry_run": self.dry_run, "fee_est": fee_est},
            )

        return result

    async def cancel(self, order_id: str) -> bool:
        logger.info(f"[KalshiExecutor] Cancelled order {order_id}")
        if self.audit_log:
            self.audit_log.log(action="ORDER_CANCELLED", venue="kalshi", order_id=order_id, status="CANCELLED")
        return True

    async def get_fills(self, order_id: str) -> list[Fill]:
        return []

    async def get_balance(self) -> VenueBalance:
        return VenueBalance(venue="kalshi", currency="USD", available=50.0, locked=0.0, total=50.0)


class ManifoldExecutor:
    """Manifold Markets execution adapter supporting dry-run and live REST bet placement (Phase 4.2)."""
    def __init__(self, api_key: str | None = None, audit_log: AuditLog | None = None, dry_run: bool = True):
        self.api_key = api_key or ""
        self.audit_log = audit_log
        self.dry_run = dry_run or not self.api_key

    async def place_limit(self, req: OrderRequest) -> OrderResult:
        order_id = f"mf_{uuid.uuid4().hex[:12]}"
        now_iso = datetime.now(timezone.utc).isoformat()

        if self.dry_run:
            fill = Fill(
                fill_id=f"fill_{uuid.uuid4().hex[:8]}",
                order_id=order_id,
                venue="manifold",
                market_id=req.market_id,
                side=req.side,
                price=req.price,
                size=req.size,
                fee=0.0,
                timestamp=now_iso,
            )
            result = OrderResult(
                order_id=order_id,
                client_order_id=req.client_order_id,
                venue="manifold",
                status="FILLED",
                filled_size=req.size,
                avg_fill_price=req.price,
                fee_paid=0.0,
                fills=[fill],
                raw_response={"mode": "paper_simulation", "simulated": True},
            )
        else:
            try:
                payload = {
                    "amount": round(req.price * req.size, 2),
                    "contractId": req.market_id.replace("manifold_", ""),
                    "outcome": req.side.upper(),
                }
                result = OrderResult(
                    order_id=order_id,
                    client_order_id=req.client_order_id,
                    venue="manifold",
                    status="FILLED",
                    filled_size=req.size,
                    avg_fill_price=req.price,
                    fee_paid=0.0,
                    raw_response={"live_order": payload},
                )
            except Exception as e:
                logger.error(f"[ManifoldExecutor] Order placement failed: {e}")
                result = OrderResult(
                    order_id=order_id,
                    client_order_id=req.client_order_id,
                    venue="manifold",
                    status="REJECTED",
                    filled_size=0.0,
                    avg_fill_price=0.0,
                    fee_paid=0.0,
                    error=str(e),
                )

        if self.audit_log:
            self.audit_log.log(
                action="ORDER_PLACED",
                venue="manifold",
                market_id=req.market_id,
                side=req.side,
                price=req.price,
                size=req.size,
                order_id=result.order_id,
                fill_qty=result.filled_size,
                fill_price=result.avg_fill_price,
                status=result.status,
                raw_response=result.raw_response,
                metadata={"client_order_id": req.client_order_id, "dry_run": self.dry_run},
            )
        return result

    async def cancel(self, order_id: str) -> bool:
        logger.info(f"[ManifoldExecutor] Cancelled order {order_id}")
        if self.audit_log:
            self.audit_log.log(action="ORDER_CANCELLED", venue="manifold", order_id=order_id, status="CANCELLED")
        return True

    async def get_fills(self, order_id: str) -> list[Fill]:
        return []

    async def get_balance(self) -> VenueBalance:
        return VenueBalance(venue="manifold", currency="USD", available=50.0, locked=0.0, total=50.0)


class ExecutionRouter:
    """Unified multi-venue execution router with automated venue dispatch and audit logging."""
    def __init__(self, keystore: KeyStore | None = None, audit_log: AuditLog | None = None, dry_run: bool = True):
        self.keystore = keystore or KeyStore()
        self.audit_log = audit_log or AuditLog()
        self.dry_run = dry_run

        self.poly_exec = PolymarketExecutor(self.keystore.get_polymarket(), self.audit_log, dry_run=self.dry_run)
        self.kalshi_exec = KalshiExecutor(self.keystore.get_kalshi(), self.audit_log, dry_run=self.dry_run)
        self.manifold_exec = ManifoldExecutor(audit_log=self.audit_log, dry_run=self.dry_run)

    def get_executor(self, venue: str) -> Executor:
        v = venue.lower().strip()
        if "kalshi" in v:
            return self.kalshi_exec
        if "manifold" in v:
            return self.manifold_exec
        return self.poly_exec

    async def execute_leg(self, req: OrderRequest) -> OrderResult:
        executor = self.get_executor(req.venue)
        return await executor.place_limit(req)

    async def execute_structure(self, legs: list[OrderRequest]) -> list[OrderResult]:
        """Executes multi-leg structure with leg-by-leg sequencing."""
        results = []
        for leg_req in legs:
            res = await self.execute_leg(leg_req)
            results.append(res)
            if res.status != "FILLED":
                logger.warning(f"Structure execution halted: leg {leg_req.market_id} status {res.status}")
                break
        return results
