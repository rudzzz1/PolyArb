"""Replay recorded books through basket logic with realistic execution simulation.

Production-grade features:
- Disconnect gap handling (stale quotes across websocket/network drops invalidated)
- Clock skew & phantom-arb filter (requires multi-tick persistence or close timestamps)
- Leg-by-leg execution simulation with execution delay, slippage, and unwind losses
- Gross vs Net fee breakdown (exchange taker fees + gas/amortization)
- Annualized return (APY) metrics based on days to resolution
- Missing/closed market safety (no KeyError)
"""
from __future__ import annotations
import sqlite3
from bisect import bisect_right
from datetime import datetime
from statistics import median
from typing import Sequence, Any

from .core import best_basket, sweep, sweep_with_limits, Level, annualized_return, unwind_loss_estimate
from .recorder import dec
from . import venues


def _parse_ts(s: str | None) -> float | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.timestamp()
    except Exception:
        return None


class Timeline:
    def __init__(self, db: sqlite3.Connection):
        self.t: dict[str, list[float]] = {}
        self.s: dict[str, list[tuple[str, str]]] = {}
        for mid, ts, y, n in db.execute("SELECT market_id, ts, yes, no FROM snaps ORDER BY ts"):
            self.t.setdefault(mid, []).append(ts)
            self.s.setdefault(mid, []).append((y, n))

        # Load network disconnect/connect events to invalidate stale books across gaps
        self.gaps: list[tuple[float, float]] = []
        try:
            evs = db.execute("SELECT ts, kind FROM events ORDER BY ts").fetchall()
            disc_ts = None
            for ts, kind in evs:
                if kind == "disconnect" and disc_ts is None:
                    disc_ts = ts
                elif kind == "connect" and disc_ts is not None:
                    self.gaps.append((disc_ts, ts))
                    disc_ts = None
            if disc_ts is not None:
                self.gaps.append((disc_ts, float("inf")))
        except sqlite3.OperationalError:
            pass  # events table does not exist or empty

    def is_in_gap(self, t: float) -> bool:
        for start, end in self.gaps:
            if start <= t <= end:
                return True
        return False

    def asof(self, mid: str, t: float, max_stale_seconds: float = 60.0) -> dict[str, list[Level]] | None:
        if self.is_in_gap(t):
            return None
        ts = self.t.get(mid)
        if not ts:
            return None
        i = bisect_right(ts, t) - 1
        if i < 0:
            return None
        # Discard quote if too stale or separated by a disconnect gap
        quote_ts = ts[i]
        if (t - quote_ts) > max_stale_seconds:
            return None
        for g_start, g_end in self.gaps:
            if quote_ts < g_start <= t:
                return None  # quote was recorded before a disconnect that occurred before t

        y, n = self.s[mid][i]
        return {"YES": dec(y), "NO": dec(n)}

    def asof_ts(self, mid: str, t: float) -> float | None:
        """Returns the actual timestamp the quote was recorded."""
        ts = self.t.get(mid)
        if not ts:
            return None
        i = bisect_right(ts, t) - 1
        return ts[i] if i >= 0 else None


def _books(rel: dict, tl: Timeline, t: float) -> list[dict[str, list[Level]]] | None:
    books = []
    for mid in rel["markets"]:
        b = tl.asof(mid, t)
        if b is None:
            return None
        books.append(b)
    return books


