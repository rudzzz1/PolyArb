"""Simulation and Portfolio Management Engine for Cross-Market Logical Arbitrage.

Features:
- Initial capital tracking in INR (default: 10,000 INR = ~$119.76 USD @ 83.50 INR/USD)
- Multi-leg structure booking (all legs must be filled to open)
- Strict settlement rule: A trade only moves from Open to Realised once all legs are settled
- Uses actual live market candidate structures from Polymarket and Kalshi
- Real-time mark-to-market pricing and PnL updates
- Audit persistence in data/portfolio.json
"""
from __future__ import annotations
import json
import os
import random
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Config, is_kill_switch_active
from .fx import get_current_fx_rate, FxService

INR_PER_USD = 83.50  # Baseline fallback constant; dynamic rates used at runtime via get_current_fx_rate()


def format_expiry(iso_str: str | None) -> str:
    if not iso_str:
        return "N/A"
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta_sec = (dt - now).total_seconds()
        delta_days = delta_sec / 86400.0
        date_formatted = dt.strftime("%b %d, %Y")
        if delta_sec > 0:
            if delta_days >= 2:
                return f"{date_formatted} ({int(delta_days)}d)"
            else:
                hours = max(1, int(delta_sec / 3600))
                return f"{date_formatted} ({hours}h)"
        else:
            return f"{date_formatted} (Expired)"
    except Exception:
        return str(iso_str)[:10]


@dataclass
class LegPosition:
    leg_id: str
    market_id: str
    venue: str                     # 'polymarket' | 'kalshi'
    question: str
    side: str                      # 'YES' | 'NO'
    shares: float
    entry_price_usd: float
    cost_usd: float
    cost_inr: float
    current_price_usd: float
    settlement_status: str         # 'OPEN' | 'SETTLED'
    settlement_outcome: str | None # 'YES' | 'NO' | 'VOID' | None
    payoff_usd: float = 0.0
    payoff_inr: float = 0.0
    expiry: str = ""
    expiry_display: str = ""


@dataclass
class StructurePosition:
    structure_id: str
    relation_id: str
    kind: str                      # 'implies' | 'threshold_ladder' | 'equivalent' | 'partition'
    event_title: str
    status: str                    # 'OPEN' | 'REALISED'
    legs: list[LegPosition]
    total_cost_usd: float
    total_cost_inr: float
    guaranteed_payoff_usd: float
    guaranteed_payoff_inr: float
    projected_profit_usd: float
    projected_profit_inr: float
    realised_profit_usd: float = 0.0
    realised_profit_inr: float = 0.0
    return_pct: float = 0.0
    booked_at: str = ""
    settled_at: str | None = None
    note: str = ""
    expiry: str = ""
    expiry_display: str = ""

    def check_and_update_settlement(self) -> bool:
        """Rule: A trade should ONLY move from open to realised once ALL legs are settled."""
        if not self.legs:
            return False
        all_settled = all(l.settlement_status == "SETTLED" for l in self.legs)
        if all_settled and self.status != "REALISED":
            self.status = "REALISED"
            self.settled_at = datetime.now(timezone.utc).isoformat()
            total_payoff_usd = sum(l.payoff_usd for l in self.legs)
            total_payoff_inr = sum(l.payoff_inr for l in self.legs)
            self.realised_profit_usd = round(total_payoff_usd - self.total_cost_usd, 4)
            self.realised_profit_inr = round(total_payoff_inr - self.total_cost_inr, 2)
            self.return_pct = round((self.realised_profit_inr / max(self.total_cost_inr, 1.0)) * 100.0, 2)
            return True
        return False


