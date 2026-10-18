"""Unit tests for Dynamic Gas and Non-Trading Cost Model (Phase 4.4)."""
from __future__ import annotations
from polyarb.costs import CostModelService, get_non_trading_cost
from polyarb.allocator import OpportunityScorer


def test_cost_model_service():
    svc = CostModelService(ttl_sec=300.0)
    # Gas in gwei
    gas = svc.update_gas_price()
    assert gas > 0.0

    # Non-trading cost on Polymarket:
    cost_poly = svc.calculate_non_trading_cost("polymarket", leg_count=2)
    assert cost_poly >= 0.01

    # Non-trading cost on Kalshi (pure ACH/banking):
    cost_kalshi = svc.calculate_non_trading_cost("kalshi", leg_count=2)
    assert cost_kalshi == 0.01

    # Non-trading cost on Manifold:
    cost_mf = svc.calculate_non_trading_cost("manifold", leg_count=2)
    assert cost_mf == 0.005


def test_opportunity_scorer_with_non_trading_cost():
    # 1. Without non-trading cost
    score_no_cost, _ = OpportunityScorer.calculate_score(
        edge=0.05, cost=0.95, apy=10.0, days_to_res=10.0, non_trading_cost=0.0
    )

    # 2. With non-trading cost of $0.02
    score_with_cost, _ = OpportunityScorer.calculate_score(
        edge=0.05, cost=0.95, apy=10.0, days_to_res=10.0, non_trading_cost=0.02
    )

    assert score_with_cost < score_no_cost

    # 3. If cost exceeds edge, score drops to 0
    score_negative_edge, _ = OpportunityScorer.calculate_score(
        edge=0.01, cost=0.99, apy=10.0, days_to_res=10.0, non_trading_cost=0.02
    )
    assert score_negative_edge == 0.0


def test_global_helper():
    cost = get_non_trading_cost("polymarket", 2)
    assert cost >= 0.01


if __name__ == "__main__":
    test_cost_model_service()
    print("ok test_cost_model_service")
    test_opportunity_scorer_with_non_trading_cost()
    print("ok test_opportunity_scorer_with_non_trading_cost")
    test_global_helper()
    print("ok test_global_helper")
