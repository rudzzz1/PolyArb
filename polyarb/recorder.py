"""Order-book recorder. Polls ask ladders for every market in the chosen relations,
stores a row ONLY when a book changes (replay carries state forward as-of any time).
Timestamp = when that market's fetch completed, so per-leg skew is visible in the data."""
from __future__ import annotations
import json, sqlite3, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .core import Level, validate_book
from .config import is_kill_switch_active
from . import venues

SCHEMA = """CREATE TABLE IF NOT EXISTS snaps(ts REAL, market_id TEXT, yes TEXT, no TEXT);
CREATE INDEX IF NOT EXISTS snaps_m ON snaps(market_id, ts);
CREATE TABLE IF NOT EXISTS events(ts REAL, kind TEXT, detail TEXT)"""


def enc(levels: list[Level], depth: int = 10) -> str:
    return json.dumps([[l.price, l.size] for l in levels[:depth]])


def dec(s: str) -> list[Level]:
    return [Level(p, sz) for p, sz in json.loads(s)]


def tracked_ids(relations: list[dict]) -> list[str]:
    seen = {}
    for r in relations:
        for i in r["markets"]:
            seen[i] = True
    return list(seen)


def load_last(db: sqlite3.Connection) -> dict[str, tuple[str, str]]:
    q = """SELECT market_id, yes, no FROM snaps s
           WHERE ts = (SELECT MAX(ts) FROM snaps WHERE market_id = s.market_id)"""
    return {m: (y, n) for m, y, n in db.execute(q)}


def store(db: sqlite3.Connection, last: dict, mid: str, ts: float, book: dict, depth: int = 10) -> bool:
    """Insert a snapshot if valid and differs from the last stored one. Returns True if written."""
    valid, _ = validate_book(book)
    if not valid:
        return False

    cur = (enc(book["YES"], depth), enc(book["NO"], depth))
    if last.get(mid) == cur:
        return False
    last[mid] = cur
    db.execute("INSERT INTO snaps VALUES(?,?,?,?)", (ts, mid, *cur))
    return True


def record_once(db: sqlite3.Connection, markets: dict, ids: list[str], last: dict,
                fetch=venues.fetch_market_asks, depth: int = 10, workers: int = 8,
                now=time.time) -> tuple[int, int]:
    def job(mid):
        if mid not in markets:
            return mid, None, None, KeyError(f"market {mid} not in markets.json")
        try:
            b = fetch(markets[mid])
            return mid, now(), b, None
        except Exception as e:
            return mid, None, None, e

    new = errs = 0
    with ThreadPoolExecutor(workers) as ex:
        for mid, ts, b, err in ex.map(job, ids):
            if err:
                errs += 1
                continue
            if store(db, last, mid, ts, b, depth):
                new += 1
    db.commit()
    return new, errs


def run(data_dir: str = "data", interval: float = 5.0, depth: int = 10, workers: int = 8,
        all_candidates: bool = False):
    d = Path(data_dir)
    markets = json.loads((d / "markets.json").read_text(encoding="utf-8"))
    rels = json.loads((d / "candidates.json").read_text(encoding="utf-8"))
    if not all_candidates:
        rels = [r for r in rels if r.get("approved")]
    ids = tracked_ids(rels)
    print(f"recording {len(ids)} markets from {len(rels)} relations every {interval}s")
    db = sqlite3.connect(d / "books.sqlite", timeout=30)
    db.executescript(SCHEMA)
    last = load_last(db)
    try:
        while True:
            if is_kill_switch_active(d):
                print("[CIRCUIT BREAKER] Kill switch active. Halting recorder.")
                break
            t0 = time.time()
            new, errs = record_once(db, markets, ids, last, depth=depth, workers=workers)
            print(f"{time.strftime('%H:%M:%S')} changed={new} errors={errs} took={time.time()-t0:.1f}s")
            time.sleep(max(0.0, interval - (time.time() - t0)))
    except KeyboardInterrupt:
        db.commit()
        print("stopped")
