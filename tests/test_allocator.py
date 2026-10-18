"""Unit tests for Opportunity Ranking and Capital Allocator (Phase 3.1)."""
from __future__ import annotations
from polyarb.allocator import OpportunityScorer, CapitalAllocator, ScoredOpportunity, AllocatedOpportunity
from polyarb.core import Basket
from polyarb.config import Config, RiskLimits


def test_opportunity_scorer():
    # 1. Base score without flags
    score_clean, rf_clean = OpportunityScorer.calculate_score(
        edge=0.05, cost=0.95, apy=15.0, days_to_res=10.0, risk_flags=[]
    )
    assert rf_clean == 1.0
    assert score_clean > 0.05  # boosted by APY velocity

    # 2. Risk flags penalize score
    score_risky, rf_risky = OpportunityScorer.calculate_score(
        edge=0.05, cost=0.95, apy=15.0, days_to_res=10.0,
        risk_flags=["RISK_DIFFERENT_RESOLUTION_SOURCES", "RISK_POLARITY_MISMATCH"]
    )
    assert rf_risky < 1.0
    assert score_risky < score_clean

    # 3. Long horizon penalty
    score_long, rf_long = OpportunityScorer.calculate_score(
        edge=0.05, cost=0.95, apy=15.0, days_to_res=365.0, risk_flags=[]
    )
    assert rf_long < 1.0
    assert score_long < score_clean


def test_capital_allocator_ranking_and_caps():
    cfg = Config(
        risk=RiskLimits(
            max_capital_per_relation=100.0,
            max_capital_per_event=150.0,
            max_capital_per_venue=300.0,
            min_shares=2.0,
        )
    )
    allocator = CapitalAllocator(cfg=cfg)

    # Basket helper
    def make_opp(cand_id: str, edge: float, score: float, event: str, venue: str = "polymarket") -> ScoredOpportunity:
        bk = Basket(legs=((0, "NO"), (1, "YES")), cost=round(1.00 - edge, 4), payoff=1.00)
        m0 = {"id": f"{cand_id}_m0", "venue": venue, "event_title": event}
        m1 = {"id": f"{cand_id}_m1", "venue": venue, "event_title": event}
        return ScoredOpportunity(
            candidate_id=cand_id,
            kind="implies",
            event_title=event,
            basket=bk,
            ms=[m0, m1],
            books=[{}, {}],
            raw_edge=edge,
            annualized_return=20.0,
            days_to_resolution=5.0,
            score=score,
        )

    opp1 = make_opp("opp_low", edge=0.03, score=0.04, event="Event_A")
    opp2 = make_opp("opp_high", edge=0.08, score=0.15, event="Event_A")
    opp3 = make_opp("opp_med", edge=0.05, score=0.09, event="Event_B")

    candidates = [opp1, opp2, opp3]

    # Rank and allocate with $200 available cash
    allocs = allocator.rank_and_allocate(candidates, available_cash_usd=200.0)

    # opp_high should be ranked #1
    assert len(allocs) >= 2
    assert allocs[0].scored_opp.candidate_id == "opp_high"
    # Relation cap is $100
    assert allocs[0].allocated_usd <= 100.0

    # opp_med should be ranked #2
    assert allocs[1].scored_opp.candidate_id == "opp_med"

    # Event cap check:
    # Event_A cap is 150. opp_high took ~100. opp_low (Event_A) cannot exceed remainder (50)
    event_a_total = sum(a.allocated_usd for a in allocs if a.scored_opp.event_title == "Event_A")
    assert event_a_total <= 150.0


def test_capital_allocator_venue_limits():
    cfg = Config(
        risk=RiskLimits(
            max_capital_per_relation=200.0,
            max_capital_per_event=500.0,
            max_capital_per_venue=150.0,
            min_shares=2.0,
        )
    )
    allocator = CapitalAllocator(cfg=cfg)

    bk = Basket(legs=((0, "NO"), (1, "YES")), cost=0.90, payoff=1.00)
    opp_poly = ScoredOpportunity(
        candidate_id="poly_opp",
        kind="implies",
        event_title="Poly_Event",
        basket=bk,
        ms=[{"id": "m1", "venue": "polymarket"}, {"id": "m2", "venue": "polymarket"}],
        books=[{}, {}],
        raw_edge=0.10,
        annualized_return=25.0,
        days_to_resolution=5.0,
        score=0.20,
    )

    # polymarket already has $140 locked (cap is $150). Available venue buffer = $10
    # $10 / 0.90 = ~11.1 shares
    allocs = allocator.rank_and_allocate(
        [opp_poly],
        available_cash_usd=500.0,
        current_venue_locked=(140.0, 0.0)
    )
    # Target shares of 200 / 0.90 = 222 sh -> spend $200 -> 140 + 200 = 340 > 150 cap! Should skip or fit
    assert len(allocs) == 0  # Exceeds venue cap


if __name__ == "__main__":
    test_opportunity_scorer()
    print("ok test_opportunity_scorer")
    test_capital_allocator_ranking_and_caps()
    print("ok test_capital_allocator_ranking_and_caps")
    test_capital_allocator_venue_limits()
    print("ok test_capital_allocator_venue_limits")
