"""Unit tests for On-Chain and Cross-Venue Settlement Monitoring (Phase 4.3)."""
from __future__ import annotations
import tempfile
import time
from polyarb.simulator import PortfolioManager
from polyarb.settlement import SettlementMonitor, MarketSettlementReport
from polyarb.alerts import AlertManager


def test_market_settlement_reports():
    rep_poly = MarketSettlementReport(market_id="p1", venue="polymarket", is_settled=True, winner_outcome="YES")
    assert rep_poly.is_settled and rep_poly.winner_outcome == "YES"

    rep_kalshi = MarketSettlementReport(market_id="k1", venue="kalshi", is_settled=True, winner_outcome="NO")
    assert rep_kalshi.is_settled and rep_kalshi.winner_outcome == "NO"

    rep_mf = MarketSettlementReport(market_id="mf_1", venue="manifold", is_settled=False)
    assert not rep_mf.is_settled


def test_settlement_monitor_cross_venue_lifecycle():
    with tempfile.TemporaryDirectory() as tmp:
        pm = PortfolioManager(initial_capital_inr=10000.0, data_dir=tmp)
        alert_mgr = AlertManager()
        monitor = SettlementMonitor(portfolio=pm, alert_mgr=alert_mgr, max_desync_hours=12.0)

        # Book a 2-leg cross-venue structure
        legs = [
            {"market_id": "poly_mkt_1", "venue": "polymarket", "question": "Fed cut?", "side": "NO", "entry_price_usd": 0.45, "shares": 10.0},
            {"market_id": "kalshi_mkt_1", "venue": "kalshi", "question": "Fed cut?", "side": "YES", "entry_price_usd": 0.45, "shares": 10.0},
        ]
        struct = pm.book_structure("implies", "Fed Rate Decision", "rel_fed_arb", legs_info=legs, unit_payoff=1.0)
        assert struct is not None
        assert len(pm.open_structures) == 1
        assert len(pm.realised_structures) == 0

        # Step 1: Mock Polymarket leg settlement to YES
        monitor.check_polymarket_settlement = lambda mid: MarketSettlementReport(mid, "polymarket", is_settled=True, winner_outcome="NO")
        # Kalshi is not yet settled
        monitor.check_kalshi_settlement = lambda mid: MarketSettlementReport(mid, "kalshi", is_settled=False)

        res1 = monitor.reconcile_structure_settlements()
        assert len(pm.open_structures) == 1  # Still open because Kalshi is not settled!
        assert len(pm.realised_structures) == 0
        leg1 = struct.legs[0]
        assert leg1.settlement_status == "SETTLED"

        # Step 2: Simulate settlement desync (> 12 hours)
        monitor._leg_settlement_timestamps[leg1.leg_id] = time.time() - (15.0 * 3600.0)
        res2 = monitor.reconcile_structure_settlements()
        assert res2["desync_alerts"] > 0

        # Step 3: Now Kalshi also settles
        monitor.check_kalshi_settlement = lambda mid: MarketSettlementReport(mid, "kalshi", is_settled=True, winner_outcome="YES")
        res3 = monitor.reconcile_structure_settlements()

        # Both legs are now settled -> Trade moves to Realised!
        assert len(pm.open_structures) == 0
        assert len(pm.realised_structures) == 1
        realised = pm.realised_structures[0]
        assert realised.status == "REALISED"
        assert realised.realised_profit_inr > 0


if __name__ == "__main__":
    test_market_settlement_reports()
    print("ok test_market_settlement_reports")
    test_settlement_monitor_cross_venue_lifecycle()
    print("ok test_settlement_monitor_cross_venue_lifecycle")
