"""Tests for API payload parsing and fixtures, ensuring API drift is detected."""
from __future__ import annotations
import json
from polyarb import gamma, kalshi

# Sample real-world Polymarket Gamma events fixture
POLY_EVENTS_FIXTURE = [
    {
        "id": "1001",
        "slug": "fed-interest-rate-december-2026",
        "title": "Fed interest rate decision in December 2026",
        "negRisk": False,
        "description": "Resolves based on the official Federal Reserve FOMC statement released after the December meeting.",
        "resolutionSource": "Federal Reserve",
        "markets": [
            {
                "conditionId": "poly_m1",
                "question": "Will the Fed cut interest rates in December 2026?",
                "outcomes": json.dumps(["Yes", "No"]),
                "clobTokenIds": json.dumps(["tok_yes_1", "tok_no_1"]),
                "enableOrderBook": True,
                "endDate": "2026-12-16T18:00:00Z",
                "description": "Resolves YES if FOMC announces a target rate reduction.",
                "resolutionSource": "Federal Reserve",
            }
        ]
    }
]

# Sample real-world Kalshi events fixture
KALSHI_EVENTS_FIXTURE = [
    {
        "event_ticker": "FED-26DEC",
        "title": "Federal Reserve Interest Rates Dec 2026",
        "mutually_exclusive": False,
        "settlement_sources": ["Federal Reserve Board of Governors"],
        "rules_primary": "The market resolves to YES if the FOMC announces a decrease in the target federal funds rate.",
        "markets": [
            {
                "ticker": "FED-26DEC-CUT",
                "title": "Will the Fed cut interest rates in December 2026?",
                "yes_sub_title": "",
                "status": "active",
                "close_time": "2026-12-16T18:00:00Z",
                "expiration_time": "2026-12-16T19:00:00Z",
                "rules_primary": "Resolves YES if the target federal funds rate is lowered.",
            }
        ]
    }
]

# Sample Polymarket CLOB book fixture
POLY_BOOK_FIXTURE = {
    "bids": [{"price": "0.48", "size": "150.0"}, {"price": "0.45", "size": "300.0"}],
    "asks": [{"price": "0.52", "size": "200.0"}, {"price": "0.55", "size": "450.0"}],
}

# Sample Kalshi orderbook_fp fixture
KALSHI_BOOK_FIXTURE = {
    "orderbook_fp": {
        "yes_dollars": [["0.50", "120.0"], ["0.48", "250.0"]],
        "no_dollars": [["0.48", "180.0"], ["0.45", "300.0"]],
    }
}


def test_polymarket_parsing_fixture():
    markets = gamma.load_markets(POLY_EVENTS_FIXTURE)
    assert len(markets) == 1
    m = markets["poly_m1"]
    assert m["id"] == "poly_m1"
    assert m["venue"] == "polymarket"
    assert m["yes"] == "tok_yes_1"
    assert m["no"] == "tok_no_1"
    assert m["end"] == "2026-12-16T18:00:00Z"
    assert m["resolution_source"] == "Federal Reserve"
    assert "target rate reduction" in m["resolution_rules"]


def test_kalshi_parsing_fixture():
    markets = kalshi.load_markets(KALSHI_EVENTS_FIXTURE)
    assert len(markets) == 1
    m = markets["kalshi:FED-26DEC-CUT"]
    assert m["id"] == "kalshi:FED-26DEC-CUT"
    assert m["venue"] == "kalshi"
    assert m["ticker"] == "FED-26DEC-CUT"
    assert m["end"] == "2026-12-16T18:00:00Z"
    assert "Federal Reserve" in m["resolution_source"]
    assert "target federal funds rate" in m["resolution_rules"]


def test_kalshi_book_derivation_fixture():
    fp = KALSHI_BOOK_FIXTURE["orderbook_fp"]
    asks = kalshi.asks_from_bids(
        kalshi._levels(fp["yes_dollars"]),
        kalshi._levels(fp["no_dollars"])
    )
    # YES ask = 1 - best NO bid (1 - 0.48 = 0.52)
    assert asks["YES"][0].price == 0.52
    assert asks["YES"][0].size == 180.0
    # NO ask = 1 - best YES bid (1 - 0.50 = 0.50)
    assert asks["NO"][0].price == 0.50
    assert asks["NO"][0].size == 120.0


def test_cross_venue_match_fixture():
    from polyarb import matcher
    p_mk = gamma.load_markets(POLY_EVENTS_FIXTURE)
    k_mk = kalshi.load_markets(KALSHI_EVENTS_FIXTURE)

    matches = matcher.match(p_mk, k_mk)
    assert len(matches) == 1
    rel = matches[0]
    assert rel["kind"] == "equivalent"
    assert rel["markets"] == ["poly_m1", "kalshi:FED-26DEC-CUT"]
    # Risk checker detects that 'Federal Reserve' vs 'Federal Reserve Board of Governors' are different strings!
    assert rel["risk_flags"] == ["RISK_DIFFERENT_RESOLUTION_SOURCES"]


if __name__ == "__main__":
    test_polymarket_parsing_fixture()
    print("ok test_polymarket_parsing_fixture")
    test_kalshi_parsing_fixture()
    print("ok test_kalshi_parsing_fixture")
    test_kalshi_book_derivation_fixture()
    print("ok test_kalshi_book_derivation_fixture")
    test_cross_venue_match_fixture()
    print("ok test_cross_venue_match_fixture")
