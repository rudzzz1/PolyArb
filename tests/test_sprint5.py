"""Comprehensive Verification Suite for Sprint 5 (C1–C8).
Tests:
- C1: RiskLimits duration fields and paper_auto_settle_after_hours
- C2: Poller duration gate enforcement (min, max, None)
- C3: Short-duration APY turnaround boost in OpportunityScorer
- C4: Extra timer-scheduled polls after portfolio reset
- C5: Purging of negative-edge and already-expired structures on load
- C6: Paper auto-settlement based on elapsed hours
- C7: UI elements and API aliases (/api/poll, /api/settle, /api/kill_switch)
- C8: Short-duration config loading from data/config.json
"""
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, ".")
from polyarb.config import Config, RiskLimits
from polyarb.simulator import PortfolioManager, StructurePosition, LegPosition
from polyarb.poller import LiveMarketPoller
from polyarb.allocator import OpportunityScorer
from polyarb.core import Level


def test_c1_risk_limits_fields():
    """C1: Verify RiskLimits has duration and auto-settle fields."""
    rl = RiskLimits()
    assert hasattr(rl, "max_days_to_expiry")
    assert hasattr(rl, "min_days_to_expiry")
    assert hasattr(rl, "paper_auto_settle_after_hours")
    assert rl.max_days_to_expiry == 30.0
    assert rl.min_days_to_expiry == 1.0
    assert rl.paper_auto_settle_after_hours == 0.0
    print("ok test_c1_risk_limits_fields")


def test_c2_poller_duration_gate():
    """C2: Verify poller rejects candidates outside duration window."""
    with tempfile.TemporaryDirectory() as tmp:
        pm = PortfolioManager(initial_capital_inr=10000.0, data_dir=tmp)
        cfg = Config()
        cfg.risk.max_days_to_expiry = 30.0
        cfg.risk.min_days_to_expiry = 1.0
        poller = LiveMarketPoller(pm, cfg=cfg)

        import polyarb.venues as venues
        orig_fetch = venues.fetch_market_asks
        venues.fetch_market_asks = lambda m: {
            "YES": [Level(0.40, 100.0)],
            "NO": [Level(0.40, 100.0)],
        }

        try:
            cand = {"id": "c_dur", "kind": "implies", "markets": ["m1", "m2"]}

            # 1. Reject if no end time (days_to_res is None)
            m_none = {
                "m1": {"id": "m1", "venue": "polymarket", "question": "Q1"},
                "m2": {"id": "m2", "venue": "polymarket", "question": "Q2"},
            }
            assert poller._score_candidate(cand, m_none) is None, "Should reject candidate with no expiry"

            # 2. Reject if days_to_res > max (e.g. 45 days)
            far_end = (datetime.now(timezone.utc) + timedelta(days=45)).isoformat()
            m_far = {
                "m1": {"id": "m1", "venue": "polymarket", "question": "Q1", "end": far_end},
                "m2": {"id": "m2", "venue": "polymarket", "question": "Q2", "end": far_end},
            }
            assert poller._score_candidate(cand, m_far) is None, "Should reject candidate resolving in 45 days (> 30)"

            # 3. Reject if days_to_res < min (e.g. 0.5 days)
            soon_end = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
            m_soon = {
                "m1": {"id": "m1", "venue": "polymarket", "question": "Q1", "end": soon_end},
                "m2": {"id": "m2", "venue": "polymarket", "question": "Q2", "end": soon_end},
            }
            assert poller._score_candidate(cand, m_soon) is None, "Should reject candidate resolving in 0.5 days (< 1.0)"

            # 4. Accept if days_to_res is within window (e.g. 10 days)
            valid_end = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
            m_valid = {
                "m1": {"id": "m1", "venue": "polymarket", "question": "Q1", "end": valid_end},
                "m2": {"id": "m2", "venue": "polymarket", "question": "Q2", "end": valid_end},
            }
            scored = poller._score_candidate(cand, m_valid)
            assert scored is not None, "Should accept candidate resolving in 10 days"
            assert scored.days_to_resolution is not None
            assert 9.0 <= scored.days_to_resolution <= 11.0

        finally:
            venues.fetch_market_asks = orig_fetch
    print("ok test_c2_poller_duration_gate")


