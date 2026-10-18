from polyarb.core import best_basket, sweep, Level
from polyarb.gamma import normalise

def A(y, n): return {"YES": y, "NO": n}

def test_implies_arb():
    b = best_basket("implies", [A(.60, .45), A(.50, .55)])
    assert set(b.legs) == {(0, "NO"), (1, "YES")}
    assert abs(b.edge - 0.05) < 1e-9

def test_implies_no_arb():
    b = best_basket("implies", [A(.40, .65), A(.60, .45)])
    assert b.edge <= 0

def test_partition_buy_all_yes():
    b = best_basket("partition", [A(.3, .75)] * 3)
    assert abs(b.edge - 0.1) < 1e-9 and all(s == "YES" for _, s in b.legs)

def test_partition_no_arb():
    assert best_basket("partition", [A(.35, .70)] * 3).edge < 0

def test_exclusive_buy_all_no():
    b = best_basket("exclusive", [A(.40, .65)] * 3)
    assert abs(b.edge - 0.05) < 1e-9 and all(s == "NO" for _, s in b.legs)

def test_large_group_uniform():
    b = best_basket("partition", [A(.09, .95)] * 12)
    assert abs(b.edge - (1 - 1.08)) < 1e-9

def test_fee_kills_thin_edge():
    assert best_basket("implies", [A(.60, .45), A(.50, .55)], fee_bps=600).edge < 0

def test_sweep_depth():
    l1 = [Level(.45, 10), Level(.50, 10)]
    l2 = [Level(.50, 5), Level(.60, 100)]
    sh, cost = sweep([l1, l2], 1.0)
    assert sh == 5 and abs(cost - 4.75) < 1e-9

def test_normalise():
    a = normalise("Will X happen by March 31, 2026?")
    b = normalise("Will X happen by June 30, 2026?")
    assert a == b


# ---- cross-venue additions ----
from polyarb.kalshi import asks_from_bids, kalshi_fee
from polyarb.matcher import match

def test_cross_venue_equivalent_with_fee():
    poly, kal = A(.55, .47), A(.50, .52)
    b0 = best_basket("equivalent", [poly, kal])           # no fees
    assert set(b0.legs) == {(0, "NO"), (1, "YES")} and abs(b0.edge - .03) < 1e-9
    b1 = best_basket("equivalent", [poly, kal], fees=[None, kalshi_fee])
    assert abs(b1.edge - (.03 - kalshi_fee(.50))) < 1e-9   # fee eats ~1.75c

def test_kalshi_asks_from_bids():
    a = asks_from_bids([(.42, 10), (.40, 5)], [(.55, 7)])
    assert a["YES"][0].price == .45 and a["YES"][0].size == 7   # 1 - NO bid
    assert a["NO"][0].price == .58                              # 1 - best YES bid

def test_sweep_with_fee_fn():
    sh, cost = sweep([[Level(.45, 10)], [Level(.50, 10)]], 1.0, fees=[None, kalshi_fee])
    assert sh == 10 and abs(cost - 10 * (.95 + kalshi_fee(.50))) < 1e-9

def test_matcher():
    p = {"p1": {"id": "p1", "question": "Will the Fed cut rates in December 2026?", "end": "2026-12-16T00:00:00Z"},
         "p2": {"id": "p2", "question": "Will BTC be above 100000 on Dec 31?", "end": "2026-12-31T00:00:00Z"}}
    k = {"k1": {"id": "k1", "question": "Fed cut rates in December?", "end": "2026-12-17T00:00:00Z"},
         "k2": {"id": "k2", "question": "Will BTC be above 110000 on Dec 31?", "end": "2026-12-31T00:00:00Z"}}
    r = match(p, k)
    assert len(r) == 1 and r[0]["markets"] == ["p1", "k1"]     # BTC threshold mismatch rejected


# ---- recorder / replay ----
import sqlite3
from polyarb import recorder, replay

def _db():
    db = sqlite3.connect(":memory:"); db.executescript(recorder.SCHEMA); return db

def _ins(db, ts, mid, yes, no):
    L = lambda xs: recorder.enc([Level(p, s) for p, s in xs])
    db.execute("INSERT INTO snaps VALUES(?,?,?,?)", (ts, mid, L(yes), L(no)))

