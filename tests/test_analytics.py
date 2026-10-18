"""Unit and integration tests for Phase 5 (Analytics & UX).
Tests equity curve tracking, Sharpe ratio, max drawdown, edge decay profiling,
category classification, gross vs net profit model, and approval notes workflow.
"""
from __future__ import annotations
import json
import shutil
import tempfile
from pathlib import Path
from dataclasses import dataclass
import sys
sys.path.insert(0, ".")

from polyarb.analytics import AnalyticsEngine, EquityPoint
from polyarb.approval_gate import ApprovalGate


@dataclass
class MockLeg:
    venue: str
    cost_usd: float
    question: str = "Test question"


@dataclass
class MockStructure:
    structure_id: str
    event_title: str
    kind: str
    total_cost_usd: float
    guaranteed_payoff_usd: float
    realised_profit_usd: float
    projected_profit_usd: float
    legs: list[MockLeg]


def test_analytics_snapshot_and_persistence():
    temp_dir = tempfile.mkdtemp()
    try:
        engine = AnalyticsEngine(data_dir=temp_dir)
        summary = {
            "initial_capital_inr": 10000.0,
            "initial_capital_usd": 119.76,
            "available_cash_usd": 100.0,
            "locked_capital_usd": 19.76,
            "realised_pnl_usd": 2.50,
            "unrealised_pnl_inr": 200.0,
            "total_portfolio_value_inr": 10200.0,
            "open_count": 1,
            "realised_count": 1,
        }
        engine.record_snapshot(summary)
        assert len(engine.equity_curve) == 1
        pt = engine.equity_curve[0]
        assert pt.available_cash_usd == 100.0
        assert pt.locked_capital_usd == 19.76
        assert pt.realised_pnl_usd == 2.50

        # Reload from disk
        engine2 = AnalyticsEngine(data_dir=temp_dir)
        assert len(engine2.equity_curve) == 1
        assert engine2.equity_curve[0].total_value_usd == pt.total_value_usd
        print("ok test_analytics_snapshot_and_persistence")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_sharpe_ratio_calculation():
    temp_dir = tempfile.mkdtemp()
    try:
        engine = AnalyticsEngine(data_dir=temp_dir)
        # 1. Baseline with empty history returns reasonable default
        assert engine.compute_sharpe_ratio() > 0

        # 2. History with steady upward returns
        for val in [100.0, 101.0, 102.5, 104.0, 105.8]:
            engine.equity_curve.append(EquityPoint(
                timestamp="2026-10-18T10:00:00Z",
                total_value_inr=val * 83.5,
                total_value_usd=val,
                available_cash_usd=val,
                locked_capital_usd=0.0,
                realised_pnl_usd=val - 100.0,
                unrealised_edge_usd=0.0,
                open_count=0,
                realised_count=1,
            ))
        sharpe = engine.compute_sharpe_ratio()
        assert sharpe > 1.0  # Consistently positive return produces good Sharpe
        print("ok test_sharpe_ratio_calculation")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_max_drawdown_calculation():
    temp_dir = tempfile.mkdtemp()
    try:
        engine = AnalyticsEngine(data_dir=temp_dir)
        # Simulate: Peak at $120, drop to $108 (10% DD), recovery to $115
        values = [100.0, 110.0, 120.0, 114.0, 108.0, 115.0]
        for val in values:
            engine.equity_curve.append(EquityPoint(
                timestamp="2026-10-18T10:00:00Z",
                total_value_inr=val * 83.5,
                total_value_usd=val,
                available_cash_usd=val,
                locked_capital_usd=0.0,
                realised_pnl_usd=0.0,
                unrealised_edge_usd=0.0,
                open_count=0,
                realised_count=0,
            ))
        dd = engine.compute_max_drawdown()
        # Peak $120 -> trough $108 = $12 drawdown = 10%
        assert abs(dd["max_drawdown_usd"] - 12.0) < 0.01
        assert abs(dd["max_drawdown_pct"] - 10.0) < 0.01
        print("ok test_max_drawdown_calculation")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_category_classification_heuristics():
    assert AnalyticsEngine.classify_category("Will the Federal Reserve cut rates in November?") == "Economics & Rates"
    assert AnalyticsEngine.classify_category("US Presidential Election Winner") == "Politics & Elections"
    assert AnalyticsEngine.classify_category("Bitcoin price reaches 100k by end of year") == "Crypto & Web3"
    assert AnalyticsEngine.classify_category("Super Bowl LVIII Champion") == "Sports & Entertainment"
    assert AnalyticsEngine.classify_category("SpaceX Starship orbital launch test") == "Science & Tech"
    assert AnalyticsEngine.classify_category("Random unknown market") == "General & Culture"
    print("ok test_category_classification_heuristics")


