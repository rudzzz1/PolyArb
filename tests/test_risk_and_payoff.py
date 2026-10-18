"""Comprehensive property-based and unit tests for risk controls, payoffs, void worlds,
orderbook validation, and depth sizing."""
from __future__ import annotations
import random
from polyarb.core import (
    best_basket, worlds, Level, validate_book, annualized_return,
    sweep, sweep_with_limits, unwind_loss_estimate, legging_risk_adjusted_edge
)


def A(y, n):
    return {"YES": y, "NO": n}


def test_independent_brute_force_solver():
    """Property test: compares best_basket against an independent reference payoff solver
    across 50 randomized market price scenarios."""
    random.seed(42)
    kinds = ["implies", "exclusive", "partition", "equivalent"]

    for kind in kinds:
        n = 2 if kind == "implies" else random.choice([2, 3, 4])
        ws = worlds(kind, n)

        for _ in range(25):
            # Generate random realistic asks in [0.05, 0.95]
            asks = []
            for _ in range(n):
                py = round(random.uniform(0.10, 0.90), 2)
                pn = round(random.uniform(0.10, 0.90), 2)
                asks.append(A(py, pn))

            res = best_basket(kind, asks)

            # Independent reference payoff check
            ref_best_edge = -999.0
            ref_best_legs = None

            # Enumerate all 3^n leg combinations independently
            from itertools import product
            for choice in product([None, "YES", "NO"], repeat=n):
                legs = [(i, s) for i, s in enumerate(choice) if s]
                if not legs:
                    continue
                cost = sum(asks[i][s] for i, s in legs)
                # calculate min payoff across all worlds
                payoff = min(sum(1 for i, s in legs if w[i] == (s == "YES")) for w in ws)
                edge = payoff - cost
                if edge > ref_best_edge:
                    ref_best_edge = edge
                    ref_best_legs = tuple(legs)

            if res is None:
                assert ref_best_edge <= 0.0 or not ref_best_legs
            else:
                assert abs(res.edge - ref_best_edge) < 1e-6
                assert set(res.legs) == set(ref_best_legs)


def test_void_worlds_modeling():
    """Test void/refund resolution modeling where a cancelled market pays 0.50."""
    ws_standard = worlds("implies", 2, include_void=False)
    assert len(ws_standard) == 3

    ws_void = worlds("implies", 2, include_void=True)
    # Extra worlds with None
    assert any(None in w for w in ws_void)

    # In implies: A => B. Buy NO(A) at 0.40, YES(B) at 0.40. Cost = 0.80.
    # In standard worlds, payoff is always >= 1.0.
    # In a void world where market A is voided (pays 0.50) and B resolves NO (pays 0.0), payoff is 0.50.
    # So guaranteed payoff drops from 1.0 to 0.50!
    b_no_void = best_basket("implies", [A(0.60, 0.40), A(0.40, 0.60)], include_void=False)
    assert b_no_void is not None
    assert b_no_void.payoff == 1.0
    assert abs(b_no_void.edge - 0.20) < 1e-6

    b_with_void = best_basket("implies", [A(0.60, 0.40), A(0.40, 0.60)], include_void=True, void_payoff=0.50)
    assert b_with_void is not None
    assert b_with_void.payoff == 0.50  # worst-case includes void scenario
    assert b_with_void.edge < 0.0      # edge is negative when considering void risk!


def test_annualized_return():
    # 2 cent edge on 95 cent cost
    cost = 0.95
    edge = 0.02
    # 2 days to resolution -> massive APY
    apy_2d = annualized_return(edge, cost, 2.0)
    assert apy_2d > 3.0  # > 300% APY

    # 180 days to resolution -> modest APY
    apy_180d = annualized_return(edge, cost, 180.0)
    assert 0.03 < apy_180d < 0.06  # ~4.2% APY

    # Zero cost or edge <= 0
    assert annualized_return(0.0, cost, 10.0) == 0.0
    assert annualized_return(-0.01, cost, 10.0) == 0.0