def _evaluate(rel: dict, markets: dict, tl: Timeline, t: float, fee_bps: float,
              min_edge: float, min_shares: float, max_clock_skew: float = 3.0,
              max_anomaly_edge: float = 0.35, min_apy: float = 0.0) -> dict | None:
    # Safe lookup: skip relations with pruned/missing markets
    for mid in rel["markets"]:
        if mid not in markets:
            return None

    # Clock skew check: quotes must have been sampled within max_clock_skew
    leg_timestamps = [tl.asof_ts(mid, t) for mid in rel["markets"]]
    if any(ts is None for ts in leg_timestamps):
        return None
    if max(leg_timestamps) - min(leg_timestamps) > max_clock_skew:
        return None  # reject phantom arb caused by unsynchronized observations

    books = _books(rel, tl, t)
    if books is None:
        return None

    fees = [venues.fee_fn(markets[i]) for i in rel["markets"]]
    asks = [{s: (b[s][0].price if b[s] else None) for s in b} for b in books]

    # Calculate days to resolution
    end_times = [_parse_ts(markets[mid].get("end")) for mid in rel["markets"]]
    valid_ends = [et for et in end_times if et is not None and et > t]
    days_to_res = min((et - t) / 86400.0 for et in valid_ends) if valid_ends else None

    bk = best_basket(rel["kind"], asks, fee_bps, fees, days_to_resolution=days_to_res)
    if not bk or bk.edge < min_edge:
        return None

    # Circuit breaker: abnormally large edges are almost always bad books or rule mismatches
    if bk.edge > max_anomaly_edge:
        return None

    # Annualized return gate
    if min_apy > 0 and days_to_res is not None:
        if bk.annualized_return < min_apy:
            return None

    lf = [fees[i] for i, _ in bk.legs]
    sh, c = sweep([books[i][s] for i, s in bk.legs], bk.payoff, fee_bps, lf)
    if sh < min_shares:
        return None

    # Calculate gross profit vs fee breakdown
    gross_cost = sum(asks[i][s] for i, s in bk.legs) * sh
    gross_profit = sh * bk.payoff - gross_cost
    fees_paid = c - gross_cost
    net_profit = sh * bk.payoff - c

    return {
        "bk": bk,
        "shares": sh,
        "profit": net_profit,
        "gross_profit": gross_profit,
        "fees_paid": fees_paid,
        "days_to_res": days_to_res,
        "apy": bk.annualized_return if days_to_res is not None else 0.0,
    }


def _simulate_legging(rel: dict, markets: dict, tl: Timeline, t: float, bk: Any,
                      shares: float, exec_delay: float = 0.25, fee_bps: float = 0.0) -> dict:
    """Simulates realistic leg-by-leg submission:
    Leg 0 is filled at time t.
    Subsequent legs are filled at time t + exec_delay.
    Checks whether subsequent legs are still available and measures slippage / unwind loss."""
    if len(bk.legs) <= 1:
        return {"filled": True, "slippage": 0.0, "net_pnl": bk.edge * shares}

    # Leg 0 price
    mid0, s0 = bk.legs[0]
    b0 = tl.asof(markets[rel["markets"][mid0]]["id"], t)

    # Check other legs at t + exec_delay
    t_second = t + exec_delay
    all_filled = True
    total_cost = 0.0
    fees = [venues.fee_fn(markets[i]) for i in rel["markets"]]

    for leg_idx, (m_idx, side) in enumerate(bk.legs):
        mid = rel["markets"][m_idx]
        eval_t = t if leg_idx == 0 else t_second
        book = tl.asof(mid, eval_t)
        if book is None or not book[side]:
            all_filled = False
            break
        # Check ask price
        p = book[side][0].price
        f = fees[m_idx]
        cost_per_sh = p * (1 + fee_bps / 1e4) + (f(p) if f else 0.0)
        total_cost += cost_per_sh * shares

    if all_filled and (shares * bk.payoff - total_cost) > 0:
        actual_profit = shares * bk.payoff - total_cost
        return {"filled": True, "slippage": max(0.0, (bk.cost * shares) - total_cost), "net_pnl": actual_profit}
    else:
        # Legging failed: leg 0 must be unwound at best bid
        unwind_loss = unwind_loss_estimate(b0[s0][0].price if b0 and b0[s0] else 0.50)
        loss = unwind_loss * shares
        return {"filled": False, "slippage": 0.0, "net_pnl": -loss}


def _same_legs_profit(rel: dict, markets: dict, tl: Timeline, t: float, bk: Any, fee_bps: float) -> float:
    for mid in rel["markets"]:
        if mid not in markets:
            return 0.0
    books = _books(rel, tl, t)
    if books is None:
        return 0.0
    fees = [venues.fee_fn(markets[i]) for i in rel["markets"]]
    sh, c = sweep([books[i][s] for i, s in bk.legs], bk.payoff, fee_bps,
                  [fees[i] for i, _ in bk.legs])
    return sh * bk.payoff - c


