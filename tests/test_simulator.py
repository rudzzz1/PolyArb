"""Tests for simulation engine, 10k INR capital management, and strict all-legs-settled rule."""
from __future__ import annotations
import tempfile
from pathlib import Path
from polyarb.simulator import PortfolioManager, ACTUAL_DATA_TEMPLATES, INR_PER_USD


def test_portfolio_inr_capital_and_booking():
    with tempfile.TemporaryDirectory() as tmp:
        pm = PortfolioManager(initial_capital_inr=10000.0, data_dir=tmp)
        assert pm.available_cash_inr == 10000.0
        assert pm.realised_pnl_inr == 0.0
        assert len(pm.open_structures) == 0

        # Book a structure
        t = ACTUAL_DATA_TEMPLATES[0]
        st = pm.book_structure(t["kind"], t["event_title"], t["relation_id"], t["legs"], t["note"])
        assert st is not None
        assert st.status == "OPEN"
        assert len(pm.open_structures) == 1
        assert pm.get_locked_capital_inr() > 0
        assert pm.available_cash_inr < 10000.0
        assert abs((pm.available_cash_inr + pm.get_locked_capital_inr()) - 10000.0) < 1e-2


def test_strict_all_legs_settled_rule():
    with tempfile.TemporaryDirectory() as tmp:
        pm = PortfolioManager(initial_capital_inr=10000.0, data_dir=tmp)
        t = ACTUAL_DATA_TEMPLATES[0]  # has 2 legs
        st = pm.book_structure(t["kind"], t["event_title"], t["relation_id"], t["legs"], t["note"])
        assert st is not None
        struct_id = st.structure_id
        assert len(st.legs) == 2

        # 1. Settle Leg 1 ONLY
        pm.settle_single_leg(struct_id, st.legs[0].leg_id, outcome="YES")

        # CRITICAL VERIFICATION:
        # Trade MUST STILL BE in open_structures, NOT in realised_structures!
        assert len(pm.open_structures) == 1
        assert len(pm.realised_structures) == 0
        assert pm.open_structures[0].status == "OPEN"
        assert pm.open_structures[0].legs[0].settlement_status == "SETTLED"
        assert pm.open_structures[0].legs[1].settlement_status == "OPEN"

        # 2. Settle Leg 2 (Now ALL legs are settled)
        pm.settle_single_leg(struct_id, st.legs[1].leg_id, outcome="YES")

        # CRITICAL VERIFICATION:
        # NOW and ONLY NOW does the trade move to realised_structures!
        assert len(pm.open_structures) == 0
        assert len(pm.realised_structures) == 1
        realised = pm.realised_structures[0]
        assert realised.status == "REALISED"
        assert realised.settled_at is not None
        # Realised profit is recorded and capital returned to available cash
        assert pm.realised_pnl_inr > 0
        assert pm.available_cash_inr > 10000.0


def test_capital_limit_enforcement():
    with tempfile.TemporaryDirectory() as tmp:
        # Low capital of 100 INR
        pm = PortfolioManager(initial_capital_inr=100.0, data_dir=tmp)
        t = ACTUAL_DATA_TEMPLATES[0]  # Cost is ~15 shares * $0.98 * 83.50 ≈ 1200 INR > 100 INR
        st = pm.book_structure(t["kind"], t["event_title"], t["relation_id"], t["legs"], t["note"])
        assert st is None  # Blocked because exceeds available capital!
        assert len(pm.open_structures) == 0


def test_expiry_tracking():
    with tempfile.TemporaryDirectory() as tmp:
        pm = PortfolioManager(initial_capital_inr=10000.0, data_dir=tmp)
        t = ACTUAL_DATA_TEMPLATES[0]
        st = pm.book_structure(t["kind"], t["event_title"], t["relation_id"], t["legs"], t["note"])
        assert st is not None
        assert st.expiry != ""
        assert st.expiry_display != ""
        for leg in st.legs:
            assert leg.expiry != ""
            assert leg.expiry_display != ""

        # Test reload from disk retains expiry
        pm2 = PortfolioManager(initial_capital_inr=10000.0, data_dir=tmp)
        assert len(pm2.open_structures) == 1
        st2 = pm2.open_structures[0]
        assert st2.expiry == st.expiry
        assert st2.expiry_display == st.expiry_display
        assert st2.legs[0].expiry == st.legs[0].expiry


if __name__ == "__main__":
    test_portfolio_inr_capital_and_booking()
    print("ok test_portfolio_inr_capital_and_booking")
    test_strict_all_legs_settled_rule()
    print("ok test_strict_all_legs_settled_rule")
    test_capital_limit_enforcement()
    print("ok test_capital_limit_enforcement")
    test_expiry_tracking()
    print("ok test_expiry_tracking")
