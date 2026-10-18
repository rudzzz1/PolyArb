"""Streaming recorder.
 - Polymarket: public CLOB market websocket (event-driven; no auth). Books are rebuilt
   from `book` snapshots + `price_change` deltas, flushed every `flush` seconds.
 - Kalshi: REST polling in a worker thread (its websocket needs an API key + signing).
Writes the same snaps table as the polling recorder, so replay works unchanged.
Needs: pip install websockets"""
from __future__ import annotations
import asyncio, json, sqlite3, time
from pathlib import Path
from typing import Any

from .core import Level
from .config import is_kill_switch_active
from . import recorder

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


class TokenBook:
    def __init__(self):
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}

    def apply_book(self, ev: dict):
        side = lambda xs: {float(x["price"]): float(x["size"]) for x in xs or [] if float(x["size"]) > 0}
        self.bids, self.asks = side(ev.get("bids")), side(ev.get("asks"))

    def apply_change(self, price: float, size: float, side: str):
        d = self.bids if str(side).upper() == "BUY" else self.asks
        if size <= 0:
            d.pop(price, None)
        else:
            d[price] = size

    def ask_ladder(self) -> list[Level]:
        return [Level(p, s) for p, s in sorted(self.asks.items())]

    def bid_ladder(self) -> list[Level]:
        return [Level(p, s) for p, s in sorted(self.bids.items(), reverse=True)]


class Router:
    """Pure state machine: websocket events in, dirty market ids out. (Unit-tested.)"""
    def __init__(self, markets: dict):
        self.markets, self.books, self.tok2mid = markets, {}, {}
        for mid, m in markets.items():
            self.tok2mid[m["yes"]] = mid
            self.tok2mid[m["no"]] = mid

    def handle(self, ev: dict) -> set[str]:
        dirty = set()
        et = ev.get("event_type")
        if et == "book" and ev.get("asset_id") in self.tok2mid:
            self.books.setdefault(ev["asset_id"], TokenBook()).apply_book(ev)
            dirty.add(ev["asset_id"])
        elif et == "price_change":
            for ch in ev.get("price_changes") or ev.get("changes") or []:
                tok = ch.get("asset_id") or ev.get("asset_id")
                if tok in self.books:                       # ignore until we hold a snapshot
                    self.books[tok].apply_change(float(ch["price"]), float(ch["size"]), ch["side"])
                    dirty.add(tok)
        return {self.tok2mid[t] for t in dirty}

    def handle_raw(self, data: Any) -> set[str]:
        out = set()
        for ev in (data if isinstance(data, list) else [data]):
            if isinstance(ev, dict):
                out |= self.handle(ev)
        return out

    def snapshot(self, mid: str) -> dict[str, list[Level]] | None:
        m = self.markets.get(mid)
        if not m:
            return None
        y, n = self.books.get(m["yes"]), self.books.get(m["no"])
        if y is None or n is None:
            return None
        return {"YES": y.ask_ladder(), "NO": n.ask_ladder()}


async def _ping(ws):
    while True:
        await asyncio.sleep(10)
        await ws.send("PING")


async def _ws_conn(name: str, tokens: list[str], router: Router, dirty: set[str],
                   db: sqlite3.Connection, data_dir: str = "data"):
    import websockets
    backoff = 1
    while True:
        if is_kill_switch_active(data_dir):
            print(f"[{name}] Kill switch active. Halting websocket.")
            break
        try:
            async with websockets.connect(WS_URL, ping_interval=None, max_size=None) as ws:
                await ws.send(json.dumps({"assets_ids": tokens, "type": "market"}))
                db.execute("INSERT INTO events VALUES(?,?,?)", (time.time(), "connect", name))
                backoff, pinger = 1, asyncio.create_task(_ping(ws))
                try:
                    async for raw in ws:
                        if is_kill_switch_active(data_dir):
                            break
                        if raw == "PONG":
                            continue
                        try:
                            data = json.loads(raw)
                        except ValueError:
                            continue
                        dirty.update(router.handle_raw(data))
                finally:
                    pinger.cancel()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[{name}] disconnected: {e!r}")
        db.execute("INSERT INTO events VALUES(?,?,?)", (time.time(), "disconnect", name))
        db.commit()
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30)