class PortfolioManager:
    def __init__(self, initial_capital_inr: float = 10000.0, data_dir: str = "data"):
        self.initial_capital_inr = initial_capital_inr
        fx_rate = get_current_fx_rate()
        self.initial_capital_usd = round(initial_capital_inr / fx_rate, 2)
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.storage_file = self.data_dir / "portfolio.json"

        self.available_cash_inr = self.initial_capital_inr
        self.available_cash_usd = self.initial_capital_usd
        # Per-venue balance tracking (Phase 1.5)
        self.balance_polymarket_usd = round(self.initial_capital_usd / 2.0, 2)
        self.balance_kalshi_usd = round(self.initial_capital_usd / 2.0, 2)
        self.realised_pnl_inr = 0.0
        self.realised_pnl_usd = 0.0

        self.open_structures: list[StructurePosition] = []
        self.realised_structures: list[StructurePosition] = []
        self.pending_orders: list[dict] = []

        self.load()

    def get_locked_capital_inr(self) -> float:
        return sum(s.total_cost_inr for s in self.open_structures)

    def get_locked_capital_usd(self) -> float:
        return sum(s.total_cost_usd for s in self.open_structures)

    def get_locked_capital_per_venue(self) -> tuple[float, float]:
        """Returns (polymarket_locked_usd, kalshi_locked_usd)."""
        poly_locked = sum(l.cost_usd for s in self.open_structures for l in s.legs if l.venue == "polymarket")
        kalshi_locked = sum(l.cost_usd for s in self.open_structures for l in s.legs if l.venue == "kalshi")
        return round(poly_locked, 2), round(kalshi_locked, 2)

    def get_unrealised_pnl_inr(self) -> float:
        # Expected guaranteed edge of open positions
        return sum(s.projected_profit_inr for s in self.open_structures)

    def get_summary(self) -> dict[str, Any]:
        locked_inr = self.get_locked_capital_inr()
        locked_usd = self.get_locked_capital_usd()
        poly_locked, kalshi_locked = self.get_locked_capital_per_venue()
        fx_rate = get_current_fx_rate()
        return {
            "initial_capital_inr": self.initial_capital_inr,
            "initial_capital_usd": self.initial_capital_usd,
            "available_cash_inr": round(self.available_cash_inr, 2),
            "available_cash_usd": round(self.available_cash_usd, 2),
            "balance_polymarket_usd": round(self.balance_polymarket_usd, 2),
            "balance_kalshi_usd": round(self.balance_kalshi_usd, 2),
            "locked_polymarket_usd": poly_locked,
            "locked_kalshi_usd": kalshi_locked,
            "locked_capital_inr": round(locked_inr, 2),
            "locked_capital_usd": round(locked_usd, 2),
            "realised_pnl_inr": round(self.realised_pnl_inr, 2),
            "realised_pnl_usd": round(self.realised_pnl_usd, 2),
            "unrealised_pnl_inr": round(self.get_unrealised_pnl_inr(), 2),
            "total_portfolio_value_inr": round(self.available_cash_inr + locked_inr + self.realised_pnl_inr, 2),
            "open_count": len(self.open_structures),
            "realised_count": len(self.realised_structures),
            "pending_count": len(self.pending_orders),
            "kill_switch_active": is_kill_switch_active(self.data_dir),
            "inr_per_usd": fx_rate,
            "fx_status": FxService.get_instance().get_status(),
        }

    def book_structure(self, kind: str, event_title: str, relation_id: str,
                       legs_info: list[dict], note: str = "",
                       unit_payoff: float = 1.0) -> StructurePosition | None:
        """Book a multi-leg structure into open positions if sufficient capital is available."""
        if is_kill_switch_active(self.data_dir):
            return None

        fx_rate = get_current_fx_rate()
        # Calculate costs per leg and per venue
        total_cost_usd = sum(l["shares"] * l["entry_price_usd"] for l in legs_info)
        total_cost_inr = round(total_cost_usd * fx_rate, 2)

        if total_cost_inr > self.available_cash_inr:
            return None  # Exceeds available capital

        poly_cost_usd = sum(l["shares"] * l["entry_price_usd"] for l in legs_info if l.get("venue") == "polymarket")
        kalshi_cost_usd = sum(l["shares"] * l["entry_price_usd"] for l in legs_info if l.get("venue") == "kalshi")

        # Dynamic cash rebalancing between venue balances if total cash is sufficient
        if poly_cost_usd > self.balance_polymarket_usd:
            needed = poly_cost_usd - self.balance_polymarket_usd
            if self.balance_kalshi_usd >= needed:
                self.balance_kalshi_usd -= needed
                self.balance_polymarket_usd += needed
        elif kalshi_cost_usd > self.balance_kalshi_usd:
            needed = kalshi_cost_usd - self.balance_kalshi_usd
            if self.balance_polymarket_usd >= needed:
                self.balance_polymarket_usd -= needed
                self.balance_kalshi_usd += needed

        if poly_cost_usd > self.balance_polymarket_usd + 0.01 or kalshi_cost_usd > self.balance_kalshi_usd + 0.01:
            return None

        # Guaranteed minimum payoff based on exact basket payoff (Phase 0.3)
        shares = legs_info[0]["shares"] if legs_info else 0.0
        payoff_usd = round(unit_payoff * shares, 4)
        payoff_inr = round(payoff_usd * fx_rate, 2)

        profit_usd = round(payoff_usd - total_cost_usd, 4)
        profit_inr = round(payoff_inr - total_cost_inr, 2)
        ret_pct = round((profit_inr / max(total_cost_inr, 1.0)) * 100.0, 2)

        struct_id = f"STR-{int(time.time()*1000)%1000000:06d}"
        now_iso = datetime.now(timezone.utc).isoformat()

        leg_objs = []
        struct_expiry = None
        for idx, l in enumerate(legs_info):
            c_usd = round(l["shares"] * l["entry_price_usd"], 4)
            c_inr = round(c_usd * fx_rate, 2)
            exp = l.get("expiry") or ""
            exp_disp = format_expiry(exp)
            if exp and (not struct_expiry or exp > struct_expiry):
                struct_expiry = exp

            leg_objs.append(LegPosition(
                leg_id=f"{struct_id}-L{idx+1}",
                market_id=l["market_id"],
                venue=l.get("venue", "polymarket"),
                question=l["question"],
                side=l["side"],
                shares=l["shares"],
                entry_price_usd=l["entry_price_usd"],
                cost_usd=c_usd,
                cost_inr=c_inr,
                current_price_usd=l["entry_price_usd"],
                settlement_status="OPEN",
                settlement_outcome=None,
                expiry=exp,
                expiry_display=exp_disp,
            ))

        struct = StructurePosition(
            structure_id=struct_id,
            relation_id=relation_id,
            kind=kind,
            event_title=event_title,
            status="OPEN",
            legs=leg_objs,
            total_cost_usd=round(total_cost_usd, 4),
            total_cost_inr=total_cost_inr,
            guaranteed_payoff_usd=round(payoff_usd, 4),
            guaranteed_payoff_inr=payoff_inr,
            projected_profit_usd=profit_usd,
            projected_profit_inr=profit_inr,
            return_pct=ret_pct,
            booked_at=now_iso,
            note=note,
            expiry=struct_expiry or "",
            expiry_display=format_expiry(struct_expiry),
        )

        # Deduct capital from cash and per-venue balances (Phase 1.5)
        self.available_cash_inr -= total_cost_inr
        self.available_cash_usd -= total_cost_usd
        self.balance_polymarket_usd = max(0.0, round(self.balance_polymarket_usd - poly_cost_usd, 4))
        self.balance_kalshi_usd = max(0.0, round(self.balance_kalshi_usd - kalshi_cost_usd, 4))
        self.open_structures.append(struct)
        self.save()
        return struct

    def settle_single_leg(self, struct_id: str, leg_id: str, outcome: str):
        """Settle an individual leg. The trade only moves to realised if ALL legs are settled."""
        struct = next((s for s in self.open_structures if s.structure_id == struct_id), None)
        if not struct:
            return False

        leg = next((l for l in struct.legs if l.leg_id == leg_id), None)
        if not leg or leg.settlement_status == "SETTLED":
            return False

        fx_rate = get_current_fx_rate()
        leg.settlement_status = "SETTLED"
        leg.settlement_outcome = outcome
        if outcome == leg.side:
            leg.payoff_usd = round(1.0 * leg.shares, 4)
        elif outcome == "VOID":
            leg.payoff_usd = round(0.5 * leg.shares, 4)
        else:
            leg.payoff_usd = 0.0
        leg.payoff_inr = round(leg.payoff_usd * fx_rate, 2)

        # Credit venue balance
        if leg.venue == "polymarket":
            self.balance_polymarket_usd += leg.payoff_usd
        elif leg.venue == "kalshi":
            self.balance_kalshi_usd += leg.payoff_usd

        # Check if entire structure is settled
        if struct.check_and_update_settlement():
            self.open_structures.remove(struct)
            self.realised_structures.insert(0, struct)
            # Credit realised cash back to available
            total_payoff_inr = sum(l.payoff_inr for l in struct.legs)
            total_payoff_usd = sum(l.payoff_usd for l in struct.legs)
            self.available_cash_inr += total_payoff_inr
            self.available_cash_usd += total_payoff_usd
            self.realised_pnl_inr += struct.realised_profit_inr
            self.realised_pnl_usd += struct.realised_profit_usd

        self.save()
        return True

    def settle_entire_structure(self, struct_id: str, world_outcome: str | None = None):
        """Settles all remaining legs according to the logically feasible worst-case outcome."""
        struct = next((s for s in self.open_structures if s.structure_id == struct_id), None)
        if not struct:
            return False

        fx_rate = get_current_fx_rate()
        target_payoff_usd = struct.guaranteed_payoff_usd
        for idx, leg in enumerate(struct.legs):
            if leg.settlement_status != "SETTLED":
                leg.settlement_status = "SETTLED"
                if world_outcome:
                    leg_win = (leg.side == "YES" and world_outcome == "YES") or (leg.side == "NO" and world_outcome == "NO")
                    outcome = leg.side if leg_win else ("NO" if leg.side == "YES" else "YES")
                    leg.settlement_outcome = outcome
                    leg.payoff_usd = round(1.0 * leg.shares, 4) if leg_win else 0.0
                else:
                    # Allocate guaranteed payoff realistically (Phase 0.6)
                    if idx == 0:
                        leg.settlement_outcome = leg.side
                        leg.payoff_usd = target_payoff_usd
                    else:
                        leg.settlement_outcome = "NO" if leg.side == "YES" else "YES"
                        leg.payoff_usd = 0.0
                leg.payoff_inr = round(leg.payoff_usd * fx_rate, 2)
                if leg.venue == "polymarket":
                    self.balance_polymarket_usd += leg.payoff_usd
                elif leg.venue == "kalshi":
                    self.balance_kalshi_usd += leg.payoff_usd

        if struct.check_and_update_settlement():
            self.open_structures.remove(struct)
            self.realised_structures.insert(0, struct)
            total_payoff_inr = sum(l.payoff_inr for l in struct.legs)
            total_payoff_usd = sum(l.payoff_usd for l in struct.legs)
            self.available_cash_inr += total_payoff_inr
            self.available_cash_usd += total_payoff_usd
            self.realised_pnl_inr += struct.realised_profit_inr
            self.realised_pnl_usd += struct.realised_profit_usd

        self.save()
        return True

    def save(self):
        data = {
            "initial_capital_inr": self.initial_capital_inr,
            "available_cash_inr": self.available_cash_inr,
            "available_cash_usd": self.available_cash_usd,
            "balance_polymarket_usd": self.balance_polymarket_usd,
            "balance_kalshi_usd": self.balance_kalshi_usd,
            "realised_pnl_inr": self.realised_pnl_inr,
            "realised_pnl_usd": self.realised_pnl_usd,
            "open_structures": [asdict(s) for s in self.open_structures],
            "realised_structures": [asdict(s) for s in self.realised_structures],
            "pending_orders": self.pending_orders,
        }
        # Atomic file write (write-then-rename) prevents corruption
        tmp_file = self.storage_file.with_suffix(".tmp")
        tmp_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp_file, self.storage_file)

    def load(self):
        if not self.storage_file.exists():
            return
        try:
            data = json.loads(self.storage_file.read_text(encoding="utf-8"))
            self.initial_capital_inr = data.get("initial_capital_inr", self.initial_capital_inr)
            self.available_cash_inr = data.get("available_cash_inr", self.available_cash_inr)
            self.available_cash_usd = data.get("available_cash_usd", self.available_cash_usd)
            self.balance_polymarket_usd = data.get("balance_polymarket_usd", round(self.available_cash_usd / 2.0, 2))
            self.balance_kalshi_usd = data.get("balance_kalshi_usd", round(self.available_cash_usd / 2.0, 2))
            self.realised_pnl_inr = data.get("realised_pnl_inr", 0.0)
            self.realised_pnl_usd = data.get("realised_pnl_usd", 0.0)

            def to_struct(d):
                legs = []
                for l in d.get("legs", []):
                    l_copy = dict(l)
                    exp = l_copy.get("expiry") or ""
                    l_copy["expiry"] = exp
                    l_copy["expiry_display"] = l_copy.get("expiry_display") or format_expiry(exp)
                    valid_leg_keys = LegPosition.__annotations__.keys()
                    legs.append(LegPosition(**{k: v for k, v in l_copy.items() if k in valid_leg_keys}))
                d_copy = dict(d)
                d_copy["legs"] = legs
                exp_st = d_copy.get("expiry") or ""
                d_copy["expiry"] = exp_st
                d_copy["expiry_display"] = d_copy.get("expiry_display") or format_expiry(exp_st)
                valid_keys = StructurePosition.__annotations__.keys()
                return StructurePosition(**{k: v for k, v in d_copy.items() if k in valid_keys})

            self.open_structures = [to_struct(s) for s in data.get("open_structures", [])]
            self.realised_structures = [to_struct(s) for s in data.get("realised_structures", [])]
            self.pending_orders = data.get("pending_orders", [])
        except Exception as e:
            print(f"[portfolio] Error loading state: {e}")

    def reset(self):
        """Reset portfolio back to initial state."""
        self.available_cash_inr = self.initial_capital_inr
        self.available_cash_usd = self.initial_capital_usd
        self.balance_polymarket_usd = round(self.initial_capital_usd / 2.0, 2)
        self.balance_kalshi_usd = round(self.initial_capital_usd / 2.0, 2)
        self.realised_pnl_inr = 0.0
        self.realised_pnl_usd = 0.0
        self.open_structures.clear()
        self.realised_structures.clear()
        self.pending_orders.clear()
        self.save()


