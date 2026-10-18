"""Unit tests for Bracket Matching, Entity Normalization, and Maker-Taker Execution (Phase 3.3, 3.4, 3.5)."""
from __future__ import annotations
from polyarb.matcher import (
    extract_series_slug, normalize_entity_name, toks, find_bracket_structures, match
)
from polyarb.orchestrator import ExecutionOrchestrator, OrchestratedLeg


def test_series_slug_extraction():
    m1 = {"id": "m1", "series_ticker": "KXFEDRATE", "question": "Fed rate cut?"}
    assert extract_series_slug(m1) == "kxfedrate"

    m2 = {"id": "m2", "group_slug": "presidential-election-2024", "question": "President?"}
    assert extract_series_slug(m2) == "presidential-election-2024"

    m3 = {"id": "m3", "question": "Independent question"}
    assert extract_series_slug(m3) == ""


def test_entity_normalization_and_toks():
    # 1. normalize_entity_name
    norm_btc = normalize_entity_name("Will BTC reach 100k?")
    assert "bitcoin" in norm_btc

    norm_fed = normalize_entity_name("Will Fed cut rates in Dec?")
    assert "federal reserve" in norm_fed

    # 2. toks bidirectional alias expansion
    t_btc = toks("Will BTC hit 100k?")
    assert "btc" in t_btc
    assert "bitcoin" in t_btc

    t_bitcoin = toks("Will Bitcoin hit 100k?")
    assert "btc" in t_bitcoin
    assert "bitcoin" in t_bitcoin

    # Both token sets share 'btc', 'bitcoin', 'hit', '100k'
    assert len(t_btc & t_bitcoin) >= 4


def test_bracket_partition_discovery():
    # 4 mutually exclusive brackets in a Kalshi series
    mkts = {
        "b1": {"id": "b1", "series_ticker": "KXFED", "question": "Fed rate < 4.00%", "end": "2026-12-15T00:00:00Z"},
        "b2": {"id": "b2", "series_ticker": "KXFED", "question": "Fed rate 4.00% to 4.50%", "end": "2026-12-15T00:00:00Z"},
        "b3": {"id": "b3", "series_ticker": "KXFED", "question": "Fed rate 4.50% to 5.00%", "end": "2026-12-15T00:00:00Z"},
        "b4": {"id": "b4", "series_ticker": "KXFED", "question": "Fed rate > 5.00%", "end": "2026-12-15T00:00:00Z"},
        # Another unrelated market
        "other": {"id": "other", "series_ticker": "OTHER", "question": "Unrelated single market", "end": "2026-12-15T00:00:00Z"},
    }

    brackets = find_bracket_structures(mkts, min_legs=3, max_legs=5)
    assert len(brackets) == 1
    rel = brackets[0]
    assert rel["kind"] == "partition"
    assert len(rel["markets"]) == 4
    assert "BRACKET_PARTITION" in rel["risk_flags"]


def test_maker_taker_policy():
    orch = ExecutionOrchestrator(leg_ordering_policy="maker_taker")
    legs = [
        OrchestratedLeg("m1", "polymarket", "YES", 0.50, 10.0, available_depth=500.0, order_type="IOC"),
        OrchestratedLeg("m2", "kalshi", "NO", 0.40, 10.0, available_depth=30.0, order_type="IOC"),
    ]

    sequenced = orch.sequence_legs(legs)
    # Thinnest leg (kalshi with depth 30) should be #1 and marked as LIMIT
    assert sequenced[0].venue == "kalshi"
    assert sequenced[0].order_type == "LIMIT"

    # Second leg remains taker IOC
    assert sequenced[1].venue == "polymarket"
    assert sequenced[1].order_type == "IOC"


if __name__ == "__main__":
    test_series_slug_extraction()
    print("ok test_series_slug_extraction")
    test_entity_normalization_and_toks()
    print("ok test_entity_normalization_and_toks")
    test_bracket_partition_discovery()
    print("ok test_bracket_partition_discovery")
    test_maker_taker_policy()
    print("ok test_maker_taker_policy")
