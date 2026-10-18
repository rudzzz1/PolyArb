"""Unit tests for replay enhancements: disconnect gap handling, phantom-arb filtering,
simulated legging execution, clock skew, and fee breakdowns."""
from __future__ import annotations
import sqlite3
from polyarb.core import Level
from polyarb import recorder, replay


def _db():
    db = sqlite3.connect(":memory:")
    db.executescript(recorder.SCHEMA)
    return db


def _ins(db, ts, mid, yes, no):
    L = lambda xs: recorder.enc([Level(p, s) for p, s in xs])
    db.execute("INSERT INTO snaps VALUES(?,?,?,?)", (ts, mid, L(yes), L(no)))


def test_disconnect_gap_handling():
    db = _db()
    # Market quotes at t=100
    _ins(db, 100, "m1", [(0.60, 50)], [(0.45, 10)])
    _ins(db, 100, "m2", [(0.50, 10)], [(0.55, 50)])

    # Disconnect at t=105, reconnect at t=120
    db.execute("INSERT INTO events VALUES(?,?,?)", (105, "disconnect", "ws0"))
    db.execute("INSERT INTO events VALUES(?,?,?)", (120, "connect", "ws0"))

    tl = replay.Timeline(db)

    # Valid before disconnect
    assert tl.asof("m1", 104) is not None

    # Invalid inside gap
    assert tl.asof("m1", 110) is None
    assert tl.is_in_gap(110) is True

    # Quote from t=100 is NOT carried forward across the disconnect gap to t=125
    assert tl.asof("m1", 125) is None

    # Once a new quote arrives at t=122, it is valid
    _ins(db, 122, "m1", [(0.60, 50)], [(0.45, 10)])
    tl_updated = replay.Timeline(db)
    assert tl_updated.asof("m1", 125) is not None


def test_phantom_arb_filter():
    db = _db()
    # Fleeting 1-tick anomaly at t=100 (arb lasts only 1 poll interval, e.g. 0.1s)
    _ins(db, 100.0, "m1", [(0.60, 50)], [(0.45, 10)])
    _ins(db, 100.0, "m2", [(0.50, 10)], [(0.55, 50)])
    _ins(db, 100.1, "m2", [(0.60, 10)], [(0.55, 50)])  # disappeared 0.1s later

    # Persistent 3-tick episode at t=200..205
    _ins(db, 200.0, "m1", [(0.60, 50)], [(0.45, 10)])
    _ins(db, 200.0, "m2", [(0.50, 10)], [(0.55, 50)])
    _ins(db, 202.0, "m1", [(0.60, 50)], [(0.45, 10)])
    _ins(db, 202.0, "m2", [(0.50, 10)], [(0.55, 50)])
    _ins(db, 204.0, "m1", [(0.60, 50)], [(0.45, 10)])
    _ins(db, 204.0, "m2", [(0.50, 10)], [(0.55, 50)])
    _ins(db, 206.0, "m2", [(0.60, 10)], [(0.55, 50)])  # ends at 206

    mk = {
        "m1": {"id": "m1", "venue": "polymarket", "end": "2026-12-31T00:00:00Z"},
        "m2": {"id": "m2", "venue": "polymarket", "end": "2026-12-31T00:00:00Z"},
    }
    rel = [{"id": "r", "kind": "implies", "markets": ["m1", "m2"]}]

    # With phantom filter ON: the 1-tick blip is filtered out, only persistent episode remains
    eps_filtered = replay.replay(db, rel, mk, filter_phantom=True)
    assert len(eps_filtered) == 1
    assert eps_filtered[0]["start"] == 200.0
    assert eps_filtered[0]["obs_count"] >= 3

    # With phantom filter OFF: both blip and persistent episode are captured
    eps_unfiltered = replay.replay(db, rel, mk, filter_phantom=False)
    assert len(eps_unfiltered) == 2


def test_simulated_legging_execution():
    db = _db()
    # At t=100, both legs look attractive
    _ins(db, 100.0, "m1", [(0.60, 50)], [(0.45, 10)])
    _ins(db, 100.0, "m2", [(0.50, 10)], [(0.55, 50)])

    # But at t=100.25 (after 250ms delay), leg 2 ask slipped higher to 0.58 (0.45 + 0.58 = 1.03 -> loss!)
    _ins(db, 100.25, "m2", [(0.58, 10)], [(0.55, 50)])

    mk = {
        "m1": {"id": "m1", "venue": "polymarket", "end": "2026-12-31T00:00:00Z"},
        "m2": {"id": "m2", "venue": "polymarket", "end": "2026-12-31T00:00:00Z"},
    }
    rel = [{"id": "r", "kind": "implies", "markets": ["m1", "m2"]}]

    # Replay with 0.25s execution delay
    eps = replay.replay(db, rel, mk, filter_phantom=False, exec_delay=0.25)
    assert len(eps) == 1
    legging = eps[0]["legging"]
    # The second leg fill slipped/failed, resulting in simulated legging failure and unwind loss
    assert legging["filled"] is False
    assert legging["net_pnl"] < 0  # realized unwind loss


def test_gross_vs_net_fee_summary():
    db = _db()
    # At t=100: NO_A @ 0.45 + YES_B @ 0.50 = 0.95 -> 5c gross edge
    _ins(db, 100.0, "m1", [(0.60, 50)], [(0.45, 10)])
    _ins(db, 100.0, "m2", [(0.50, 10)], [(0.55, 50)])
    _ins(db, 105.0, "m2", [(0.60, 10)], [(0.55, 50)])

    mk = {
        "m1": {"id": "m1", "venue": "polymarket", "end": "2026-12-31T00:00:00Z"},
        "m2": {"id": "m2", "venue": "kalshi", "ticker": "KXFED", "end": "2026-12-31T00:00:00Z"},
    }
    rel = [{"id": "r", "kind": "implies", "markets": ["m1", "m2"]}]

    # Kalshi charges fee on m2: 0.07 * 0.50 * 0.50 = 0.0175 per share
    eps = replay.replay(db, rel, mk, filter_phantom=False)
    summary = replay.summarize(eps)

    assert summary["gross_profit"] == 0.50  # 10 shares * 0.05
    assert summary["fees_consumed"] > 0.15   # 10 shares * 0.0175 ≈ 0.175
    assert summary["net_profit_at_detection"] < summary["gross_profit"]


if __name__ == "__main__":
    test_disconnect_gap_handling()
    print("ok test_disconnect_gap_handling")
    test_phantom_arb_filter()
    print("ok test_phantom_arb_filter")
    test_simulated_legging_execution()
    print("ok test_simulated_legging_execution")
    test_gross_vs_net_fee_summary()
    print("ok test_gross_vs_net_fee_summary")