def test_edge_decay_distribution_and_breakdowns():
    temp_dir = tempfile.mkdtemp()
    try:
        engine = AnalyticsEngine(data_dir=temp_dir)
        # Record observations
        engine.record_edge_observation("cand_1", "mutually_exclusive", 0.04, 3.2)  # <5s
        engine.record_edge_observation("cand_2", "partition", 0.03, 11.5)           # 5-15s
        engine.record_edge_observation("cand_3", "bracket", 0.05, 45.0)             # 15-60s
        engine.record_edge_observation("cand_4", "equivalent", 0.02, 90.0)          # >60s

        decay = engine.get_edge_decay_distribution()
        assert decay["buckets"]["< 5s"] == 1
        assert decay["buckets"]["5 - 15s"] == 1
        assert decay["buckets"]["15 - 60s"] == 1
        assert decay["buckets"]["> 60s"] == 1
        assert len(decay["decay_curve"]) >= 5

        # Test episode aggregation
        s1 = MockStructure(
            structure_id="STR-1",
            event_title="Fed Rate Decision 2026",
            kind="mutually_exclusive",
            total_cost_usd=20.0,
            guaranteed_payoff_usd=22.0,
            realised_profit_usd=2.0,
            projected_profit_usd=2.0,
            legs=[MockLeg(venue="polymarket", cost_usd=10.0), MockLeg(venue="kalshi", cost_usd=10.0)],
        )
        s2 = MockStructure(
            structure_id="STR-2",
            event_title="Bitcoin ATH Target",
            kind="partition",
            total_cost_usd=30.0,
            guaranteed_payoff_usd=33.0,
            realised_profit_usd=3.0,
            projected_profit_usd=3.0,
            legs=[MockLeg(venue="polymarket", cost_usd=15.0), MockLeg(venue="manifold", cost_usd=15.0)],
        )

        breakdowns = engine.aggregate_breakdowns([s1], [s2])
        cats = {c["category"] for c in breakdowns["categories"]}
        assert "Economics & Rates" in cats
        assert "Crypto & Web3" in cats

        venues = {v["venue"] for v in breakdowns["venues"]}
        assert "polymarket" in venues
        assert "kalshi" in venues
        assert "manifold" in venues

        # Profit per capital day
        pcd = engine.compute_profit_per_capital_day([s1, s2], locked_capital_usd=50.0)
        assert pcd["net_profit_usd"] == 5.0
        assert pcd["profit_per_capital_day_usd"] > 0
        print("ok test_edge_decay_distribution_and_breakdowns")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_approval_notes_and_expiry_ttl():
    gate = ApprovalGate(quote_ttl_sec=30.0)
    item = gate.submit_for_approval(
        relation_id="rel_diff_test",
        kind="mutually_exclusive",
        event_title="CPI Inflation Benchmark",
        legs=[
            {"market_id": "poly_1", "venue": "polymarket", "side": "YES", "entry_price_usd": 0.45, "shares": 100.0},
            {"market_id": "kalshi_1", "venue": "kalshi", "side": "NO", "entry_price_usd": 0.50, "shares": 100.0},
        ],
        unit_cost=0.95,
        expected_payoff=1.00,
        net_edge=0.05,
        shares=100.0,
    )

    pending = gate.get_pending_items()
    assert len(pending) == 1
    p_item = pending[0]
    assert "ttl_remaining_sec" in p_item
    assert p_item["ttl_remaining_sec"] > 25.0

    # Test approve with note
    ok, msg, approved = gate.approve(item.id, approved_by="senior_trader", notes="Confirmed BLS methodology match")
    assert ok is True
    assert approved.operator_notes == "Confirmed BLS methodology match"
    assert approved.approved_by == "senior_trader"

    # Test reject with note
    item2 = gate.submit_for_approval(
        relation_id="rel_rej_test",
        kind="partition",
        event_title="Election Winner",
        legs=[],
        unit_cost=0.96,
        expected_payoff=1.00,
        net_edge=0.04,
        shares=50.0,
    )
    ok, msg, rejected = gate.reject(item2.id, reason="Rules Ambiguity", notes="Threshold date differs by 12 hours")
    assert ok is True
    assert rejected.rejection_reason == "Rules Ambiguity"
    assert rejected.operator_notes == "Threshold date differs by 12 hours"
    print("ok test_approval_notes_and_expiry_ttl")


if __name__ == "__main__":
    test_analytics_snapshot_and_persistence()
    test_sharpe_ratio_calculation()
    test_max_drawdown_calculation()
    test_category_classification_heuristics()
    test_edge_decay_distribution_and_breakdowns()
    test_approval_notes_and_expiry_ttl()
    print("\nALL PHASE 5 ANALYTICS & UX TESTS PASSED!")
