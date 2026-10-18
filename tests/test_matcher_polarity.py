"""Unit tests for matcher enhancements: polarity checks, resolution risk auto-flagging,
threshold ladders, and approval lifecycle."""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from polyarb.matcher import (
    detect_polarity, check_resolution_mismatches, extract_numeric_threshold,
    find_threshold_ladders, match, approve_relation, is_approval_expired
)


def test_polarity_detection():
    # Positive framing
    assert detect_polarity("Will Candidate A win the election?") == "POS"
    assert detect_polarity("Will BTC reach 100,000 by December?") == "POS"
    assert detect_polarity("Will the bill pass in the Senate?") == "POS"
    assert detect_polarity("Will the Fed raise rates above 5%?") == "POS"

    # Negative framing
    assert detect_polarity("Will Candidate A lose the election?") == "NEG"
    assert detect_polarity("Will BTC drop below 80,000?") == "NEG"
    assert detect_polarity("Will the referendum fail?") == "NEG"
    assert detect_polarity("Will the team not qualify for the finals?") == "NEG"

    # Neutral framing
    assert detect_polarity("What will the CPI number be?") == "NEUTRAL"


def test_opposite_polarity_rejection():
    poly = {
        "p1": {
            "id": "p1",
            "question": "Will Trump win the 2024 Presidential Election?",
            "end": "2024-11-06T00:00:00Z",
            "resolution_source": "AP",
        }
    }
    kalshi = {
        "k1": {
            "id": "k1",
            "question": "Will Trump lose the 2024 Presidential Election?",
            "end": "2024-11-06T00:00:00Z",
            "resolution_source": "AP",
        }
    }
    # Matches will detect opposite polarity and flag it!
    results = match(poly, kalshi, min_score=0.4)
    assert len(results) == 1
    assert "RISK_POLARITY_MISMATCH" in results[0]["risk_flags"]
    assert "OPPOSITE POLARITY" in results[0]["note"]


def test_resolution_rule_mismatches():
    # 1. Cutoff time mismatch (> 24 hours)
    m1 = {"question": "Fed rate cut in Dec?", "end": "2026-12-10T00:00:00Z", "resolution_source": "Fed"}
    m2 = {"question": "Fed rate cut in Dec?", "end": "2026-12-15T00:00:00Z", "resolution_source": "Fed"}
    flags = check_resolution_mismatches(m1, m2)
    assert any("CUTOFF_MISMATCH" in f for f in flags)

    # 2. Timing semantics: "by" vs "in"
    m_by = {"question": "Will BTC reach 100k by March 31?", "end": "2026-03-31T00:00:00Z"}
    m_in = {"question": "Will BTC reach 100k in March?", "end": "2026-03-31T00:00:00Z"}
    flags_by_in = check_resolution_mismatches(m_by, m_in)
    assert "RISK_BY_VS_IN_SEMANTICS" in flags_by_in

    # 3. Source mismatch
    m_src1 = {"question": "Will X happen?", "end": "2026-12-31T00:00:00Z", "resolution_source": "Associated Press"}
    m_src2 = {"question": "Will X happen?", "end": "2026-12-31T00:00:00Z", "resolution_source": "Fox News"}
    flags_src = check_resolution_mismatches(m_src1, m_src2)
    assert "RISK_DIFFERENT_RESOLUTION_SOURCES" in flags_src


def test_numeric_threshold_extraction():
    val, direction = extract_numeric_threshold("Will Bitcoin be above 100,000 on Dec 31?")
    assert val == 100000.0 and direction == "above"

    val2, dir2 = extract_numeric_threshold("Will Ethereum drop below 2,500 by Friday?")
    assert val2 == 2500.0 and dir2 == "below"


def test_threshold_ladders():
    markets = {
        "m1": {"id": "m1", "question": "Will BTC be above 90,000 on Dec 31?", "end": "2026-12-31T00:00:00Z"},
        "m2": {"id": "m2", "question": "Will BTC be above 100,000 on Dec 31?", "end": "2026-12-31T00:00:00Z"},
        "m3": {"id": "m3", "question": "Will BTC be above 110,000 on Dec 31?", "end": "2026-12-31T00:00:00Z"},
    }
    ladders = find_threshold_ladders(markets)
    # 3 markets -> 3 pairs of implies: (110k => 100k), (110k => 90k), (100k => 90k)
    assert len(ladders) == 3
    # Check that high implies low:
    pair_100_90 = next(r for r in ladders if set(r["markets"]) == {"m1", "m2"})
    assert pair_100_90["markets"] == ["m2", "m1"]  # m2 (100k) implies m1 (90k)


def test_approval_lifecycle_and_expiry():
    rel = {"id": "r1", "kind": "implies", "markets": ["a", "b"], "approved": False}
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    # Approve for 14 days
    approve_relation(rel, approved_by="analyst_alice", expiry_days=14, now=now)
    assert rel["approved"] is True
    assert rel["approved_by"] == "analyst_alice"
    assert rel["approved_at"] == now.isoformat()

    # Check validity after 5 days
    assert not is_approval_expired(rel, now=now + timedelta(days=5))

    # Check expiration after 15 days
    assert is_approval_expired(rel, now=now + timedelta(days=15))


if __name__ == "__main__":
    test_polarity_detection()
    print("ok test_polarity_detection")
    test_opposite_polarity_rejection()
    print("ok test_opposite_polarity_rejection")
    test_resolution_rule_mismatches()
    print("ok test_resolution_rule_mismatches")
    test_numeric_threshold_extraction()
    print("ok test_numeric_threshold_extraction")
    test_threshold_ladders()
    print("ok test_threshold_ladders")
    test_approval_lifecycle_and_expiry()
    print("ok test_approval_lifecycle_and_expiry")
