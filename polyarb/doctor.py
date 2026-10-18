"""First-live-run checker: hits the real APIs and verifies every field name/format the
code relies on. Run it, and paste the output back if anything FAILs.
    python -m polyarb doctor"""
from __future__ import annotations
import asyncio, json, os, time

from . import gamma, kalshi
from .gamma import CLOB, GAMMA, http_get
from .stream import WS_URL


def _s(x, n=260):
    t = json.dumps(x, default=str)
    return t if len(t) <= n else t[:n] + "..."


def check_gamma():
    evs = http_get(f"{GAMMA}/events", {"active": "true", "closed": "false", "limit": 20})
    if not evs:
        return False, "no events returned", None
    ms = gamma.load_markets(evs)
    if not ms:
        sample = (evs[0].get("markets") or [{}])[0]
        return False, f"0 binary markets parsed. sample market keys: {sorted(sample)[:40]}", None
    m = next(iter(ms.values()))
    raw = (evs[0].get("markets") or [{}])[0]
    return True, (f"{len(ms)} markets from {len(evs)} events. event has negRisk: {'negRisk' in evs[0]}; "
                  f"market has endDate: {'endDate' in raw}; sample: {_s(m)}"), m


def check_clob(m):
    d = http_get(f"{CLOB}/book", {"token_id": m["yes"]})
    asks, bids = d.get("asks"), d.get("bids")
    if asks is None or bids is None:
        return False, f"book keys: {sorted(d)}"
    if not asks:
        return True, "book reachable but no asks on this token (try again); keys: " + str(sorted(d))
    lv = gamma.fetch_asks(m["yes"])
    asc = all(a.price <= b.price for a, b in zip(lv, lv[1:]))
    return True, (f"keys {sorted(d)}; raw ask[0]={_s(asks[0])}, raw ask[-1]={_s(asks[-1])}; "
                  f"parsed ladder ascending={asc}, best ask={lv[0].price}")


def check_kalshi():
    d = http_get(f"{kalshi.BASE}/events", {"status": "open", "with_nested_markets": "true", "limit": 10})
    evs = d.get("events", [])
    ms = kalshi.load_markets(evs)
    if not ms:
        return False, f"0 markets parsed. response keys: {sorted(d)}; sample event keys: {sorted(evs[0]) if evs else None}"
    raw = (evs[0].get("markets") or [{}])[0]
    need = [k for k in ("ticker", "title", "yes_sub_title", "close_time", "status") if k not in raw]
    for m in list(ms.values())[:8]:
        ob = http_get(f"{kalshi.BASE}/markets/{m['ticker']}/orderbook")
        fp = ob.get("orderbook_fp")
        if fp is None:
            return False, f"no 'orderbook_fp' in orderbook response; keys: {sorted(ob)}"
        if fp.get("yes_dollars") or fp.get("no_dollars"):
            a = kalshi.asks_from_bids(kalshi._levels(fp.get("yes_dollars")), kalshi._levels(fp.get("no_dollars")))
            prices = [l.price for side in a.values() for l in side]
            ok = all(0 <= p <= 1 for p in prices)
            return ok, (f"{len(ms)} markets; market fields missing: {need or 'none'}; "
                        f"raw yes_dollars[0]={_s((fp.get('yes_dollars') or [None])[0])}; "
                        f"derived prices in [0,1]: {ok}")
    return True, f"{len(ms)} markets parsed, but first 8 had empty books; fields missing: {need or 'none'}"


async def _ws(token):
    import websockets
    async with websockets.connect(WS_URL, ping_interval=None) as ws:
        await ws.send(json.dumps({"assets_ids": [token], "type": "market"}))
        t0 = time.time()
        while time.time() - t0 < 10:
            raw = await asyncio.wait_for(ws.recv(), 10)
            if raw == "PONG":
                continue
            data = json.loads(raw)
            for ev in (data if isinstance(data, list) else [data]):
                if isinstance(ev, dict) and ev.get("event_type") == "book":
                    return True, (f"book event keys {sorted(ev)}; levels: {len(ev.get('bids', []))} bids / "
                                  f"{len(ev.get('asks', []))} asks; ask[0]={_s((ev.get('asks') or [None])[0])}")
        return False, "no 'book' event within 10s"


def main():
    results, m = [], None
    steps = [("Polymarket Gamma", check_gamma), ("Polymarket CLOB book", None),
             ("Kalshi events + orderbook", check_kalshi), ("Polymarket websocket", None)]
    has_network_block = False

    for name, fn in steps:
        try:
            if name == "Polymarket CLOB book":
                ok, msg = check_clob(m) if m else (False, "skipped (Gamma failed)")
            elif name == "Polymarket websocket":
                try:
                    import websockets  # noqa: F401
                except ImportError:
                    ok, msg = False, "skipped: pip install websockets"
                else:
                    ok, msg = asyncio.run(_ws(m["yes"])) if m else (False, "skipped (Gamma failed)")
            elif name == "Polymarket Gamma":
                ok, msg, m = fn()
            else:
                ok, msg = fn()
        except Exception as e:
            err_str = repr(e)
            if "10054" in err_str or "reset" in err_str.lower():
                has_network_block = True
                msg = f"{err_str}\n  -> Connection reset by remote host/ISP. Prediction market APIs are blocked on some regional networks. Set HTTP_PROXY/HTTPS_PROXY or use a VPN."
            else:
                msg = f"exception: {err_str}"
            ok = False
        results.append(ok)
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {msg}\n")

    print(f"{sum(results)}/{len(results)} checks passed")
    if has_network_block:
        print("[NOTE] Regional network blocking detected. Configure HTTP_PROXY or HTTPS_PROXY environment variables to route traffic through a proxy/VPN.")
    return 0 if all(results) else 1