def test_book_validation():
    # Valid book
    valid_book = {
        "YES": [Level(0.45, 10.0), Level(0.50, 20.0)],
        "NO": [Level(0.52, 10.0), Level(0.58, 20.0)],
    }
    ok, reason = validate_book(valid_book)
    assert ok and reason == "ok"

    # Price out of bounds
    invalid_price = {"YES": [Level(1.05, 10.0)], "NO": [Level(0.50, 10.0)]}
    ok, _ = validate_book(invalid_price)
    assert not ok

    # Negative price
    neg_price = {"YES": [Level(-0.05, 10.0)], "NO": [Level(0.50, 10.0)]}
    ok, _ = validate_book(neg_price)
    assert not ok

    # Zero size
    zero_size = {"YES": [Level(0.45, 0.0)], "NO": [Level(0.50, 10.0)]}
    ok, _ = validate_book(zero_size)
    assert not ok

    # Unsorted asks
    unsorted = {"YES": [Level(0.50, 10.0), Level(0.45, 10.0)], "NO": [Level(0.50, 10.0)]}
    ok, _ = validate_book(unsorted)
    assert not ok


def test_sweep_with_limits():
    l1 = [Level(0.45, 10.0), Level(0.50, 20.0)]
    l2 = [Level(0.50, 5.0), Level(0.52, 15.0)]

    # 1. Unconstrained: 5 shares at (0.45 + 0.50 = 0.95), then 5 shares at (0.45 + 0.52 = 0.97) -> 10 shares
    res = sweep_with_limits([l1, l2], 1.0, max_capital=100.0, max_slippage_bps=1000.0)
    assert res.shares == 10.0
    assert abs(res.total_cost - (5 * 0.95 + 5 * 0.97)) < 1e-4

    # 2. Capital capped at $5.00 (5 shares @ 0.95 = 4.75, remaining $0.25 buys 0.25/0.97 shares)
    res_cap = sweep_with_limits([l1, l2], 1.0, max_capital=5.0, max_slippage_bps=1000.0)
    assert abs(res_cap.total_cost - 5.0) < 1e-3
    assert abs(res_cap.shares - (5.0 + 0.25 / 0.97)) < 1e-3

    # 3. Slippage capped: reject levels beyond 20 bps slippage
    res_slip = sweep_with_limits([l1, l2], 1.0, max_slippage_bps=20.0)
    assert res_slip.shares == 5.0  # only top of book taken, second level exceeds slippage

    # 4. Thinnest leg depth requirement
    res_depth = sweep_with_limits([l1, l2], 1.0, min_depth=50.0)
    assert res_depth.shares == 0.0  # available depth (20) < 50


def test_legging_and_unwind():
    # If ask is 0.50 and bid is 0.46, unwind loss is 0.04
    assert abs(unwind_loss_estimate(0.50, 0.46) - 0.04) < 1e-6

    # If bid is missing, fallback to default spread
    assert unwind_loss_estimate(0.50, None, default_spread=0.05) == 0.05

    # Risk adjusted edge: raw edge is 0.03, fill prob is 0.90, unwind loss is 0.04
    # Expected edge = 0.90 * 0.03 - 0.10 * 0.04 = 0.027 - 0.004 = 0.023
    adj = legging_risk_adjusted_edge(0.03, [0.04], fill_prob=0.90)
    assert abs(adj - 0.023) < 1e-6


if __name__ == "__main__":
    test_independent_brute_force_solver()
    print("ok test_independent_brute_force_solver")
    test_void_worlds_modeling()
    print("ok test_void_worlds_modeling")
    test_annualized_return()
    print("ok test_annualized_return")
    test_book_validation()
    print("ok test_book_validation")
    test_sweep_with_limits()
    print("ok test_sweep_with_limits")
    test_legging_and_unwind()
    print("ok test_legging_and_unwind")
