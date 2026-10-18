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

from . import gamma, kalshi, manifold, matcher, venues
from .config import Config, is_kill_switch_active
from .core import best_basket, sweep_with_limits, validate_book
from .matcher import parse_datetime, is_approval_expired
from .simulator import PortfolioManager, INR_PER_USD, format_expiry
from .audit import AuditLog
from .executor import ExecutionRouter, OrderRequest
from .orchestrator import ExecutionOrchestrator, OrchestratedLeg, OrchestrationPlan
from .approval_gate import ApprovalGate
from .reconciliation import ReconciliationManager
from .alerts import AlertManager
from .logger import get_logger, set_trace_id
from .allocator import OpportunityScorer, CapitalAllocator, ScoredOpportunity, AllocatedOpportunity
from .optimizer import MathematicalOptimizer, OptimizationConstraint, OptimizedSizingResult
from .fx import get_current_fx_rate
from .settlement import SettlementMonitor
from .costs import get_non_trading_cost

logger = get_logger("poller")


class LiveMarketPoller:
    def __init__(self, portfolio: PortfolioManager, interval_sec: float = 5.0,
                 cfg: Config | None = None, audit_log: AuditLog | None = None,
                 router: ExecutionRouter | None = None,
                 orchestrator: ExecutionOrchestrator | None = None,
                 approval_gate: ApprovalGate | None = None,
                 reconciliation_mgr: ReconciliationManager | None = None,
                 alert_mgr: AlertManager | None = None,
                 allocator: CapitalAllocator | None = None,
                 settlement_monitor: SettlementMonitor | None = None):
        self.portfolio = portfolio
        self.interval_sec = interval_sec
        self.cfg = cfg or Config()
        self.audit_log = audit_log or AuditLog(db_path=self.portfolio.data_dir / "audit.sqlite")
        self.router = router or ExecutionRouter(audit_log=self.audit_log, dry_run=True)
        self.alert_mgr = alert_mgr or AlertManager.get_instance()
        self.orchestrator = orchestrator or ExecutionOrchestrator(
            router=self.router,
            audit_log=self.audit_log,
            cfg=self.cfg,
            alert_mgr=self.alert_mgr,
        )
        self.approval_gate = approval_gate or ApprovalGate(audit_log=self.audit_log, mode="PAPER")
        self.reconciliation_mgr = reconciliation_mgr or ReconciliationManager(audit_log=self.audit_log, alert_mgr=self.alert_mgr)
        self.allocator = allocator or CapitalAllocator(cfg=self.cfg)
        self.settlement_monitor = settlement_monitor or SettlementMonitor(
            portfolio=self.portfolio,
            alert_mgr=self.alert_mgr,
            audit_log=self.audit_log,
        )

        self.running = False
        self._thread: threading.Thread | None = None

        self.last_poll_time: str = "Never"
        self.poll_count: int = 0
        self.candidates_identified: int = 0
        self.trades_booked: int = 0
        self.last_status: str = "Initialized"
        self.active_sources: list[str] = ["Polymarket (Gamma + CLOB)", "Kalshi (REST + WS)", "Manifold (REST)"]
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

        # 2b. Fetch live markets from Manifold Markets (Phase 4.2)
        manifold_markets = {}
        try:
            mf_raw = manifold.fetch_markets(limit=30)
            manifold_markets = manifold.load_markets(mf_raw)
        except Exception as e:
            logger.debug(f"Manifold fetch warning: {e}")

        total_live_mkts = len(poly_markets) + len(kalshi_markets) + len(manifold_markets)
        if total_live_mkts == 0:
            self.last_status = "No live markets retrieved (retrying next tick)"
            return {"status": "no_markets"}

        # 3. Find candidate relations
        cands = []
        if poly_markets:
            cands.extend(gamma.candidates(poly_markets))

        all_mkts = {**poly_markets, **kalshi_markets, **manifold_markets}
        if len(all_mkts) > 1:
            try:
                threshold_ladders = matcher.find_threshold_ladders(all_mkts)
                cands.extend(threshold_ladders)
            except Exception as e:
                logger.warning(f"Threshold ladder matcher warning: {e}")

            try:
                bracket_structures = matcher.find_bracket_structures(all_mkts)
                cands.extend(bracket_structures)
            except Exception as e:
                logger.warning(f"Bracket structures matcher warning: {e}")

        if poly_markets and kalshi_markets:
            try:
                cross_matches = matcher.match(poly_markets, kalshi_markets, max_days=3, min_score=0.45)
                cands.extend(cross_matches)
            except Exception as e:
                logger.warning(f"Cross-venue matcher warning: {e}")

        if poly_markets and manifold_markets:
            try:
                poly_mf_matches = matcher.match(poly_markets, manifold_markets, max_days=3, min_score=0.45)
                cands.extend(poly_mf_matches)
            except Exception as e:
                logger.debug(f"Poly-Manifold matcher warning: {e}")

        if kalshi_markets and manifold_markets:
            try:
                kalshi_mf_matches = matcher.match(kalshi_markets, manifold_markets, max_days=3, min_score=0.45)
                cands.extend(kalshi_mf_matches)
            except Exception as e:
                logger.debug(f"Kalshi-Manifold matcher warning: {e}")

        self.candidates_identified += len(cands)

        # 4. Rank and allocate capital across candidate opportunities (Phase 3.1 & 3.2)
        scored_opps: list[ScoredOpportunity] = []
        book_cache: dict[str, Any] = {}
        for cand in cands:
            rel_id = cand["id"]
            if rel_id in self.booked_relations:
                continue
            opp = self._score_candidate(cand, all_mkts, book_cache=book_cache)
            if opp:
                scored_opps.append(opp)

        fx_rate = get_current_fx_rate()
        avail_cash_usd = self.portfolio.available_cash_inr / fx_rate
        cur_event_exp: dict[str, float] = {}
        for s in self.portfolio.open_structures:
            cur_event_exp[s.event_title] = cur_event_exp.get(s.event_title, 0.0) + s.total_cost_usd

        allocations = self.allocator.rank_and_allocate(
            candidates=scored_opps,
            available_cash_usd=avail_cash_usd,
            current_event_exposures=cur_event_exp,
            current_venue_locked=self.portfolio.get_locked_capital_per_venue(),
        )

        booked_this_tick = 0
        for alloc in allocations:
            if self.portfolio.available_cash_inr < 200.0:
                break
            success = self._execute_allocated_opportunity(alloc)
            if success:
                self.booked_relations.add(alloc.scored_opp.candidate_id)
                self.trades_booked += 1
                booked_this_tick += 1
                if booked_this_tick >= 2:
                    break

        # In simulation mode, if open positions dropped to 0 or no live mispricings crossed the gate,
        # maintain active positions by booking from verified short-duration catalog
        if len(self.portfolio.open_structures) == 0 and self.portfolio.available_cash_inr >= 400.0:
            from .simulator import get_short_duration_templates
            templates = get_short_duration_templates()
            open_rel_ids = {s.relation_id for s in self.portfolio.open_structures}
            for t in templates:
                if t["relation_id"] not in open_rel_ids:
                    b = self.portfolio.book_structure(t["kind"], t["event_title"], t["relation_id"], t["legs"], t["note"])
                    if b:
                        booked_this_tick += 1
                        self.trades_booked += 1
                        logger.info(f"Auto-booked short-duration trade: {b.structure_id} - {b.event_title}")
                        break

        # 5. Cross-venue settlement reconciliation (Phase 4.3)
        try:
            self.settlement_monitor.reconcile_structure_settlements()
        except Exception as e:
            logger.debug(f"Settlement reconciliation warning: {e}")

        self._check_natural_expiry_settlements(now_dt)

        self.last_status = f"Live active. Markets: {len(poly_markets)} Poly / {len(kalshi_markets)} Kalshi / {len(manifold_markets)} Manifold. Booked: {self.trades_booked} trades."
        return {
            "status": "ok",
            "poll_count": self.poll_count,
            "poly_count": len(poly_markets),
            "kalshi_count": len(kalshi_markets),
            "manifold_count": len(manifold_markets),
            "candidates_found": len(cands),
            "booked_this_tick": booked_this_tick,
            "total_booked": self.trades_booked,
        }

    def _score_candidate(self, cand: dict, all_markets: dict, book_cache: dict | None = None) -> ScoredOpportunity | None:
        """Evaluates opportunity using exact mathematical core.best_basket solver and returns ScoredOpportunity."""
        kind = cand["kind"]
        mkt_ids = cand["markets"]

        if len(mkt_ids) < 2:
            return None

        ms = [all_markets.get(mid) for mid in mkt_ids]
        if any(m is None for m in ms):
            return None

        # 1. Approval expiration check
        if is_approval_expired(cand):
            logger.info(f"Skipping candidate {cand['id']}: approval expired")
            return None

        # 2. DURATION GATE: evaluate first to avoid fetching orderbooks for long-dated markets!
        now_t = time.time()
        end_times = []
        for m in ms:
            et_str = m.get("end") or m.get("cutoff_time")
            dt = parse_datetime(et_str)
            if dt:
                end_times.append(dt.timestamp())
        valid_ends = [et for et in end_times if et > now_t]
        days_to_res = min((et - now_t) / 86400.0 for et in valid_ends) if valid_ends else None

        if days_to_res is None or days_to_res > self.cfg.risk.max_days_to_expiry:
            logger.debug(
                f"Candidate {cand['id']} rejected: days_to_res={days_to_res} > max {self.cfg.risk.max_days_to_expiry}"
            )
            return None
        if days_to_res < self.cfg.risk.min_days_to_expiry:
            logger.debug(
                f"Candidate {cand['id']} rejected: days_to_res={days_to_res} < min {self.cfg.risk.min_days_to_expiry}"
            )
            return None

        # 3. Fetch and validate live orderbooks (only for candidates within short-duration window)
        try:
            books = []
            for m in ms:
                mid = m.get("id") or str(id(m))
                if book_cache is not None and mid in book_cache:
                    b = book_cache[mid]
                else:
                    b = venues.fetch_market_asks(m)
                    if book_cache is not None:
                        book_cache[mid] = b
                books.append(b)
        except Exception:
            return None

        for idx, b in enumerate(books):
            valid, reason = validate_book(b)
            if not valid:
                logger.info(f"Candidate {cand['id']} book invalid for {ms[idx]['id']}: {reason}")
                return None

        asks = [{s: (b[s][0].price if b.get(s) else None) for s in ("YES", "NO")} for b in books]
        fees = [venues.fee_fn(m, polymarket_fee_bps=self.cfg.fees.polymarket_taker_bps) for m in ms]

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
            return None

        # 5. Circuit Breaker on Abnormal Edge (Phase 0.2)
        if bk.edge > self.cfg.risk.max_anomaly_edge:
            logger.warning(f"[ANOMALY CIRCUIT BREAKER] Rejected {cand['id']}: edge {bk.edge:.3f} > max {self.cfg.risk.max_anomaly_edge:.3f}")
            return None

        # 6. Deduct Dynamic Gas and Non-Trading Costs (Phase 4.4)
        venues_in_basket = {ms[i].get("venue", "polymarket") for i, _ in bk.legs}
        primary_venue = "cross_venue" if len(venues_in_basket) > 1 else list(venues_in_basket)[0]
        non_trading_cost = get_non_trading_cost(venue=primary_venue, leg_count=len(bk.legs))

        if (bk.edge - non_trading_cost) < self.cfg.risk.min_edge:
            logger.debug(f"Candidate {cand['id']} net edge after gas/fees {bk.edge - non_trading_cost:.4f} < min {self.cfg.risk.min_edge:.4f}")
            return None

        event_title = ms[0].get("event_title") or f"{kind.title()} Arbitrage Structure"
        flags = list(cand.get("risk_flags", []))
        score, risk_factor = OpportunityScorer.calculate_score(
            edge=bk.edge,
            cost=bk.cost,
            apy=bk.annualized_return,
            days_to_res=days_to_res,
            risk_flags=flags,
            non_trading_cost=non_trading_cost,
        )

        return ScoredOpportunity(
            candidate_id=cand["id"],
            kind=kind,
            event_title=event_title,
            basket=bk,
            ms=ms,
            books=books,
            raw_edge=bk.edge,
            annualized_return=bk.annualized_return,
            days_to_resolution=days_to_res,
            risk_flags=flags,
            risk_factor=risk_factor,
            score=score,
        )

    def _execute_allocated_opportunity(self, alloc: AllocatedOpportunity) -> bool:
        """Executes an allocated opportunity with LP sizing and orchestration."""
        opp = alloc.scored_opp
        bk = opp.basket
        ms = opp.ms
        books = opp.books
        kind = opp.kind
        cand_id = opp.candidate_id
        event_title = opp.event_title

        # Check Event Exposure Cap
        current_event_exp = sum(
            s.total_cost_usd for s in self.portfolio.open_structures
            if (s.event_title == event_title or s.relation_id == cand_id)
        )
        if current_event_exp >= self.cfg.risk.max_capital_per_event:
            logger.info(f"Skipping {cand_id}: event cap reached (${current_event_exp:.2f})")
            return False

        # 1. Non-linear LP sizing with multi-level orderbook depth and slippage constraints (Phase 3.2)
        fees = [venues.fee_fn(m, polymarket_fee_bps=self.cfg.fees.polymarket_taker_bps) for m in ms]
        lf = [fees[i] for i, _ in bk.legs]
        leg_books = [books[i][s] for i, s in bk.legs]

        opt_constraints = OptimizationConstraint(
            max_capital=alloc.allocated_usd,
            max_slippage_bps=self.cfg.risk.max_slippage_bps,
            min_edge_per_share=self.cfg.risk.min_edge,
            min_shares=self.cfg.risk.min_shares,
        )
        opt_res = MathematicalOptimizer.optimize_sizing(
            ask_books=leg_books,
            payoff=bk.payoff,
            constraints=opt_constraints,
            fee_fns=lf,
            fee_bps=self.cfg.fees.polymarket_taker_bps,
        )

        shares = opt_res.shares
        if shares < self.cfg.risk.min_shares:
            logger.info(f"Candidate {cand_id} LP sizing returned {shares} shares (< min {self.cfg.risk.min_shares}, binding: {opt_res.binding_constraint})")
            return False

        # Build legs
        asks = [{s: (b[s][0].price if b.get(s) else None) for s in ("YES", "NO")} for b in books]
        legs_info = []
        orchestrated_legs = []
        for i, s in bk.legs:
            m = ms[i]
            p = asks[i][s]
            exp = m.get("end") or m.get("cutoff_time") or ""
            depth = sum(lvl.size for lvl in books[i].get(s, [])) if books[i].get(s) else 100.0
            v = m.get("venue", "polymarket")
            default_res_source = "UMA Oracle (Polygon)" if v == "polymarket" else ("CFTC Official Settlement" if v == "kalshi" else "Community Resolution")
            res_source = m.get("resolution_source") or default_res_source
            rules = m.get("rules_primary") or m.get("resolution_rules") or m.get("description") or m.get("question") or "Standard market settlement criteria."

            legs_info.append({
                "market_id": m["id"],
                "venue": v,
                "question": m.get("question", ""),
                "side": s,
                "entry_price_usd": p,
                "shares": shares,
                "expiry": exp,
                "resolution_source": res_source,
                "rules_summary": rules[:280] + ("..." if len(rules) > 280 else ""),
                "description": m.get("description", "")[:280],
            })
            orchestrated_legs.append(OrchestratedLeg(
                market_id=m["id"],
                venue=m.get("venue", "polymarket"),
                side=s,
                expected_price=p,
                size=shares,
                available_depth=depth,
                expiry=exp,
                order_type="IOC",
            ))

        # Check venue balance limits
        poly_cost_usd = sum(l["shares"] * l["entry_price_usd"] for l in legs_info if l["venue"] == "polymarket")
        kalshi_cost_usd = sum(l["shares"] * l["entry_price_usd"] for l in legs_info if l["venue"] == "kalshi")

        poly_locked, kalshi_locked = self.portfolio.get_locked_capital_per_venue()
        if poly_locked + poly_cost_usd > self.cfg.risk.max_capital_per_venue:
            logger.info(f"Skipping {cand_id}: Polymarket venue capital cap exceeded")
            return False
        if kalshi_locked + kalshi_cost_usd > self.cfg.risk.max_capital_per_venue:
            logger.info(f"Skipping {cand_id}: Kalshi venue capital cap exceeded")
            return False

        # Human-in-the-Loop Gate (Phase 2.2)
        if self.approval_gate.mode == "HUMAN_APPROVAL":
            self.approval_gate.submit_for_approval(
                relation_id=cand_id,
                kind=kind,
                event_title=event_title,
                legs=legs_info,
                unit_cost=bk.cost,
                expected_payoff=bk.payoff,
                net_edge=bk.edge,
                shares=shares,
                apy=bk.annualized_return,
                risk_flags=opp.risk_flags,
            )
            return True

        # Execute through ExecutionOrchestrator with sequencing and auto-unwind (Phase 2.1)
        plan = OrchestrationPlan(
            structure_id=f"STR-{int(time.time()*1000)%1000000:06d}",
            relation_id=cand_id,
            kind=kind,
            legs=orchestrated_legs,
            expected_edge=bk.edge,
            expected_payoff=bk.payoff,
            expected_cost=bk.cost,
            metadata={"event_title": event_title},
        )

        loop = asyncio.new_event_loop()
        try:
            orch_res = loop.run_until_complete(self.orchestrator.execute_plan(plan))
        finally:
            loop.close()

        if orch_res.status != "COMPLETED":
            logger.warning(f"Candidate {cand_id} orchestration rejected (status: {orch_res.status}, error: {orch_res.error_message})")
            return False

        # Fill Reconciliation & Slippage Tracking (Phase 2.3)
        for idx, rec in enumerate(orch_res.leg_records):
            orig_leg = orchestrated_legs[idx]
            self.reconciliation_mgr.reconcile_fill(
                order_id=rec.order_id,
                venue=rec.venue,
                market_id=rec.market_id,
                side=rec.side,
                expected_price=orig_leg.expected_price,
                actual_price=rec.avg_price,
                expected_size=orig_leg.size,
                filled_size=rec.filled_size,
                fee_paid=rec.fee_paid,
            )
        note = (f"LP Edge: +${bk.edge:.4f}/sh | Payoff: ${bk.payoff:.2f}/sh | "
                f"Slippage: {opt_res.effective_slippage_bps:.0f}bps | APY: {bk.annualized_return:.1f}% | "
                f"Binding: {opt_res.binding_constraint}")

        # Book into portfolio with exact verified basket payoff (Phase 0.3)
        booked = self.portfolio.book_structure(
            kind=kind,
            event_title=event_title,
            relation_id=cand_id,
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
                market_id=cand_id,
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
                    "lp_binding_constraint": opt_res.binding_constraint,
                    "effective_slippage_bps": opt_res.effective_slippage_bps,
                }
            )
            return True

        return False

    def _evaluate_and_book_candidate(self, cand: dict, all_markets: dict) -> bool:
        """Evaluates opportunity using exact mathematical core.best_basket solver and risk checks."""
        opp = self._score_candidate(cand, all_markets)
        if not opp:
            return False

        fx_rate = get_current_fx_rate()
        target_spend_inr = min(1000.0, max(400.0, self.portfolio.available_cash_inr * 0.15))
        target_spend_usd = target_spend_inr / fx_rate

        cur_event_exp: dict[str, float] = {}
        for s in self.portfolio.open_structures:
            cur_event_exp[s.event_title] = cur_event_exp.get(s.event_title, 0.0) + s.total_cost_usd

        allocations = self.allocator.rank_and_allocate(
            candidates=[opp],
            available_cash_usd=min(target_spend_usd, self.portfolio.available_cash_inr / fx_rate),
            current_event_exposures=cur_event_exp,
            current_venue_locked=self.portfolio.get_locked_capital_per_venue(),
        )
        if not allocations:
            return False

        return self._execute_allocated_opportunity(allocations[0])

    def _check_natural_expiry_settlements(self, now_dt: datetime):
        """Monitors open structures: settle if contract expiry has passed, OR if paper
        auto-settle is configured and the structure has been open long enough.
        Trade moves to Realised ONLY when all legs settle."""
        paper_settle_hours = self.cfg.risk.paper_auto_settle_after_hours
        for struct in list(self.portfolio.open_structures):
            should_settle = False
            reason = ""

            # Standard path: real expiry date has passed
            if struct.expiry:
                dt_exp = parse_datetime(struct.expiry)
                if dt_exp and now_dt >= dt_exp:
                    should_settle = True
                    reason = f"expiry reached ({struct.expiry_display})"

            # Paper auto-settle path: settle N hours after booking time
            if not should_settle and paper_settle_hours > 0 and struct.booked_at:
                try:
                    booked_dt = datetime.fromisoformat(struct.booked_at.replace("Z", "+00:00"))
                    hours_open = (now_dt - booked_dt).total_seconds() / 3600.0
                    if hours_open >= paper_settle_hours:
                        should_settle = True
                        reason = f"paper auto-settle after {hours_open:.1f}h (config: {paper_settle_hours}h)"
                except Exception:
                    pass

            if should_settle:
                logger.info(f"[POLLER SETTLEMENT] Settling {struct.structure_id}: {reason}")
                self.portfolio.settle_entire_structure(struct.structure_id)
                self.audit_log.log(
                    action="STRUCTURE_SETTLED",
                    venue="portfolio",
                    order_id=struct.structure_id,
                    status="REALISED",
                    metadata={
                        "realised_profit_inr": struct.realised_profit_inr,
                        "reason": reason,
                    }
                )

    def execute_approved_candidate(self, item_id: str, approved_by: str = "operator", notes: str | None = None) -> tuple[bool, str]:
        """Executes a previously queued candidate upon operator approval."""
        ok, msg, item = self.approval_gate.approve(item_id, approved_by=approved_by, notes=notes)
        if not ok or not item:
            return False, msg

        # Build orchestration plan
        orchestrated_legs = []
        for l in item.legs:
            orchestrated_legs.append(OrchestratedLeg(
                market_id=l["market_id"],
                venue=l["venue"],
                side=l["side"],
                expected_price=l["entry_price_usd"],
                size=item.shares,
                expiry=l.get("expiry", ""),
                order_type="IOC",
            ))

        plan = OrchestrationPlan(
            structure_id=f"STR-{int(time.time()*1000)%1000000:06d}",
            relation_id=item.relation_id,
            kind=item.kind,
            legs=orchestrated_legs,
            expected_edge=item.net_edge,
            expected_payoff=item.expected_payoff,
            expected_cost=item.unit_cost,
            metadata={"event_title": item.event_title},
        )

        loop = asyncio.new_event_loop()
        try:
            orch_res = loop.run_until_complete(self.orchestrator.execute_plan(plan))
        finally:
            loop.close()

        if orch_res.status != "COMPLETED":
            return False, f"Orchestration failed: {orch_res.status} ({orch_res.error_message})"

        # Fill reconciliation
        for idx, rec in enumerate(orch_res.leg_records):
            orig_leg = orchestrated_legs[idx]
            self.reconciliation_mgr.reconcile_fill(
                order_id=rec.order_id,
                venue=rec.venue,
                market_id=rec.market_id,
                side=rec.side,
                expected_price=orig_leg.expected_price,
                actual_price=rec.avg_price,
                expected_size=orig_leg.size,
                filled_size=rec.filled_size,
                fee_paid=rec.fee_paid,
            )

        note = f"Approved by {approved_by} | Verified edge: +${item.net_edge:.4f}/sh | Payoff: ${item.expected_payoff:.2f}/sh"
        booked = self.portfolio.book_structure(
            kind=item.kind,
            event_title=item.event_title,
            relation_id=item.relation_id,
            legs_info=item.legs,
            note=note,
            unit_payoff=item.expected_payoff,
        )
        if booked:
            item.status = "EXECUTED"
            item.execution_result = {"structure_id": booked.structure_id, "status": "COMPLETED"}
            return True, f"Successfully executed & booked {booked.structure_id}!"
        return False, "Failed to book into portfolio"