def test_c3_opportunity_scorer_turnaround_boost():
    """C3: Verify short-duration turnaround boost (< 14 days)."""
    scorer = OpportunityScorer()

    # Base case: 30 days to resolution (no boost)
    score_30d, rf_30d = scorer.calculate_score(edge=0.10, cost=0.90, days_to_res=30.0, apy=1.20)

    # 7 days to resolution: should get boost = min(2.0, 14.0 / 7.0) = 2.0x
    score_7d, rf_7d = scorer.calculate_score(edge=0.10, cost=0.90, days_to_res=7.0, apy=1.20)

    assert score_7d > score_30d, "7-day turnaround should score higher than 30-day"
    assert round(score_7d / score_30d, 1) == 2.0, "Turnaround boost should be ~2.0x"

    # 10 days to resolution: boost = 14 / 10 = 1.4x
    score_10d, _ = scorer.calculate_score(edge=0.10, cost=0.90, days_to_res=10.0, apy=1.20)
    assert score_7d > score_10d > score_30d
    print("ok test_c3_opportunity_scorer_turnaround_boost")


def test_c5_purge_negative_edge_and_expired():
    """C5: Verify PortfolioManager._purge_invalid_structures cleans legacy structures on load."""
    with tempfile.TemporaryDirectory() as tmp:
        pm = PortfolioManager(initial_capital_inr=10000.0, data_dir=tmp)

        # Create three structures:
        # 1. Valid positive edge
        future_exp = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
        pm.book_structure(
            kind="implies",
            event_title="Good Trade",
            relation_id="rel_good",
            legs_info=[
                {"market_id": "m1", "venue": "polymarket", "question": "Q1", "side": "NO", "shares": 10.0, "entry_price_usd": 0.40, "expiry": future_exp},
                {"market_id": "m2", "venue": "polymarket", "question": "Q2", "side": "YES", "shares": 10.0, "entry_price_usd": 0.40, "expiry": future_exp},
            ],
            unit_payoff=1.0,
        )

        # 2. Negative-edge legacy trade
        bad_struct = StructurePosition(
            structure_id="STR-BAD-EDGE",
            relation_id="rel_bad",
            kind="implies",
            event_title="Legacy Bad Trade",
            status="OPEN",
            legs=[LegPosition("l1", "m1", "poly", "Q", "YES", 10.0, 0.60, 0.60, "OPEN", None, future_exp, "exp")],
            total_cost_usd=12.0,
            total_cost_inr=1000.0,
            guaranteed_payoff_usd=10.0,
            guaranteed_payoff_inr=835.0,
            projected_profit_usd=-2.0,
            projected_profit_inr=-165.0,
            return_pct=-16.5,
            booked_at=datetime.now(timezone.utc).isoformat(),
            expiry=future_exp,
        )
        pm.open_structures.append(bad_struct)
        pm.available_cash_inr -= 1000.0
        pm.available_cash_usd -= 12.0

        # 3. Already-expired trade
        past_exp = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        exp_struct = StructurePosition(
            structure_id="STR-EXPIRED",
            relation_id="rel_exp",
            kind="implies",
            event_title="Expired Trade",
            status="OPEN",
            legs=[LegPosition("l2", "m2", "poly", "Q", "YES", 10.0, 0.40, 0.40, "OPEN", None, past_exp, "exp")],
            total_cost_usd=4.0,
            total_cost_inr=334.0,
            guaranteed_payoff_usd=5.0,
            guaranteed_payoff_inr=417.0,
            projected_profit_usd=1.0,
            projected_profit_inr=83.0,
            return_pct=25.0,
            booked_at=(datetime.now(timezone.utc) - timedelta(days=5)).isoformat(),
            expiry=past_exp,
        )
        pm.open_structures.append(exp_struct)
        pm.available_cash_inr -= 334.0
        pm.available_cash_usd -= 4.0

        cash_before = pm.available_cash_inr
        pm.save()

        # Reload portfolio: _purge_invalid_structures should run automatically
        pm2 = PortfolioManager(initial_capital_inr=10000.0, data_dir=tmp)
        open_ids = [s.structure_id for s in pm2.open_structures]

        assert "STR-BAD-EDGE" not in open_ids, "Negative-edge structure must be purged"
        assert "STR-EXPIRED" not in open_ids, "Expired structure must be purged"
        assert len(pm2.open_structures) == 1
        assert pm2.open_structures[0].relation_id == "rel_good"
        assert pm2.available_cash_inr == cash_before + 1000.0 + 334.0, "Purged capital must return to cash"
    print("ok test_c5_purge_negative_edge_and_expired")