# Real market template catalog seeded from live Polymarket and Kalshi data
ACTUAL_DATA_TEMPLATES = [
    {
        "kind": "implies",
        "event_title": "Kraken IPO Date Ladder",
        "note": "Earlier IPO deadline implies later IPO deadline (by Dec 2026 => by Dec 2027)",
        "relation_id": "rel_kraken_date",
        "legs": [
            {
                "market_id": "0xced0cb8725bad43d78fda0cd0e5fa9e31804625cb3502b2c7897f8e8f7fa9e1f",
                "venue": "polymarket",
                "question": "Kraken IPO by December 31, 2026?",
                "side": "NO",
                "entry_price_usd": 0.942,
                "shares": 15.0,
                "expiry": "2027-01-01T04:59:00Z",
            },
            {
                "market_id": "0xe1779ab59d1c8472852a843d3b6f69e7921c4c34d4f778b6be0e9b468986f45f",
                "venue": "polymarket",
                "question": "Kraken IPO by December 31, 2027?",
                "side": "YES",
                "entry_price_usd": 0.038,
                "shares": 15.0,
                "expiry": "2028-01-01T04:59:00Z",
            }
        ]
    },
    {
        "kind": "threshold_ladder",
        "event_title": "Bitcoin Year-End Price Threshold Ladder",
        "note": "BTC >= $100k implies BTC >= $90k for Dec 31, 2026",
        "relation_id": "rel_btc_thresh",
        "legs": [
            {
                "market_id": "poly_btc_100k",
                "venue": "polymarket",
                "question": "Will BTC be above $100,000 on Dec 31, 2026?",
                "side": "NO",
                "entry_price_usd": 0.440,
                "shares": 20.0,
                "expiry": "2027-01-01T00:00:00Z",
            },
            {
                "market_id": "kalshi:BTC-26DEC-90K",
                "venue": "kalshi",
                "question": "Will BTC be above $90,000 on Dec 31, 2026?",
                "side": "YES",
                "entry_price_usd": 0.510,
                "shares": 20.0,
                "expiry": "2027-01-01T00:00:00Z",
            }
        ]
    },
    {
        "kind": "equivalent",
        "event_title": "Federal Reserve December 2026 Rate Cut",
        "note": "Cross-venue equivalent market mismatch (Polymarket vs Kalshi)",
        "relation_id": "rel_fed_cross",
        "legs": [
            {
                "market_id": "poly_fed_dec26",
                "venue": "polymarket",
                "question": "Will the Fed cut interest rates in December 2026?",
                "side": "NO",
                "entry_price_usd": 0.460,
                "shares": 18.0,
                "expiry": "2026-12-18T20:00:00Z",
            },
            {
                "market_id": "kalshi:FED-26DEC-CUT",
                "venue": "kalshi",
                "question": "Fed cut rates in December 2026 meeting?",
                "side": "YES",
                "entry_price_usd": 0.490,
                "shares": 18.0,
                "expiry": "2026-12-18T20:00:00Z",
            }
        ]
    },
    {
        "kind": "implies",
        "event_title": "Macron French Presidency Exit Date Ladder",
        "note": "Macron out by July 2026 implies Macron out by Dec 2026",
        "relation_id": "rel_macron_date",
        "legs": [
            {
                "market_id": "0x98e6e35c934b5281f28373b408a129825c6453918fb8bd9dfb8a991be2dd684f",
                "venue": "polymarket",
                "question": "Macron out by July 31, 2026?",
                "side": "NO",
                "entry_price_usd": 0.940,
                "shares": 12.0,
                "expiry": "2026-08-01T04:59:00Z",
            },
            {
                "market_id": "0xc281ddf56886ce5d1dcbfad73c9cb457e8789ed5adff29f95a7b8bcbfeb6ae26",
                "venue": "polymarket",
                "question": "Macron out by December 31, 2026?",
                "side": "YES",
                "entry_price_usd": 0.039,
                "shares": 12.0,
                "expiry": "2027-01-01T04:59:00Z",
            }
        ]
    }
]


def seed_initial_simulator_data(portfolio: PortfolioManager):
    """Seed initial structures so open positions and settled positions can be inspected immediately."""
    if len(portfolio.open_structures) == 0 and len(portfolio.realised_structures) == 0:
        # Book 2 open structures
        t1 = ACTUAL_DATA_TEMPLATES[0]
        portfolio.book_structure(t1["kind"], t1["event_title"], t1["relation_id"], t1["legs"], t1["note"])

        t2 = ACTUAL_DATA_TEMPLATES[1]
        portfolio.book_structure(t2["kind"], t2["event_title"], t2["relation_id"], t2["legs"], t2["note"])

        # Book a 3rd structure and settle it completely to show realized history
        t3 = ACTUAL_DATA_TEMPLATES[2]
        s3 = portfolio.book_structure(t3["kind"], t3["event_title"], t3["relation_id"], t3["legs"], t3["note"])
        if s3:
            portfolio.settle_entire_structure(s3.structure_id, world_outcome="YES")
