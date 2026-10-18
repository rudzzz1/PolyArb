"""Unit tests for LP Sizing and Mathematical Optimizer (Phase 3.2)."""
from __future__ import annotations
from polyarb.optimizer import MathematicalOptimizer, OptimizationConstraint
from polyarb.core import Level


def test_optimizer_multi_tier_cost_calculation():
    # Leg 0: 10 @ 0.40, 20 @ 0.42, 50 @ 0.45
    book0 = [Level(0.40, 10.0), Level(0.42, 20.0), Level(0.45, 50.0)]
    # Leg 1: 15 @ 0.45, 25 @ 0.48, 40 @ 0.50
    book1 = [Level(0.45, 15.0), Level(0.48, 25.0), Level(0.50, 40.0)]

    books = [book0, book1]

    # 1. 5 shares: fits within top level of both legs
    spend_5, marginal_5 = MathematicalOptimizer.calculate_cost_at_shares(books, 5.0)
    assert abs(spend_5 - 4.25) < 1e-4
    assert abs(marginal_5 - 0.85) < 1e-4

    # 2. 12 shares: Leg 0 sweeps into level 2 (10 @ 0.40 + 2 @ 0.42 = 4.84)
    # Leg 1 stays in level 1 (12 @ 0.45 = 5.40)
    spend_12, marginal_12 = MathematicalOptimizer.calculate_cost_at_shares(books, 12.0)
    assert abs(spend_12 - (4.84 + 5.40)) < 1e-4
    assert abs(marginal_12 - (0.42 + 0.45)) < 1e-4

    # 3. Beyond available depth (Leg 1 total depth = 15 + 25 + 40 = 80)
    spend_inf, _ = MathematicalOptimizer.calculate_cost_at_shares(books, 100.0)
    assert spend_inf == float("inf")


def test_optimizer_sizing_constraints():
    book0 = [Level(0.40, 10.0), Level(0.45, 10.0), Level(0.55, 10.0)]
    book1 = [Level(0.45, 10.0), Level(0.48, 10.0), Level(0.55, 10.0)]
    books = [book0, book1]
    payoff = 1.00  # Base cost = 0.85, base edge = 0.15

    # 1. Capital binding constraint
    res_cap = MathematicalOptimizer.optimize_sizing(
        books, payoff,
        constraints=OptimizationConstraint(max_capital=10.0, max_slippage_bps=2000.0)
    )
    assert res_cap.total_cost <= 10.0
    assert res_cap.net_profit > 0
    assert res_cap.binding_constraint == "CAPITAL"

    # 2. Slippage binding constraint
    # Top marginal = 0.85. Next marginal = 0.45 + 0.48 = 0.93 -> slippage = (0.93 - 0.85)/0.85 * 10000 = 941 bps
    res_slip = MathematicalOptimizer.optimize_sizing(
        books, payoff,
        constraints=OptimizationConstraint(max_capital=100.0, max_slippage_bps=50.0)
    )
    # Should stop around 10 shares (top of book) before slipping into level 2
    assert res_slip.shares <= 10.1
    assert res_slip.effective_slippage_bps <= 50.0

    # 3. Negative edge margin constraint
    res_no_edge = MathematicalOptimizer.optimize_sizing(
        books, payoff=0.80,  # Payoff 0.80 < base cost 0.85
        constraints=OptimizationConstraint(min_edge_per_share=0.01)
    )
    assert res_no_edge.shares == 0.0
    assert res_no_edge.binding_constraint == "EDGE_MARGIN"


def test_optimizer_net_common_legs():
    positions = [
        {
            "legs": [
                {"market_id": "mkt_btc", "side": "YES", "shares": 10.0},
                {"market_id": "mkt_eth", "side": "NO", "shares": 10.0},
            ]
        },
        {
            "legs": [
                {"market_id": "mkt_btc", "side": "NO", "shares": 4.0},
                {"market_id": "mkt_sol", "side": "YES", "shares": 6.0},
            ]
        }
    ]

    net = MathematicalOptimizer.net_common_legs(positions)
    # mkt_btc: +10 - 4 = +6.0
    assert net["mkt_btc"] == 6.0
    # mkt_eth: -10.0
    assert net["mkt_eth"] == -10.0
    # mkt_sol: +6.0
    assert net["mkt_sol"] == 6.0


if __name__ == "__main__":
    test_optimizer_multi_tier_cost_calculation()
    print("ok test_optimizer_multi_tier_cost_calculation")
    test_optimizer_sizing_constraints()
    print("ok test_optimizer_sizing_constraints")
    test_optimizer_net_common_legs()
    print("ok test_optimizer_net_common_legs")