async def _flusher(router: Router, dirty: set[str], db: sqlite3.Connection,
                   last: dict, depth: int, flush: float, data_dir: str = "data"):
    written, t0 = 0, time.time()
    while True:
        if is_kill_switch_active(data_dir):
            break
        await asyncio.sleep(flush)
        if dirty:
            batch = list(dirty)
            dirty.clear()
            ts = time.time()
            for mid in batch:
                snap = router.snapshot(mid)
                if snap and recorder.store(db, last, mid, ts, snap, depth):
                    written += 1
            db.commit()
        if time.time() - t0 >= 10:
            print(f"{time.strftime('%H:%M:%S')} polymarket rows written (last 10s): {written}")
            written, t0 = 0, time.time()


def _poll_kalshi(db_path: Path, markets: dict, ids: list[str], last: dict, depth: int, workers: int):
    db = sqlite3.connect(db_path, timeout=30)
    try:
        return recorder.record_once(db, markets, ids, last, depth=depth, workers=workers)
    finally:
        db.close()


async def _kalshi_loop(db_path: Path, markets: dict, ids: list[str], depth: int,
                       interval: float, workers: int, data_dir: str = "data"):
    db = sqlite3.connect(db_path, timeout=30)
    last = recorder.load_last(db)
    db.close()
    while True:
        if is_kill_switch_active(data_dir):
            print("[kalshi_loop] Kill switch active. Halting loop.")
            break
        new, errs = await asyncio.to_thread(_poll_kalshi, db_path, markets, ids, last, depth, workers)
        print(f"{time.strftime('%H:%M:%S')} kalshi changed={new} errors={errs}")
        await asyncio.sleep(interval)


async def _main(markets: dict, poly_ids: list[str], kal_ids: list[str], db_path: Path,
                depth: int, flush: float, chunk: int, kal_interval: float, workers: int,
                data_dir: str = "data"):
    db = sqlite3.connect(db_path, timeout=30)
    db.executescript(recorder.SCHEMA)
    db.execute("PRAGMA journal_mode=WAL")
    last, dirty = recorder.load_last(db), set()
    router = Router({i: markets[i] for i in poly_ids if i in markets})
    tasks = [asyncio.create_task(_flusher(router, dirty, db, last, depth, flush, data_dir))]
    for k in range(0, len(poly_ids), chunk):
        part = poly_ids[k:k + chunk]
        toks = [t for i in part if i in markets for t in (markets[i]["yes"], markets[i]["no"])]
        if toks:
            tasks.append(asyncio.create_task(_ws_conn(f"ws{k // chunk}", toks, router, dirty, db, data_dir)))
    if kal_ids:
        tasks.append(asyncio.create_task(_kalshi_loop(db_path, markets, kal_ids, depth, kal_interval, workers, data_dir)))
    try:
        await asyncio.gather(*tasks)
    finally:
        db.commit()


def run(data_dir: str = "data", depth: int = 10, flush: float = 0.1, chunk: int = 100,
        kalshi_interval: float = 5.0, workers: int = 8, all_candidates: bool = False):
    d = Path(data_dir)
    markets = json.loads((d / "markets.json").read_text(encoding="utf-8"))
    rels = json.loads((d / "candidates.json").read_text(encoding="utf-8"))
    if not all_candidates:
        rels = [r for r in rels if r.get("approved")]
    ids = recorder.tracked_ids(rels)
    poly = [i for i in ids if markets.get(i, {}).get("venue", "polymarket") == "polymarket"]
    kal = [i for i in ids if markets.get(i, {}).get("venue") == "kalshi"]
    print(f"streaming {len(poly)} Polymarket markets (websocket), polling {len(kal)} Kalshi markets")
    try:
        asyncio.run(_main(markets, poly, kal, d / "books.sqlite", depth, flush, chunk,
                          kalshi_interval, workers, data_dir))
    except KeyboardInterrupt:
        print("stopped")