def test_record_dedup():
    db, last = _db(), {}
    mk = {"m": {"id": "m", "venue": "polymarket"}}
    book = {"YES": [Level(.5, 10)], "NO": [Level(.5, 10)]}
    f = lambda m: book
    assert recorder.record_once(db, mk, ["m"], last, fetch=f, now=lambda: 1.0) == (1, 0)
    assert recorder.record_once(db, mk, ["m"], last, fetch=f, now=lambda: 2.0) == (0, 0)  # unchanged
    book = {"YES": [Level(.6, 10)], "NO": [Level(.5, 10)]}
    assert recorder.record_once(db, mk, ["m"], last, fetch=f, now=lambda: 3.0) == (1, 0)
    assert recorder.load_last(db)["m"][0] == recorder.enc([Level(.6, 10)])

def test_replay_episode_and_decay():
    db = _db()
    _ins(db, 100, "m1", [(.60, 50)], [(.45, 10)])
    _ins(db, 100, "m2", [(.50, 10)], [(.55, 50)])      # NO_A .45 + YES_B .50 = .95 -> arb
    _ins(db, 110, "m2", [(.60, 10)], [(.55, 50)])      # arb disappears
    mk = {k: {"id": k, "venue": "polymarket"} for k in ("m1", "m2")}
    rel = [{"id": "r", "kind": "implies", "markets": ["m1", "m2"]}]
    eps = replay.replay(db, rel, mk, horizons=(5, 15))
    assert len(eps) == 1 and eps[0]["duration"] == 10 and not eps[0]["censored"]
    assert abs(eps[0]["profit0"] - 0.5) < 1e-9          # 10 shares * 5c
    assert eps[0]["after"][5] > 0 and eps[0]["after"][15] == 0
    assert replay.summarize(eps, (5, 15))["alive_after_15s"] == "0/1"


# ---- websocket router ----
from polyarb.stream import Router

def _lv(*xs): return [{"price": str(p), "size": str(z)} for p, z in xs]

def _router():
    mk = {"m": {"id": "m", "venue": "polymarket", "yes": "Y", "no": "N"}}
    r = Router(mk)
    r.handle_raw([{"event_type": "book", "asset_id": "Y", "bids": _lv((.50, 5)), "asks": _lv((.60, 5), (.55, 10), (.70, 0))},
                  {"event_type": "book", "asset_id": "N", "bids": _lv((.40, 5)), "asks": _lv((.48, 9))}])
    return r

def test_router_snapshot_and_deltas():
    r = _router()
    s = r.snapshot("m")
    assert [(l.price, l.size) for l in s["YES"]] == [(.55, 10), (.60, 5)]      # sorted, zero-size dropped
    d = r.handle({"event_type": "price_change", "price_changes": [
        {"asset_id": "Y", "price": "0.55", "size": "0", "side": "SELL"},       # level removed
        {"asset_id": "Y", "price": "0.52", "size": "7", "side": "SELL"},       # new best ask
        {"asset_id": "Y", "price": "0.49", "size": "3", "side": "BUY"}]})      # bid: no effect on asks
    assert d == {"m"}
    assert [(l.price, l.size) for l in r.snapshot("m")["YES"]] == [(.52, 7), (.60, 5)]
    r.handle({"event_type": "price_change", "asset_id": "N", "changes": [{"price": "0.48", "size": "4", "side": "SELL"}]})
    assert r.snapshot("m")["NO"][0].size == 4                                  # legacy 'changes' shape

def test_router_waits_for_both_books_and_ignores_unknown():
    mk = {"m": {"id": "m", "yes": "Y", "no": "N"}}
    r = Router(mk)
    assert r.handle({"event_type": "book", "asset_id": "Y", "asks": _lv((.5, 1))}) == {"m"}
    assert r.snapshot("m") is None                                             # NO book not seen yet
    assert r.handle({"event_type": "book", "asset_id": "ZZZ", "asks": []}) == set()
    assert r.handle({"event_type": "price_change", "price_changes": [
        {"asset_id": "N", "price": "0.4", "size": "1", "side": "SELL"}]}) == set()  # delta before snapshot

def test_stream_store_roundtrip():
    db, last = _db(), {}
    r = _router()
    assert recorder.store(db, last, "m", 1.0, r.snapshot("m")) is True
    assert recorder.store(db, last, "m", 2.0, r.snapshot("m")) is False        # unchanged
    assert replay.Timeline(db).asof("m", 5.0)["YES"][0].price == .55

if __name__ == "__main__":  # run all
    for k, v in list(globals().items()):
        if k.startswith("test_"):
            v(); print("ok", k)