def replay(db: sqlite3.Connection, relations: Sequence[dict], markets: dict,
           horizons: tuple[int, ...] = (5, 15, 60), fee_bps: float = 0.0,
           min_edge: float = 0.005, min_shares: float = 5.0,
           filter_phantom: bool = True, exec_delay: float = 0.25,
           min_apy: float = 0.0) -> list[dict]:
    tl, episodes = Timeline(db), []
    for rel in relations:
        # Pruning check
        if any(mid not in markets for mid in rel["markets"]):
            continue

        times = sorted({t for i in rel["markets"] for t in tl.t.get(i, [])})
        cur = None
        consecutive_ticks = 0

        for t in times:
            res = _evaluate(rel, markets, tl, t, fee_bps, min_edge, min_shares, min_apy=min_apy)
            if res:
                consecutive_ticks += 1
                if cur is None:
                    # Phantom arb filter: candidate start
                    cur = {
                        "relation": rel["id"],
                        "kind": rel["kind"],
                        "start": t,
                        "profit0": res["profit"],
                        "gross_profit0": res["gross_profit"],
                        "fees_paid0": res["fees_paid"],
                        "shares0": res["shares"],
                        "legs": res["bk"].legs,
                        "bk": res["bk"],
                        "days_to_res": res["days_to_res"],
                        "apy": res["apy"],
                        "obs_count": 1,
                    }
                else:
                    cur["obs_count"] += 1
            elif not res and cur is not None:
                cur["duration"], cur["censored"] = t - cur["start"], False
                # If filter_phantom is on, require >= 2 observations or duration > 0.5s
                if not filter_phantom or cur["obs_count"] >= 2 or cur["duration"] >= 0.5:
                    episodes.append(cur)
                cur = None
                consecutive_ticks = 0

        if cur is not None and len(times) > 0:
            cur["duration"], cur["censored"] = (times[-1] - cur["start"]), True
            if not filter_phantom or cur["obs_count"] >= 2 or cur["duration"] >= 0.5:
                episodes.append(cur)

    # Simulate horizons and execution
    for e in episodes:
        rel = rel_by(relations, e["relation"])
        e["after"] = {h: _same_legs_profit(rel, markets, tl, e["start"] + h, e["bk"], fee_bps)
                      for h in horizons}
        # Simulate legging
        e["legging"] = _simulate_legging(rel, markets, tl, e["start"], e["bk"],
                                         e["shares0"], exec_delay=exec_delay, fee_bps=fee_bps)

    return episodes


def rel_by(relations: Sequence[dict], rid: str) -> dict:
    return next(r for r in relations if r["id"] == rid)


def summarize(episodes: Sequence[dict], horizons: tuple[int, ...] = (5, 15, 60)) -> dict[str, Any]:
    if not episodes:
        return {"episodes": 0}
    done = [e["duration"] for e in episodes if not e["censored"]]
    gross_total = sum(e.get("gross_profit0", e["profit0"]) for e in episodes)
    net_total = sum(e["profit0"] for e in episodes)
    fees_total = sum(e.get("fees_paid0", 0.0) for e in episodes)

    legging_fills = sum(1 for e in episodes if e.get("legging", {}).get("filled"))
    legging_pnl = sum(e.get("legging", {}).get("net_pnl", 0.0) for e in episodes)

    apys = [e["apy"] for e in episodes if e.get("apy", 0.0) > 0]

    out: dict[str, Any] = {
        "episodes": len(episodes),
        "median_duration_s": round(median(done), 2) if done else None,
        "gross_profit": round(gross_total, 2),
        "fees_consumed": round(fees_total, 2),
        "net_profit_at_detection": round(net_total, 2),
        "median_annualized_return_pct": f"{median(apys) * 100:.1f}%" if apys else "N/A",
        "simulated_legging_fill_rate": f"{legging_fills}/{len(episodes)} ({(legging_fills / len(episodes))*100:.1f}%)",
        "simulated_legging_net_pnl": round(legging_pnl, 2),
    }
    for h in horizons:
        alive = [e for e in episodes if e["after"][h] > 0]
        out[f"alive_after_{h}s"] = f"{len(alive)}/{len(episodes)}"
        out[f"profit_after_{h}s"] = round(sum(max(e["after"][h], 0) for e in episodes), 2)
    return out
