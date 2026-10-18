"""Live 5-Second Market Poller for Cross-Market Arbitrage & Mispricing Detection.
Polls Polymarket Gamma/CLOB and Kalshi APIs every 5 seconds to identify real candidate trade legs,
evaluates live orderbooks with mathematical best_basket search and full risk controls,
books verified positive-edge trades, integrates execution routing and immutable audit logging,
tracks contract expiry, and monitors for settlement.
"""
from __future__ import annotations
import asyncio
import json
import math
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any

from . import gamma, kalshi, matcher, venues
from .config import Config, is_kill_switch_active
from .core import best_basket, sweep_with_limits, validate_book
from .matcher import parse_datetime, is_approval_expired
from .simulator import PortfolioManager, INR_PER_USD, format_expiry
from .audit import AuditLog
from .executor import ExecutionRouter, OrderRequest
from .logger import get_logger, set_trace_id

logger = get_logger("poller")


class LiveMarketPoller:
    def __init__(self, portfolio: PortfolioManager, interval_sec: float = 5.0,
                 cfg: Config | None = None, audit_log: AuditLog | None = None,
                 router: ExecutionRouter | None = None):
        self.portfolio = portfolio
        self.interval_sec = interval_sec
        self.cfg = cfg or Config()
        self.audit_log = audit_log or AuditLog(db_path=self.portfolio.data_dir / "audit.sqlite")
        self.router = router or ExecutionRouter(audit_log=self.audit_log, dry_run=True)

        self.running = False
        self._thread: threading.Thread | None = None

        self.last_poll_time: str = "Never"
        self.poll_count: int = 0
        self.candidates_identified: int = 0
        self.trades_booked: int = 0
        self.last_status: str = "Initialized"
        self.active_sources: list[str] = ["Polymarket (Gamma + CLOB)", "Kalshi (REST)"]
        self.booked_relations: set[str] = set()

        # Seed already booked relations from portfolio
        for s in self.portfolio.open_structures:
            self.booked_relations.add(s.relation_id)

    def start(self):
        if self.running:
            return
        self.running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="LiveMarketPoller")
        self._thread.start()
        logger.info(f"Started 5-second live poller for Polymarket & Kalshi with full risk controls.")

    def stop(self):
        self.running = False

    def _run_loop(self):
        time.sleep(1.0)
        while self.running:
            try:
                self.poll_once()
            except Exception as e:
                self.last_status = f"Error during poll: {str(e)[:80]}"
                logger.error(f"Poller loop error: {e}", exc_info=True)
            time.sleep(self.interval_sec)

    def poll_once(self) -> dict[str, Any]:
        """Executes a single poll tick across Polymarket and Kalshi."""
        self.poll_count += 1
        now_dt = datetime.now(timezone.utc)
        self.last_poll_time = now_dt.strftime("%H:%M:%S UTC")
        set_trace_id(f"poll_{self.poll_count}")

        # Check Kill Switch (Phase 0.2)
        if is_kill_switch_active(self.portfolio.data_dir):
            self.last_status = "Paused (Kill switch active)"
            return {"status": "paused", "reason": "kill_switch"}

        # 1. Fetch live markets from Polymarket
        poly_markets = {}
        try:
            poly_ev = gamma.fetch_events(limit=40, max_pages=1)
            poly_markets = gamma.load_markets(poly_ev)
        except Exception as e:
            logger.warning(f"Polymarket fetch warning: {e}")

        # 2. Fetch live markets from Kalshi
        kalshi_markets = {}
        try:
            kalshi_ev = kalshi.fetch_events(max_pages=1)
            kalshi_markets = kalshi.load_markets(kalshi_ev)
        except Exception as e:
            logger.warning(f"Kalshi fetch warning: {e}")

        total_live_mkts = len(poly_markets) + len(kalshi_markets)
        if total_live_mkts == 0:
            self.last_status = "No live markets retrieved (retrying next tick)"
            return {"status": "no_markets"}

        # 3. Find candidate relations
        cands = []
        if poly_markets:
            cands.extend(gamma.candidates(poly_markets))

        all_mkts = {**poly_markets, **kalshi_markets}
        if len(all_mkts) > 1:
            try:
                threshold_ladders = matcher.find_threshold_ladders(all_mkts)
                cands.extend(threshold_ladders)
            except Exception as e:
                logger.warning(f"Threshold ladder matcher warning: {e}")

        if poly_markets and kalshi_markets:
            try:
                cross_matches = matcher.match(poly_markets, kalshi_markets, max_days=3, min_score=0.45)
                cands.extend(cross_matches)
            except Exception as e:
                logger.warning(f"Cross-venue matcher warning: {e}")

        self.candidates_identified += len(cands)

        # 4. Evaluate candidates for price mismatch with mathematical best_basket search
        booked_this_tick = 0
        for cand in cands:
            rel_id = cand["id"]
            if rel_id in self.booked_relations:
                continue

            # Need at least minimum capital for a trade (₹200 INR ≈ $2.40 USD)
            if self.portfolio.available_cash_inr < 200.0:
                break

            mismatch = self._evaluate_and_book_candidate(cand, all_mkts)
            if mismatch:
                self.booked_relations.add(rel_id)
                self.trades_booked += 1
                booked_this_tick += 1
                if booked_this_tick >= 2:
                    break

        # 5. Check auto-settlement for expired structures
        self._check_natural_expiry_settlements(now_dt)

        self.last_status = f"Live active. Markets: {len(poly_markets)} Poly / {len(kalshi_markets)} Kalshi. Booked: {self.trades_booked} trades."
        return {
            "status": "ok",
            "poll_count": self.poll_count,
            "poly_count": len(poly_markets),
            "kalshi_count": len(kalshi_markets),
            "candidates_found": len(cands),
            "booked_this_tick": booked_this_tick,
            "total_booked": self.trades_booked,
        }

    def _evaluate_and_book_candidate(self, cand: dict, all_markets: dict) -> bool:
        """Evaluates opportunity using exact mathematical core.best_basket solver and risk checks."""
        kind = cand["kind"]
        mkt_ids = cand["markets"]

        if len(mkt_ids) < 2:
            return False

        ms = [all_markets.get(mid) for mid in mkt_ids]
        if any(m is None for m in ms):
            return False

        # 1. Approval expiration check
        if is_approval_expired(cand):
            logger.info(f"Skipping candidate {cand['id']}: approval expired")
            return False

        # 2. Fetch and validate live orderbooks
        try:
            books = [venues.fetch_market_asks(m) for m in ms]
        except Exception as e:
            return False

        for idx, b in enumerate(books):
            valid, reason = validate_book(b)
            if not valid:
                logger.info(f"Candidate {cand['id']} book invalid for {ms[idx]['id']}: {reason}")
                return False

        asks = [{s: (b[s][0].price if b.get(s) else None) for s in ("YES", "NO")} for b in books]
        fees = [venues.fee_fn(m, polymarket_fee_bps=self.cfg.fees.polymarket_taker_bps) for m in ms]

        # 3. Calculate days to resolution for APY calculation
        now_t = time.time()
        end_times = []
        for m in ms:
            et_str = m.get("end") or m.get("cutoff_time")
            dt = parse_datetime(et_str)
            if dt:
                end_times.append(dt.timestamp())
        valid_ends = [et for et in end_times if et > now_t]
        days_to_res = min((et - now_t) / 86400.0 for et in valid_ends) if valid_ends else None

        # 4. Exact Mathematical Basket Search (Phase 0.1)
        bk = best_basket(
            kind=kind,
            asks=asks,
            fee_bps=self.cfg.fees.polymarket_taker_bps,
            fees=fees,
            include_void=self.cfg.risk.model_void_outcomes,
            days_to_resolution=days_to_res,
        )

        # STRICT EDGE GATE (Phase 0.1): Only positive net edge after all taker fees
        if not bk or bk.edge < self.cfg.risk.min_edge:
            return False

        # 5. Circuit Breaker on Abnormal Edge (Phase 0.2)
        if bk.edge > self.cfg.risk.max_anomaly_edge:
            logger.warning(f"[ANOMALY CIRCUIT BREAKER] Rejected {cand['id']}: edge {bk.edge:.3f} > max {self.cfg.risk.max_anomaly_edge:.3f}")
            return False

        # 6. Event and Venue Exposure Caps (Phase 0.2 & 1.5)
        event_slug = ms[0].get("event") or ms[0].get("event_title") or "default_event"
        current_event_exp = sum(
            s.total_cost_usd for s in self.portfolio.open_structures
            if (s.event_title == ms[0].get("event_title") or s.relation_id == cand["id"])
        )
        if current_event_exp >= self.cfg.risk.max_capital_per_event:
            logger.info(f"Skipping {cand['id']}: event cap reached (${current_event_exp:.2f})")
            return False

        # 7. Sizing with depth limits and slippage controls (Phase 0.4)
        lf = [fees[i] for i, _ in bk.legs]
        target_spend_inr = min(1000.0, max(400.0, self.portfolio.available_cash_inr * 0.15))
        target_spend_usd = target_spend_inr / INR_PER_USD

        sweep_res = sweep_with_limits(
            [books[i][s] for i, s in bk.legs],
            bk.payoff,
            fee_bps=self.cfg.fees.polymarket_taker_bps,
            fees=lf,
            max_capital=min(target_spend_usd, self.cfg.risk.max_capital_per_relation),
            max_slippage_bps=self.cfg.risk.max_slippage_bps,
            min_depth=self.cfg.risk.min_depth_thinnest_leg,
        )

        shares = sweep_res.shares
        if shares < 2.0:
            return False

        # Build legs
        legs_info = []
        order_requests = []
        for i, s in bk.legs:
            m = ms[i]
            p = asks[i][s]
            exp = m.get("end") or m.get("cutoff_time") or ""
            legs_info.append({
                "market_id": m["id"],
                "venue": m.get("venue", "polymarket"),
                "question": m.get("question", ""),
                "side": s,
                "entry_price_usd": p,
                "shares": shares,
                "expiry": exp,
            })
            order_requests.append(OrderRequest(
                market_id=m["id"],
                venue=m.get("venue", "polymarket"),
                side=s,
                price=p,
                size=shares,
                order_type="IOC",
            ))

        # Check venue balance limits
        poly_cost_usd = sum(l["shares"] * l["entry_price_usd"] for l in legs_info if l["venue"] == "polymarket")
        kalshi_cost_usd = sum(l["shares"] * l["entry_price_usd"] for l in legs_info if l["venue"] == "kalshi")

        poly_locked, kalshi_locked = self.portfolio.get_locked_capital_per_venue()
        if poly_locked + poly_cost_usd > self.cfg.risk.max_capital_per_venue:
            logger.info(f"Skipping {cand['id']}: Polymarket venue capital cap exceeded")
            return False
        if kalshi_locked + kalshi_cost_usd > self.cfg.risk.max_capital_per_venue:
            logger.info(f"Skipping {cand['id']}: Kalshi venue capital cap exceeded")
            return False

        # Execute through ExecutionRouter (Phase 1.3)
        loop = asyncio.new_event_loop()
        try:
            exec_results = loop.run_until_complete(self.router.execute_structure(order_requests))
        finally:
            loop.close()

        if any(r.status != "FILLED" for r in exec_results):
            logger.warning(f"Candidate {cand['id']} rejected: one or more legs failed execution.")
            return False

        event_title = ms[0].get("event_title") or f"{kind.title()} Arbitrage Structure"
        note = (f"Mathematically verified edge: +${bk.edge:.4f}/sh | Payoff: ${bk.payoff:.2f}/sh | "
                f"Slippage: {sweep_res.slippage_bps:.0f}bps | APY: {bk.annualized_return:.1f}%")

        # Book into portfolio with exact verified basket payoff (Phase 0.3)
        booked = self.portfolio.book_structure(
            kind=kind,
            event_title=event_title,
            relation_id=cand["id"],
            legs_info=legs_info,
            note=note,
            unit_payoff=bk.payoff,
        )

        if booked:
            logger.info(f"[POLLER BOOKED] {booked.structure_id}: {event_title} | "
                        f"Cost: INR {booked.total_cost_inr:.2f} | Exp: {booked.expiry_display} | "
                        f"Edge: +INR {booked.projected_profit_inr:.2f}")

            # Record in immutable audit log (Phase 1.4)
            self.audit_log.log(
                action="STRUCTURE_BOOKED",
                venue="cross_venue" if len({l["venue"] for l in legs_info}) > 1 else legs_info[0]["venue"],
                market_id=cand["id"],
                price=round(booked.total_cost_usd / max(shares, 1.0), 4),
                size=shares,
                order_id=booked.structure_id,
                status="OPEN",
                metadata={
                    "kind": kind,
                    "event_title": event_title,
                    "edge": bk.edge,
                    "payoff_per_share": bk.payoff,
                    "projected_profit_inr": booked.projected_profit_inr,
                    "legs": legs_info,
                }
            )
            return True

        return False

    def _check_natural_expiry_settlements(self, now_dt: datetime):
        """Monitors open structures: if contract expiry timestamp has passed, settle all legs.
        Trade moves to Realised ONLY when all legs settle."""
        for struct in list(self.portfolio.open_structures):
            if struct.expiry:
                dt_exp = parse_datetime(struct.expiry)
                if dt_exp and now_dt >= dt_exp:
                    logger.info(f"[POLLER SETTLEMENT] Contract {struct.structure_id} reached expiry ({struct.expiry_display}). Settling all legs...")
                    self.portfolio.settle_entire_structure(struct.structure_id)
                    self.audit_log.log(
                        action="STRUCTURE_SETTLED",
                        venue="portfolio",
                        order_id=struct.structure_id,
                        status="REALISED",
                        metadata={"realised_profit_inr": struct.realised_profit_inr}
                    )
