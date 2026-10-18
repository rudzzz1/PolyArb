"""Tests for Poller Edge Gate and Risk Verification (Phase 0.1, 0.2, 0.4)."""
from __future__ import annotations
import tempfile
from polyarb.simulator import PortfolioManager
from polyarb.poller import LiveMarketPoller
from polyarb.config import Config
from polyarb.core import Level


def test_poller_rejects_negative_edge():
    """Verify poller never books guaranteed losses or sub-threshold edges."""
    with tempfile.TemporaryDirectory() as tmp:
        pm = PortfolioManager(initial_capital_inr=10000.0, data_dir=tmp)
        poller = LiveMarketPoller(pm, cfg=Config(risk=Config().risk))

        cand = {
            "id": "rel_test_loss",
            "kind": "implies",
            "markets": ["m1", "m2"],
        }
        # Inverted asks: NO(m1) ask = 0.60, YES(m2) ask = 0.45 -> sum = 1.05 > 1.00 (Guaranteed LOSS)
        all_markets = {
            "m1": {"id": "m1", "venue": "polymarket", "question": "Event A?"},
            "m2": {"id": "m2", "venue": "polymarket", "question": "Event B?"},
        }

        # Mock venues.fetch_market_asks to return these losing books
        import polyarb.venues as venues
        orig_fetch = venues.fetch_market_asks
        venues.fetch_market_asks = lambda m: {
            "YES": [Level(0.45, 100.0)],
            "NO": [Level(0.60, 100.0)],
        }

        try:
            booked = poller._evaluate_and_book_candidate(cand, all_markets)
            assert booked is False, "Poller MUST reject trade with negative edge (unit cost 1.05 > payoff 1.00)!"
            assert len(pm.open_structures) == 0
        finally:
            venues.fetch_market_asks = orig_fetch


def test_poller_books_verified_positive_edge():
    """Verify poller books when a verified positive edge is present after fees."""
    with tempfile.TemporaryDirectory() as tmp:
        pm = PortfolioManager(initial_capital_inr=10000.0, data_dir=tmp)
        poller = LiveMarketPoller(pm, cfg=Config())

        cand = {
            "id": "rel_test_profit",
            "kind": "implies",
            "markets": ["m1", "m2"],
        }
        # Favorable asks: NO(m1) ask = 0.45, YES(m2) ask = 0.45 -> sum = 0.90 < 1.00 (Edge = +0.10)
        from datetime import datetime, timezone, timedelta
        valid_end = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        all_markets = {
            "m1": {"id": "m1", "venue": "polymarket", "question": "Event A?", "end": valid_end},
            "m2": {"id": "m2", "venue": "polymarket", "question": "Event B?", "end": valid_end},
        }

        import polyarb.venues as venues
        orig_fetch = venues.fetch_market_asks
        venues.fetch_market_asks = lambda m: {
            "YES": [Level(0.45, 100.0)],
            "NO": [Level(0.45, 100.0)],
        }

        try:
            booked = poller._evaluate_and_book_candidate(cand, all_markets)
            assert booked is True, "Poller should book trade with guaranteed positive edge!"
            assert len(pm.open_structures) == 1
            st = pm.open_structures[0]
            assert st.projected_profit_inr > 0
        finally:
            venues.fetch_market_asks = orig_fetch


if __name__ == "__main__":
    test_poller_rejects_negative_edge()
    print("ok test_poller_rejects_negative_edge")
    test_poller_books_verified_positive_edge()
    print("ok test_poller_books_verified_positive_edge")
