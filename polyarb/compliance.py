"""Tax and Regulatory Compliance Engine (Phase 6.5).
Provides:
1. FIFO (First-In, First-Out) cost basis accounting for prediction market contracts.
2. Tax-compliant CSV export (IRS Form 8949 / Schedule D compatible format).
3. Geo-jurisdiction compliance verification for offshore prediction venue rules.
"""
from __future__ import annotations
import csv
import io
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .logger import get_logger

logger = get_logger("compliance")


@dataclass
class TaxLot:
    lot_id: str
    market_id: str
    side: str
    shares: float
    unit_cost_usd: float
    total_cost_usd: float
    fee_paid_usd: float
    acquired_at: str


@dataclass
class RealizedTaxEvent:
    lot_id: str
    market_id: str
    description: str
    shares: float
    acquired_at: str
    disposed_at: str
    proceeds_usd: float
    cost_basis_usd: float
    gain_loss_usd: float
    is_short_term: bool
    venue: str


class FifoCostBasisTracker:
    """Tracks token inventory in First-In, First-Out queues to calculate tax cost basis."""

    def __init__(self):
        # Queues of open lots keyed by (market_id, side)
        self.open_lots: dict[tuple[str, str], list[TaxLot]] = {}
        self.realized_events: list[RealizedTaxEvent] = []

    def record_buy(
        self,
        lot_id: str,
        market_id: str,
        side: str,
        shares: float,
        unit_price_usd: float,
        fee_paid_usd: float = 0.0,
        acquired_at: str | None = None,
    ):
        """Records an acquisition lot."""
        ts = acquired_at or datetime.now(timezone.utc).isoformat()
        lot = TaxLot(
            lot_id=lot_id,
            market_id=market_id,
            side=side.upper(),
            shares=round(shares, 4),
            unit_cost_usd=round(unit_price_usd, 4),
            total_cost_usd=round(shares * unit_price_usd + fee_paid_usd, 4),
            fee_paid_usd=round(fee_paid_usd, 4),
            acquired_at=ts,
        )
        key = (market_id, side.upper())
        if key not in self.open_lots:
            self.open_lots[key] = []
        self.open_lots[key].append(lot)

    def record_disposition(
        self,
        market_id: str,
        side: str,
        shares: float,
        unit_proceeds_usd: float,
        venue: str = "polymarket",
        disposed_at: str | None = None,
        description: str = "",
    ) -> list[RealizedTaxEvent]:
        """Matches a sale or settlement against earliest available lots (FIFO)."""
        now_ts = disposed_at or datetime.now(timezone.utc).isoformat()
        key = (market_id, side.upper())
        lots = self.open_lots.get(key, [])
        remaining_to_dispose = round(shares, 4)
        events = []

        while remaining_to_dispose > 0 and lots:
            lot = lots[0]
            matched_shares = min(remaining_to_dispose, lot.shares)
            ratio = matched_shares / lot.shares
            cost_basis = round(lot.total_cost_usd * ratio, 4)
            proceeds = round(matched_shares * unit_proceeds_usd, 4)
            gain_loss = round(proceeds - cost_basis, 4)

            # Check holding period (<= 365 days is short-term)
            try:
                acq_dt = datetime.fromisoformat(lot.acquired_at)
                disp_dt = datetime.fromisoformat(now_ts)
                holding_days = (disp_dt - acq_dt).total_seconds() / 86400.0
                is_short_term = holding_days <= 365.0
            except Exception:
                is_short_term = True

            event = RealizedTaxEvent(
                lot_id=lot.lot_id,
                market_id=market_id,
                description=description or f"{venue.title()} {market_id} {side}",
                shares=matched_shares,
                acquired_at=lot.acquired_at,
                disposed_at=now_ts,
                proceeds_usd=proceeds,
                cost_basis_usd=cost_basis,
                gain_loss_usd=gain_loss,
                is_short_term=is_short_term,
                venue=venue,
            )
            events.append(event)
            self.realized_events.append(event)

            lot.shares -= matched_shares
            lot.total_cost_usd -= cost_basis
            remaining_to_dispose -= matched_shares

            if lot.shares <= 0.0001:
                lots.pop(0)

        return events

    def export_csv(self, file_path: str | Path | None = None) -> str:
        """Exports tax disposition ledger to CSV (Form 8949 compatible)."""
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "Description of Property",
            "Date Acquired",
            "Date Sold or Disposed",
            "Proceeds (USD)",
            "Cost Basis (USD)",
            "Gain or Loss (USD)",
            "Holding Type",
            "Venue",
        ])

        for ev in self.realized_events:
            writer.writerow([
                ev.description,
                ev.acquired_at[:10],
                ev.disposed_at[:10],
                f"{ev.proceeds_usd:.2f}",
                f"{ev.cost_basis_usd:.2f}",
                f"{ev.gain_loss_usd:.2f}",
                "Short-Term" if ev.is_short_term else "Long-Term",
                ev.venue.upper(),
            ])

        csv_str = output.getvalue()
        if file_path:
            p = Path(file_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(csv_str, encoding="utf-8")
        return csv_str


class JurisdictionCompliance:
    """Verifies jurisdictional compliance for prediction market operators."""

    RESTRICTED_COUNTRIES = {
        "US": "United States (CFTC restrictions on Polymarket offshore CLOB)",
        "IR": "Iran (OFAC Sanctions)",
        "KP": "North Korea (OFAC Sanctions)",
        "SY": "Syria (OFAC Sanctions)",
        "CU": "Cuba (OFAC Sanctions)",
    }

    @classmethod
    def check_jurisdiction(cls, country_code: str | None = None) -> dict[str, Any]:
        """Checks if operator environment complies with prediction venue regulatory constraints."""
        code = (country_code or os.environ.get("POLYARB_JURISDICTION", "IN")).upper().strip()
        is_restricted = code in cls.RESTRICTED_COUNTRIES
        warning = cls.RESTRICTED_COUNTRIES.get(code, "")

        return {
            "country_code": code,
            "compliant": not is_restricted,
            "warning": warning if is_restricted else "Jurisdiction compliant for global prediction markets.",
            "recommended_action": "Enable approved overseas VPN/proxy routing" if is_restricted else "None",
        }
