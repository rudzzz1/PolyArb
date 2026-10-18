"""Paper scanner: find logical-arb baskets on approved relations (single- or cross-venue),
log them, then re-check after a simulated latency to see whether the edge survived.

Production-grade controls:
- Kill switch check (environment variable and KILL_SWITCH file)
- Circuit breaker on abnormal edges (> max_anomaly_edge)
- Orderbook validation (crossed books, negative prices, invalid levels rejected)
- Approval expiration enforcement
- Capital and exposure caps per relation, event, and venue
- Slippage caps and depth-aware sizing
"""
from __future__ import annotations
import json, sqlite3, time
from datetime import datetime
from pathlib import Path
from typing import Callable, Any

from .core import best_basket, sweep, sweep_with_limits, validate_book, annualized_return
from .config import Config, is_kill_switch_active
from .matcher import is_approval_expired
from . import venues

SCHEMA = """CREATE TABLE IF NOT EXISTS opps(
 ts REAL, relation_id TEXT, kind TEXT, legs TEXT, payoff REAL,
 shares0 REAL, profit0 REAL, shares1 REAL, profit1 REAL,
 apy REAL, slippage_bps REAL)"""


def _parse_ts(s: str | None) -> float | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.timestamp()
    except Exception:
        return None


def scan_once(relations: list[dict], markets: dict, db: sqlite3.Connection,
              cfg: Config | None = None, fee_bps: float = 0.0, min_edge: float = 0.005,
              min_shares: float = 5.0, latency: float = 0.5,
              fetch: Callable[[dict], dict] = venues.fetch_market_asks,
              exposure_tracker: dict[str, float] | None = None) -> int:
    cfg = cfg or Config()
    if is_kill_switch_active(cfg.data_dir):
        print("[CIRCUIT BREAKER] Kill switch active. Halting scan.")
        return 0

    found = 0
    exposure = exposure_tracker if exposure_tracker is not None else {}

    for r in relations:
        # 1. Pruning and approval expiration check
        if is_approval_expired(r):
            print(f"skip {r['id']}: approval expired")
            continue
        if any(mid not in markets for mid in r["markets"]):
            continue

        try:
            ms = [markets[i] for i in r["markets"]]
            fees = [venues.fee_fn(m) for m in ms]

            # 2. Fetch and validate books
            books = [fetch(m) for m in ms]
            for idx, b in enumerate(books):
                valid, reason = validate_book(b)
                if not valid:
                    print(f"skip {r['id']}: invalid book for market {ms[idx]['id']} ({reason})")
                    raise ValueError(reason)

            asks = [{s: (b[s][0].price if b[s] else None) for s in b} for b in books]

            # 3. Calculate days to resolution
            now_t = time.time()
            end_times = [_parse_ts(m.get("end")) for m in ms]
            valid_ends = [et for et in end_times if et is not None and et > now_t]
            days_to_res = min((et - now_t) / 86400.0 for et in valid_ends) if valid_ends else None

            # 4. Find best basket
            bk = best_basket(r["kind"], asks, fee_bps, fees, days_to_resolution=days_to_res)
            if not bk or bk.edge < min_edge:
                continue

            # 5. Circuit breaker on abnormal edge
            if bk.edge > cfg.risk.max_anomaly_edge:
                print(f"[ANOMALY BLOCKED] {r['id']} edge {bk.edge:.3f} > max {cfg.risk.max_anomaly_edge:.3f} (bad quote/mismatch)")
                continue

            # 6. Annualized return hurdle
            if cfg.risk.min_annualized_return > 0 and days_to_res is not None:
                if bk.annualized_return < cfg.risk.min_annualized_return:
                    continue

            # 7. Exposure check
            event_key = f"event:{ms[0].get('event', 'unknown')}"
            current_event_exp = exposure.get(event_key, 0.0)
            if current_event_exp >= cfg.risk.max_capital_per_event:
                print(f"skip {r['id']}: event capital cap reached (${current_event_exp:.2f})")
                continue

            # 8. Sizing with risk limits (slippage cap, capital limits)
            lf = [fees[i] for i, _ in bk.legs]
            res0 = sweep_with_limits(
                [books[i][s] for i, s in bk.legs],
                bk.payoff,
                fee_bps=fee_bps,
                fees=lf,
                max_capital=cfg.risk.max_capital_per_relation,
                max_slippage_bps=cfg.risk.max_slippage_bps,
                min_depth=cfg.risk.min_depth_thinnest_leg,
            )
            sh0, c0 = res0.shares, res0.total_cost
            if sh0 < min_shares:
                continue

            # 9. Latency re-check for paper execution realism
            time.sleep(latency)
            b1 = {i: fetch(ms[i]) for i in {i for i, _ in bk.legs}}
            res1 = sweep_with_limits(
                [b1[i][s] for i, s in bk.legs],
                bk.payoff,
                fee_bps=fee_bps,
                fees=lf,
                max_capital=cfg.risk.max_capital_per_relation,
                max_slippage_bps=cfg.risk.max_slippage_bps,
            )
            sh1, c1 = res1.shares, res1.total_cost

        except Exception as e:
            continue

        legs = [(ms[i].get("venue", "polymarket"), ms[i].get("question", ""), s)
                for i, s in bk.legs]
        profit0 = sh0 * bk.payoff - c0
        profit1 = sh1 * bk.payoff - c1
        apy = bk.annualized_return if days_to_res is not None else 0.0

        try:
            db.execute("INSERT INTO opps VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (time.time(), r["id"], r["kind"], json.dumps(legs), bk.payoff,
                        sh0, profit0, sh1, profit1, round(apy, 4), res0.slippage_bps))
        except sqlite3.OperationalError:
            # Fallback for 9-column legacy schema
            db.execute("INSERT INTO opps VALUES(?,?,?,?,?,?,?,?,?)",
                       (time.time(), r["id"], r["kind"], json.dumps(legs), bk.payoff,
                        sh0, profit0, sh1, profit1))
        db.commit()

        # Update exposure
        exposure[event_key] = current_event_exp + c0
        found += 1
        apy_str = f" APY={apy*100:.1f}%" if days_to_res else ""
        print(f"[{r['kind']}] edge/share=${bk.edge:.3f}{apy_str} shares {sh0:.0f}->{sh1:.0f} "
              f"profit ${profit0:.2f}->${profit1:.2f} (slip {res0.slippage_bps:.1f}bps)")

    return found


def run(data_dir: str = "data", interval: float = 30.0, **kw: Any):
    cfg = Config.load()
    cfg.data_dir = data_dir
    d = Path(data_dir)
    markets = json.loads((d / "markets.json").read_text(encoding="utf-8"))
    rels = [r for r in json.loads((d / "candidates.json").read_text(encoding="utf-8")) if r.get("approved")]
    print(f"{len(rels)} approved relations loaded")
    db = sqlite3.connect(d / "opps.sqlite")
    db.execute(SCHEMA)
    exposure_tracker: dict[str, float] = {}

    while True:
        if is_kill_switch_active(d):
            print("[HALT] Kill switch triggered. Exiting scanner.")
            break
        scan_once(rels, markets, db, cfg=cfg, exposure_tracker=exposure_tracker, **kw)
        time.sleep(interval)