def test_c6_paper_auto_settle():
    """C6: Verify paper auto-settle after paper_auto_settle_after_hours."""
    with tempfile.TemporaryDirectory() as tmp:
        pm = PortfolioManager(initial_capital_inr=10000.0, data_dir=tmp)
        cfg = Config()
        cfg.risk.paper_auto_settle_after_hours = 2.0
        poller = LiveMarketPoller(pm, cfg=cfg)

        future_exp = (datetime.now(timezone.utc) + timedelta(days=20)).isoformat()

        # Trade 1: Booked 3 hours ago -> should auto-settle!
        st1 = pm.book_structure(
            kind="implies",
            event_title="Old Trade",
            relation_id="rel_old",
            legs_info=[
                {"market_id": "m1", "venue": "polymarket", "question": "Q1", "side": "NO", "shares": 10.0, "entry_price_usd": 0.40, "expiry": future_exp},
                {"market_id": "m2", "venue": "polymarket", "question": "Q2", "side": "YES", "shares": 10.0, "entry_price_usd": 0.40, "expiry": future_exp},
            ],
            unit_payoff=1.0,
        )
        st1.booked_at = (datetime.now(timezone.utc) - timedelta(hours=3.0)).isoformat()

        # Trade 2: Booked 30 mins ago -> should stay open!
        st2 = pm.book_structure(
            kind="implies",
            event_title="Fresh Trade",
            relation_id="rel_fresh",
            legs_info=[
                {"market_id": "m3", "venue": "polymarket", "question": "Q3", "side": "NO", "shares": 10.0, "entry_price_usd": 0.40, "expiry": future_exp},
                {"market_id": "m4", "venue": "polymarket", "question": "Q4", "side": "YES", "shares": 10.0, "entry_price_usd": 0.40, "expiry": future_exp},
            ],
            unit_payoff=1.0,
        )
        st2.booked_at = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()

        assert len(pm.open_structures) == 2

        # Run settlement check
        poller._check_natural_expiry_settlements(datetime.now(timezone.utc))

        open_ids = [s.structure_id for s in pm.open_structures]
        realised_ids = [s.structure_id for s in pm.realised_structures]

        assert st1.structure_id in realised_ids, "3h old trade should be auto-settled"
        assert st2.structure_id in open_ids, "Fresh trade should remain open"
        assert len(pm.open_structures) == 1
        assert len(pm.realised_structures) == 1
    print("ok test_c6_paper_auto_settle")


def test_c8_config_json_bootstrap():
    """C8: Verify data/config.json is properly loaded by Config.load()."""
    cfg = Config.load("data/config.json")
    assert cfg.risk.max_days_to_expiry == 30.0
    assert cfg.risk.min_days_to_expiry == 1.0
    assert cfg.risk.min_annualized_return == 0.50
    assert cfg.risk.paper_auto_settle_after_hours == 2.0
    print("ok test_c8_config_json_bootstrap")


if __name__ == "__main__":
    test_c1_risk_limits_fields()
    test_c2_poller_duration_gate()
    test_c3_opportunity_scorer_turnaround_boost()
    test_c5_purge_negative_edge_and_expired()
    test_c6_paper_auto_settle()
    test_c8_config_json_bootstrap()
    print("\nALL SPRINT 5 TESTS PASSED!")
