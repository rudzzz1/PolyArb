"""Opportunity Ranking and Capital Allocation Engine (Phase 3.1).
Replaces first-come-first-served execution with a multi-factor scoring pipeline:
1. Scores candidates by Risk-Adjusted Edge * Annualized Yield Factor.
2. Sorts candidates descending by score.
3. Allocates capital greedily to the highest-scoring opportunities subject to
   per-relation, per-event, and per-venue capital constraints.
"""
from __future__ import annotations
import math
from dataclasses import dataclass, field
from typing import Any

from .core import Basket, Level
from .config import Config, RiskLimits
from .logger import get_logger

logger = get_logger("allocator")


@dataclass
class ScoredOpportunity:
    candidate_id: str
    kind: str
    event_title: str
    basket: Basket
    ms: list[dict[str, Any]]
    books: list[dict[str, list[Level]]]
    raw_edge: float
    annualized_return: float | None
    days_to_resolution: float | None
    risk_flags: list[str] = field(default_factory=list)
    risk_factor: float = 1.0
    score: float = 0.0


@dataclass
class AllocatedOpportunity:
    scored_opp: ScoredOpportunity
    allocated_usd: float
    allocated_shares: float
    projected_profit_usd: float


class OpportunityScorer:
    """Calculates risk-adjusted opportunity scores considering edge, capital velocity (APY), and risk flags."""
    @staticmethod
    def calculate_score(edge: float, cost: float, apy: float | None,
                        days_to_res: float | None, risk_flags: list[str] | None = None) -> tuple[float, float]:
        """Returns (score, risk_factor)."""
        flags = risk_flags or []
        risk_factor = 1.0

        # Penalize risk flags
        for f in flags:
            f_upper = f.upper()
            if "DIFFERENT_RESOLUTION" in f_upper:
                risk_factor *= 0.70
            elif "POLARITY" in f_upper:
                risk_factor *= 0.75
            elif "TIME_WINDOW" in f_upper:
                risk_factor *= 0.85
            elif "CROSS_VENUE" in f_upper:
                risk_factor *= 0.95

        # Long horizon penalty (capital locked too long)
        if days_to_res and days_to_res > 180:
            horizon_penalty = max(0.50, 1.0 - (days_to_res - 180) / 365.0)
            risk_factor *= horizon_penalty

        # APY velocity factor: rewarding fast turnover (logarithmic scale)
        if apy and apy > 0:
            velocity_multiplier = 1.0 + min(4.0, math.log1p(apy))
        else:
            velocity_multiplier = 1.0

        score = edge * velocity_multiplier * risk_factor
        return round(score, 6), round(risk_factor, 4)


class CapitalAllocator:
    """Allocates available portfolio capital to top-ranked arbitrage opportunities."""
    def __init__(self, cfg: Config | None = None):
        self.cfg = cfg or Config()

    def rank_and_allocate(self, candidates: list[ScoredOpportunity],
                          available_cash_usd: float,
                          current_event_exposures: dict[str, float] | None = None,
                          current_venue_locked: tuple[float, float] = (0.0, 0.0)) -> list[AllocatedOpportunity]:
        """Ranks scored opportunities and allocates capital greedily within risk limits."""
        if not candidates or available_cash_usd <= 1.0:
            return []

        # Sort descending by score
        ranked = sorted(candidates, key=lambda opp: opp.score, reverse=True)
        allocations: list[AllocatedOpportunity] = []

        rem_cash = available_cash_usd
        event_exp = dict(current_event_exposures or {})
        poly_locked, kalshi_locked = current_venue_locked

        risk = self.cfg.risk

        for opp in ranked:
            if rem_cash <= 2.0:
                break

            # 1. Check Event Exposure Cap
            event_slug = opp.event_title or "default_event"
            cur_event = event_exp.get(event_slug, 0.0)
            avail_event_cap = max(0.0, risk.max_capital_per_event - cur_event)
            if avail_event_cap <= 1.0:
                continue

            # 2. Check Per-Relation Cap
            max_for_relation = min(risk.max_capital_per_relation, avail_event_cap, rem_cash)
            if max_for_relation <= 1.0:
                continue

            # 3. Unit cost check
            unit_cost = opp.basket.cost
            if unit_cost <= 0.01:
                continue

            # Preliminary target shares
            target_shares = max_for_relation / unit_cost
            if target_shares < risk.min_shares:
                continue

            # 4. Check Venue Capital Caps
            poly_spend = 0.0
            kalshi_spend = 0.0
            for i, s in opp.basket.legs:
                if i < len(opp.ms):
                    v = opp.ms[i].get("venue", "polymarket")
                    # Use book top price if available, else split basket unit cost equally across legs
                    leg_p = (
                        opp.books[i][s][0].price
                        if (i < len(opp.books) and isinstance(opp.books[i], dict) and opp.books[i].get(s) and len(opp.books[i][s]) > 0)
                        else (unit_cost / max(len(opp.basket.legs), 1))
                    )
                    leg_spend = target_shares * leg_p
                    if v == "kalshi":
                        kalshi_spend += leg_spend
                    else:
                        poly_spend += leg_spend

            if poly_locked + poly_spend > risk.max_capital_per_venue:
                continue
            if kalshi_locked + kalshi_spend > risk.max_capital_per_venue:
                continue

            # Valid allocation!
            allocated_usd = round(target_shares * unit_cost, 2)
            projected_profit = round(target_shares * opp.basket.edge, 2)

            alloc = AllocatedOpportunity(
                scored_opp=opp,
                allocated_usd=allocated_usd,
                allocated_shares=round(target_shares, 2),
                projected_profit_usd=projected_profit,
            )
            allocations.append(alloc)

            # Update remaining limits
            rem_cash -= allocated_usd
            event_exp[event_slug] = cur_event + allocated_usd
            poly_locked += poly_spend
            kalshi_locked += kalshi_spend

            logger.info(f"[ALLOCATION] #{len(allocations)} {opp.candidate_id} ({opp.event_title}): "
                        f"${allocated_usd:.2f} USD allocated ({target_shares:.1f} sh, edge: ${opp.basket.edge:.3f}, score: {opp.score:.4f})")

        return allocations
