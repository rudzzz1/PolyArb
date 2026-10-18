"""Tests for Per-Venue Balance Tracking and Risk Limit Enforcement (Phase 1.5)."""
from __future__ import annotations
import tempfile
from pathlib import Path
from polyarb.simulator import PortfolioManager, ACTUAL_DATA_TEMPLATES, INR_PER_USD


def test_per_venue_balance_tracking_and_deductions():
    with tempfile.TemporaryDirectory() as tmp:
        pm = PortfolioManager(initial_capital_inr=10000.0, data_dir=tmp)
        init_poly = pm.balance_polymarket_usd
        init_kalshi = pm.balance_kalshi_usd
        assert init_poly > 0
        assert init_kalshi > 0
        assert round(init_poly + init_kalshi, 1) == round(pm.available_cash_usd, 1)

        # Cross-venue structure: Leg 1 Polymarket, Leg 2 Kalshi
        # Using ACTUAL_DATA_TEMPLATES[1] (threshold ladder: poly + kalshi)
        t = ACTUAL_DATA_TEMPLATES[1]
        st = pm.book_structure(t["kind"], t["event_title"], t["relation_id"], t["legs"], t["note"])
        assert st is not None

        # Verify deductions occurred on both venues
        poly_cost = sum(l.cost_usd for l in st.legs if l.venue == "polymarket")
        kalshi_cost = sum(l.cost_usd for l in st.legs if l.venue == "kalshi")
        assert poly_cost > 0
        assert kalshi_cost > 0
        assert abs(pm.balance_polymarket_usd - (init_poly - poly_cost)) < 1e-2
        assert abs(pm.balance_kalshi_usd - (init_kalshi - kalshi_cost)) < 1e-2

        # Verify locked per venue
        poly_locked, kalshi_locked = pm.get_locked_capital_per_venue()
        assert poly_locked == round(poly_cost, 2)
        assert kalshi_locked == round(kalshi_cost, 2)

        # Test settlement returns proceeds to proper venue
        pm.settle_entire_structure(st.structure_id, world_outcome="YES")
        assert pm.balance_polymarket_usd > (init_poly - poly_cost) or pm.balance_kalshi_usd > (init_kalshi - kalshi_cost)


def test_atomic_file_persistence():
    with tempfile.TemporaryDirectory() as tmp:
        pm = PortfolioManager(initial_capital_inr=10000.0, data_dir=tmp)
        t = ACTUAL_DATA_TEMPLATES[0]
        pm.book_structure(t["kind"], t["event_title"], t["relation_id"], t["legs"], t["note"])

        # Check file exists and tmp file does NOT linger
        json_file = Path(tmp) / "portfolio.json"
        tmp_file = Path(tmp) / "portfolio.tmp"
        assert json_file.exists()
        assert not tmp_file.exists()

        # Reload from disk
        pm2 = PortfolioManager(initial_capital_inr=10000.0, data_dir=tmp)
        assert len(pm2.open_structures) == 1
        assert pm2.balance_polymarket_usd == pm.balance_polymarket_usd
        assert pm2.balance_kalshi_usd == pm.balance_kalshi_usd


if __name__ == "__main__":
    test_per_venue_balance_tracking_and_deductions()
    print("ok test_per_venue_balance_tracking_and_deductions")
    test_atomic_file_persistence()
    print("ok test_atomic_file_persistence")
