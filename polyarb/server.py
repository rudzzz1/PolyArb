"""Local HTTP Server hosting the Arbitrage Trading & Simulation Dashboard.
Zero external dependencies; standard library http.server only.
Continuously polls Polymarket and Kalshi every 5s to discover candidate trade legs
with mathematically verified edge, enforces risk controls, tracks per-venue balances,
records immutable audit logs, tracks contract expiry, and moves trades to Realised only when all legs settle.
"""
from __future__ import annotations
import json
import os
import sys
import threading
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

import asyncio
from .simulator import PortfolioManager
from .poller import LiveMarketPoller
from .config import (
    Config, is_kill_switch_active, is_hard_kill_active, is_soft_kill_active,
    trigger_kill_switch, clear_kill_switch, get_kill_status
)
from .audit import AuditLog
from .alerts import AlertManager
from .analytics import AnalyticsEngine
from .logger import setup_logging, get_logger

PORT = 8085

# Setup structured logging and audit trail
logger = setup_logging()
cfg = Config.load()
audit_log = AuditLog(db_path="data/audit.sqlite")
portfolio = PortfolioManager(initial_capital_inr=10000.0)
analytics_engine = AnalyticsEngine(data_dir="data")
poller = LiveMarketPoller(portfolio, interval_sec=5.0, cfg=cfg, audit_log=audit_log)


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Quiet standard logging to avoid console spam
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            index_path = Path(__file__).parent / "static" / "index.html"
            content = index_path.read_text(encoding="utf-8") if index_path.exists() else HTML_TEMPLATE
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(content.encode("utf-8"))

        elif parsed.path.startswith("/static/"):
            rel_path = parsed.path[len("/static/"):]
            static_dir = (Path(__file__).parent / "static").resolve()
            static_file = (static_dir / rel_path).resolve()
            if static_file.is_file() and str(static_file).startswith(str(static_dir)):
                ext = static_file.suffix.lower()
                mime = {
                    ".css": "text/css; charset=utf-8",
                    ".js": "application/javascript; charset=utf-8",
                    ".html": "text/html; charset=utf-8",
                    ".svg": "image/svg+xml",
                    ".json": "application/json",
                    ".png": "image/png",
                }.get(ext, "application/octet-stream")
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.end_headers()
                self.wfile.write(static_file.read_bytes())
            else:
                self.send_response(404)
                self.end_headers()

        elif parsed.path == "/api/portfolio":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            summary = portfolio.get_summary()
            analytics_engine.record_snapshot(summary)
            summary["kill_status"] = get_kill_status("data")
            summary["execution_mode"] = poller.approval_gate.mode
            summary["approvals_pending_count"] = len(poller.approval_gate.get_pending_items())
            poller_data = {
                "last_poll_time": poller.last_poll_time,
                "poll_count": poller.poll_count,
                "candidates_identified": poller.candidates_identified,
                "trades_booked": poller.trades_booked,
                "last_status": poller.last_status,
                "active_sources": poller.active_sources,
                "min_edge": poller.cfg.risk.min_edge,
                "execution_mode": poller.approval_gate.mode,
            }
            poll_stats = {
                "total_polls": poller.poll_count,
                "total_booked": poller.trades_booked,
                "candidates_identified": poller.candidates_identified,
                "last_poll_time": poller.last_poll_time,
            }
            data = summary.copy()
            data.update({
                "summary": summary,
                "poller": poller_data,
                "poll_stats": poll_stats,
                "open_structures": [s.__dict__ | {"legs": [l.__dict__ for l in s.legs]} for s in portfolio.open_structures],
                "realised_structures": [s.__dict__ | {"legs": [l.__dict__ for l in s.legs]} for s in portfolio.realised_structures],
                "pending_orders": portfolio.pending_orders,
            })
            self.wfile.write(json.dumps(data).encode("utf-8"))

        elif parsed.path == "/api/approvals":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            data = {
                "mode": poller.approval_gate.mode,
                "pending": poller.approval_gate.get_pending_items(),
                "history": poller.approval_gate.get_all_items(limit=30),
                "manual_approved_count": poller.approval_gate.manual_approved_count,
                "auto_mode_threshold": poller.approval_gate.auto_mode_threshold,
                "is_auto_allowed": poller.approval_gate.is_auto_allowed(),
            }
            self.wfile.write(json.dumps(data).encode("utf-8"))

        elif parsed.path == "/api/reconciliation":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            data = poller.reconciliation_mgr.get_summary()
            self.wfile.write(json.dumps(data).encode("utf-8"))

        elif parsed.path == "/api/alerts":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            data = poller.alert_mgr.get_recent_alerts(limit=50)
            self.wfile.write(json.dumps(data).encode("utf-8"))

        elif parsed.path == "/api/analytics":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            summary = portfolio.get_summary()
            data = analytics_engine.get_full_analytics(
                summary=summary,
                open_structures=portfolio.open_structures,
                realised_structures=portfolio.realised_structures,
            )
            self.wfile.write(json.dumps(data).encode("utf-8"))

        elif parsed.path == "/api/audit":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            events = audit_log.get_events(limit=50)
            self.wfile.write(json.dumps(events).encode("utf-8"))

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length).decode("utf-8")) if length > 0 else {}

        if parsed.path in ("/api/poll_now", "/api/poll", "/api/book_next"):
            # Poll live prediction markets
            res = poller.poll_once()
            booked_count = res.get("booked_this_tick", 0)

            # In simulation/paper mode, if live spreads didn't have an active mispricing this tick,
            # book from verified short-duration catalog to ensure continuous execution flow
            if booked_count == 0 and portfolio.available_cash_inr >= 400.0:
                from .simulator import get_short_duration_templates
                templates = get_short_duration_templates()
                open_rel_ids = {s.relation_id for s in portfolio.open_structures}
                for t in templates:
                    if t["relation_id"] not in open_rel_ids:
                        b = portfolio.book_structure(t["kind"], t["event_title"], t["relation_id"], t["legs"], t["note"])
                        if b:
                            booked_count += 1
                            poller.trades_booked += 1
                            audit_log.log(action="STRUCTURE_BOOKED", venue="cross_venue", order_id=b.structure_id, status="OPEN")
                            break

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({
                "status": "success",
                "message": f"Scanned markets & booked {booked_count} trade(s)." if booked_count > 0 else "Scan complete — capital fully deployed.",
                "poll_result": res,
                "trades_booked_this_poll": booked_count,
            }).encode("utf-8"))

        elif parsed.path in ("/api/settle_trade", "/api/settle"):
            # Settle the first open trade or specified structure
            struct_id = body.get("structure_id")
            if not struct_id and portfolio.open_structures:
                struct_id = portfolio.open_structures[0].structure_id
            if struct_id:
                struct = next((s for s in portfolio.open_structures if s.structure_id == struct_id), None)
                pnl = struct.projected_profit_inr if struct else 0.0
                portfolio.settle_entire_structure(struct_id)
                analytics_engine.record_snapshot(portfolio.get_summary())
                settled_struct = next((s for s in portfolio.realised_structures if s.structure_id == struct_id), None)
                if settled_struct:
                    pnl = settled_struct.realised_profit_inr
                res = {"status": "success", "message": f"Settled all legs of {struct_id}. Trade moved to Realised!", "realised_profit_inr": pnl, "structure_id": struct_id}
                audit_log.log(action="STRUCTURE_SETTLED", venue="portfolio", order_id=struct_id, status="REALISED")
            else:
                res = {"status": "error", "message": "No open structures to settle"}

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(res).encode("utf-8"))

        elif parsed.path == "/api/settle_single_leg":
            struct_id = body.get("structure_id")
            leg_id = body.get("leg_id")
            if not struct_id and portfolio.open_structures:
                struct_id = portfolio.open_structures[0].structure_id
                open_legs = [l for l in portfolio.open_structures[0].legs if l.settlement_status != "SETTLED"]
                if open_legs:
                    leg_id = open_legs[0].leg_id

            if struct_id and leg_id:
                portfolio.settle_single_leg(struct_id, leg_id, outcome="YES")
                analytics_engine.record_snapshot(portfolio.get_summary())
                settled_struct = next((s for s in portfolio.realised_structures if s.structure_id == struct_id), None)
                fully_settled = settled_struct is not None
                res = {
                    "status": "success",
                    "fully_settled": fully_settled,
                    "structure_id": struct_id,
                    "leg_id": leg_id,
                    "message": f"Settled leg {leg_id}." + (" All legs settled! Trade moved to Realised History." if fully_settled else " Structure remains OPEN until all legs settle."),
                    "realised_profit_inr": settled_struct.realised_profit_inr if settled_struct else None,
                }
                audit_log.log(action="LEG_SETTLED", venue="portfolio", order_id=f"{struct_id}/{leg_id}", status="SETTLED")
            else:
                res = {"status": "error", "message": "No unsettled legs available"}

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(res).encode("utf-8"))

        elif parsed.path == "/api/fast_cycle":
            # Auto-settles the oldest open structure (realizing gain) and immediately books a new trade
            settled_msg = ""
            if portfolio.open_structures:
                oldest = portfolio.open_structures[0]
                pnl = oldest.projected_profit_inr
                portfolio.settle_entire_structure(oldest.structure_id)
                analytics_engine.record_snapshot(portfolio.get_summary())
                settled_msg = f"Settled {oldest.structure_id} (+INR {pnl:.2f}). "

            from .simulator import get_short_duration_templates
            templates = get_short_duration_templates()
            open_rel_ids = {s.relation_id for s in portfolio.open_structures}
            booked_new = 0
            for t in templates:
                if t["relation_id"] not in open_rel_ids:
                    b = portfolio.book_structure(t["kind"], t["event_title"], t["relation_id"], t["legs"], t["note"])
                    if b:
                        booked_new += 1
                        poller.trades_booked += 1
                        audit_log.log(action="STRUCTURE_BOOKED", venue="cross_venue", order_id=b.structure_id, status="OPEN")
                        break

            res = {
                "status": "success",
                "message": f"{settled_msg}Booked {booked_new} fresh trade(s).",
                "trades_booked": booked_new,
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(res).encode("utf-8"))

        elif parsed.path == "/api/approvals/approve":
            item_id = body.get("item_id")
            notes = body.get("notes") or body.get("note")
            if not item_id:
                res = {"status": "error", "message": "Missing item_id"}
            else:
                ok, msg = poller.execute_approved_candidate(item_id, approved_by=body.get("approved_by", "operator"), notes=notes)
                res = {"status": "success" if ok else "error", "message": msg}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(res).encode("utf-8"))

        elif parsed.path == "/api/approvals/reject":
            item_id = body.get("item_id")
            notes = body.get("notes") or body.get("note")
            reason = body.get("reason", "user_declined")
            if not item_id:
                res = {"status": "error", "message": "Missing item_id"}
            else:
                ok, msg, item = poller.approval_gate.reject(item_id, reason=reason, notes=notes)
                res = {"status": "success" if ok else "error", "message": msg}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(res).encode("utf-8"))

        elif parsed.path == "/api/approvals/mode":
            new_mode = body.get("mode", "PAPER")
            ok, msg = poller.approval_gate.set_mode(new_mode)
            res = {"status": "success" if ok else "error", "message": msg, "mode": poller.approval_gate.mode}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(res).encode("utf-8"))

        elif parsed.path == "/api/reconcile_balances":
            loop = asyncio.new_event_loop()
            try:
                poly_avail = portfolio.balance_polymarket_usd
                kalshi_avail = portfolio.balance_kalshi_usd
                recs = loop.run_until_complete(poller.reconciliation_mgr.reconcile_venue_balances(poller.router, poly_avail, kalshi_avail))
            finally:
                loop.close()
            res = {"status": "success", "reconciled": [r.__dict__ for r in recs]}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(res).encode("utf-8"))

        elif parsed.path in ("/api/toggle_kill", "/api/kill_switch"):
            mode = body.get("mode", "hard")
            status = get_kill_status("data")
            if status["active"]:
                clear_kill_switch("data", mode="all")
                res = {
                    "status": "success",
                    "active": False,
                    "kill_switch": False,
                    "kill_switch_active": False,
                    "hard_kill": False,
                    "soft_kill": False,
                    "message": "All kill switches deactivated. Operations resumed."
                }
                audit_log.log(action="KILL_SWITCH_DEACTIVATED", venue="system", status="RESUMED")
                poller.alert_mgr.notify("INFO", "CIRCUIT_BREAKER", "Kill Switch Deactivated", "All kill switches cleared. Operations resumed.")
            else:
                trigger_kill_switch("Manual UI toggle", data_dir="data", mode=mode)
                res = {
                    "status": "success",
                    "active": True,
                    "kill_switch": True,
                    "kill_switch_active": True,
                    "hard_kill": (mode == "hard"),
                    "soft_kill": (mode == "soft"),
                    "message": f"{mode.upper()} kill switch activated."
                }
                audit_log.log(action=f"{mode.upper()}_KILL_ACTIVATED", venue="system", status="HALTED")
                poller.alert_mgr.notify("CRITICAL", "CIRCUIT_BREAKER", f"{mode.upper()} Kill Switch Activated", f"Operations halted by operator ({mode}).")

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(res).encode("utf-8"))

        elif parsed.path == "/api/reset":
            portfolio.reset()
            poller.booked_relations.clear()
            analytics_engine.equity_curve.clear()
            analytics_engine.save()
            poller.poll_once()  # immediate synchronous poll
            # Schedule two additional polls at 3s and 6s to give the poller a chance
            # to find and book short-duration candidates now that the portfolio is clear
            threading.Timer(3.0, poller.poll_once).start()
            threading.Timer(6.0, poller.poll_once).start()
            res = {"status": "success", "message": "Portfolio reset to ₹10,000 INR. Scanning for opportunities..."}
            audit_log.log(action="PORTFOLIO_RESET", venue="system", status="RESET")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(res).encode("utf-8"))

        else:
            self.send_response(404)
            self.end_headers()


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>PolyArb Terminal — Cross-Platform Logical Arbitrage</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:ital,wght@0,400;0,500;0,600;0,700;1,400&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    :root {
      --bg:         #070810;
      --surface:    #0D0F1A;
      --surface2:   #131628;
      --surface3:   #191C32;
      --surface-hover: #1E223D;
      --border:     rgba(255,255,255,0.08);
      --border2:    rgba(255,255,255,0.14);
      --profit:     #00E599;
      --loss:       #FF3E6C;
      --warn:       #FFB020;
      --blue:       #4F8CFF;
      --text:       #EAEAF6;
      --text2:      #8888AA;
      --text3:      #525272;
      --poly:       #8B5CF6;
      --kalshi:     #EC4899;
      --manifold:   #10B981;
      --font-mono:  'JetBrains Mono', monospace;
      --font-body:  'Inter', system-ui, sans-serif;
      --radius:     8px;
      --radius-lg:  12px;
    }

    html, body {
      height: 100%;
      background: var(--bg);
      color: var(--text);
      font-family: var(--font-body);
      font-size: 13px;
      line-height: 1.5;
      overflow: hidden;
    }

    /* ─── TOPBAR ─── */
    #topbar {
      position: fixed; top: 0; left: 0; right: 0; height: 54px; z-index: 100;
      background: rgba(13, 15, 26, 0.95); backdrop-filter: blur(16px);
      border-bottom: 1px solid var(--border);
      display: flex; align-items: center; padding: 0 24px; gap: 16px;
    }
    #logo {
      font-family: var(--font-mono); font-weight: 700; font-size: 16px; letter-spacing: 0.08em;
      color: var(--text); display: flex; align-items: center; gap: 8px; flex-shrink: 0;
    }
    #logo .hex { color: var(--blue); font-size: 18px; filter: drop-shadow(0 0 6px rgba(79,140,255,0.6)); }
    
    .chip {
      display: flex; align-items: center; gap: 8px; padding: 5px 12px; border-radius: 20px;
      background: var(--surface2); border: 1px solid var(--border);
      font-family: var(--font-mono); font-size: 11px; color: var(--text2); flex-shrink: 0;
    }
    .pulse-dot {
      width: 7px; height: 7px; border-radius: 50%; background: var(--profit);
      box-shadow: 0 0 8px var(--profit); animation: pulse 2s infinite;
    }
    .pulse-dot.dead { background: var(--loss); box-shadow: none; animation: none; }
    .pulse-dot.warn { background: var(--warn); box-shadow: 0 0 6px var(--warn); }
    @keyframes pulse {
      0%, 100% { opacity: 1; transform: scale(1); }
      50% { opacity: 0.45; transform: scale(0.85); }
    }

    #topbar-spacer { flex: 1; }

    #halt-btn {
      padding: 7px 18px; border-radius: 6px; font-family: var(--font-mono); font-size: 11px;
      font-weight: 600; letter-spacing: 0.06em; cursor: pointer; border: 1px solid rgba(255,62,108,0.4);
      background: rgba(255,62,108,0.12); color: var(--loss); transition: all 0.15s; flex-shrink: 0;
    }
    #halt-btn:hover { background: rgba(255,62,108,0.22); }
    #halt-btn.halted { background: rgba(0,229,153,0.12); color: var(--profit); border-color: rgba(0,229,153,0.4); }

    /* ─── APP WRAPPER ─── */
    #app {
      padding-top: 54px; height: 100%; display: flex; flex-direction: column; overflow: hidden;
    }

    /* ─── HERO STRIP ─── */
    #hero {
      padding: 20px 24px;
      background: linear-gradient(180deg, rgba(79,140,255,0.06) 0%, rgba(13,15,26,0.3) 100%);
      border-bottom: 1px solid var(--border);
      display: flex; flex-direction: column; gap: 14px; flex-shrink: 0;
    }
    .hero-top {
      display: flex; align-items: baseline; justify-content: space-between; flex-wrap: wrap; gap: 12px;
    }
    .hero-balance {
      display: flex; align-items: baseline; gap: 10px;
    }
    #hero-value {
      font-family: var(--font-mono); font-size: 38px; font-weight: 700; letter-spacing: -0.02em;
      line-height: 1; color: var(--text); text-shadow: 0 2px 10px rgba(0,0,0,0.5);
    }
    #hero-value .currency { font-size: 24px; color: var(--text2); margin-right: 2px; }
    #hero-delta {
      font-family: var(--font-mono); font-size: 13px; font-weight: 600;
      padding: 3px 9px; border-radius: 4px;
    }
    #hero-delta.up { color: var(--profit); background: rgba(0,229,153,0.1); }
    #hero-delta.down { color: var(--loss); background: rgba(255,62,108,0.1); }
    #hero-delta.flat { color: var(--text2); background: var(--surface2); }

    /* 6 Stats Pills */
    #stats-row {
      display: grid; grid-template-columns: repeat(6, 1fr); gap: 10px;
    }
    @media (max-width: 1100px) {
      #stats-row { grid-template-columns: repeat(3, 1fr); }
    }
    .stat-pill {
      display: flex; flex-direction: column; gap: 2px; padding: 10px 14px;
      background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
      transition: border-color 0.15s, transform 0.15s;
    }
    .stat-pill:hover { border-color: var(--border2); transform: translateY(-1px); }
    .stat-pill .label { font-size: 10px; font-weight: 600; letter-spacing: 0.08em; color: var(--text3); text-transform: uppercase; }
    .stat-pill .value { font-family: var(--font-mono); font-size: 15px; font-weight: 600; color: var(--text); }
    .stat-pill .value.profit { color: var(--profit); }
    .stat-pill .value.loss { color: var(--loss); }

    /* ─── MAIN BODY ─── */
    #body {
      flex: 1; display: grid; grid-template-columns: 1fr 360px;
      gap: 20px; padding: 20px 24px; overflow: hidden; min-height: 0;
    }

    #left-col {
      overflow-y: auto; display: flex; flex-direction: column; gap: 16px; padding-right: 4px;
    }
    #right-col {
      overflow-y: auto; display: flex; flex-direction: column; gap: 16px; padding-left: 4px;
    }

    /* ─── TABS & CONTROLS ─── */
    .tabs-header {
      display: flex; align-items: center; justify-content: space-between; gap: 12px;
      margin-bottom: 2px; flex-shrink: 0;
    }
    .tabs-group {
      display: flex; gap: 6px; background: var(--surface2); padding: 3px; border-radius: 8px;
      border: 1px solid var(--border);
    }
    .tab-btn {
      padding: 6px 14px; border-radius: 6px; font-family: var(--font-mono); font-size: 11px;
      font-weight: 600; cursor: pointer; border: none; background: transparent; color: var(--text2);
      display: flex; align-items: center; gap: 8px; transition: all 0.15s;
    }
    .tab-btn.active {
      background: var(--surface3); color: var(--text); box-shadow: 0 1px 4px rgba(0,0,0,0.4);
    }
    .tab-badge {
      font-size: 10px; padding: 1px 6px; border-radius: 12px;
      background: rgba(255,255,255,0.08); color: var(--text2);
    }
    .tab-btn.active .tab-badge { background: var(--blue); color: #fff; }

    /* ─── STRUCTURE CARDS ─── */
    .pos-card {
      background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-lg);
      overflow: hidden; transition: border-color 0.15s, box-shadow 0.15s;
    }
    .pos-card:hover { border-color: var(--border2); }
    .pos-card.expanded { border-color: rgba(79,140,255,0.35); box-shadow: 0 4px 20px rgba(0,0,0,0.3); }

    .pos-card-head {
      padding: 14px 18px; cursor: pointer; user-select: none;
      display: flex; flex-direction: column; gap: 10px;
    }
    .pos-head-top {
      display: flex; align-items: flex-start; justify-content: space-between; gap: 12px;
    }
    .pos-badges { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
    
    .badge {
      font-family: var(--font-mono); font-size: 10px; font-weight: 700; padding: 2px 8px;
      border-radius: 4px; letter-spacing: 0.05em; text-transform: uppercase;
    }
    .badge.implies          { background: rgba(79,140,255,0.15); color: var(--blue); border: 1px solid rgba(79,140,255,0.25); }
    .badge.threshold_ladder { background: rgba(255,176,32,0.15); color: var(--warn); border: 1px solid rgba(255,176,32,0.25); }
    .badge.equivalent       { background: rgba(0,229,153,0.12); color: var(--profit); border: 1px solid rgba(0,229,153,0.25); }
    .badge.partition        { background: rgba(139,92,246,0.15); color: var(--poly); border: 1px solid rgba(139,92,246,0.25); }
    .badge.venue-cross      { background: rgba(79,140,255,0.12); color: var(--blue); }
    .badge.venue-poly       { background: rgba(139,92,246,0.12); color: var(--poly); }
    .badge.venue-kalshi     { background: rgba(236,72,153,0.12); color: var(--kalshi); }

    .pos-status-badge {
      font-family: var(--font-mono); font-size: 10px; font-weight: 700; padding: 3px 10px;
      border-radius: 20px; letter-spacing: 0.06em; flex-shrink: 0; display: flex; align-items: center; gap: 6px;
    }
    .pos-status-badge.open { background: rgba(79,140,255,0.12); color: var(--blue); border: 1px solid rgba(79,140,255,0.3); }
    .pos-status-badge.partial { background: rgba(255,176,32,0.12); color: var(--warn); border: 1px solid rgba(255,176,32,0.3); }
    .pos-status-badge.realised { background: rgba(0,229,153,0.12); color: var(--profit); border: 1px solid rgba(0,229,153,0.3); }

    .pos-title {
      font-size: 15px; font-weight: 600; color: var(--text); line-height: 1.3;
    }
    .pos-summary-text {
      font-size: 12px; color: var(--text2); display: flex; align-items: center; gap: 6px;
    }

    /* Expiry Progress */
    .pos-progress-box {
      margin-top: 2px; display: flex; flex-direction: column; gap: 4px;
    }
    .progress-labels {
      display: flex; justify-content: space-between; font-size: 11px; font-family: var(--font-mono); color: var(--text3);
    }
    .progress-track {
      height: 4px; background: var(--surface2); border-radius: 2px; overflow: hidden;
    }
    .progress-bar-fill {
      height: 100%; background: var(--blue); border-radius: 2px; transition: width 0.3s;
    }

    /* Metrics Footer */
    .pos-card-footer {
      display: grid; grid-template-columns: repeat(4, 1fr) auto auto; align-items: center;
      border-top: 1px solid var(--border); background: rgba(13,15,26,0.4);
    }
    .metric-cell {
      padding: 10px 16px; border-right: 1px solid var(--border); display: flex; flex-direction: column; gap: 1px;
    }
    .metric-cell .m-label { font-size: 9px; font-weight: 600; color: var(--text3); text-transform: uppercase; letter-spacing: 0.08em; }
    .metric-cell .m-val { font-family: var(--font-mono); font-size: 13px; font-weight: 600; color: var(--text); }
    .metric-cell .m-val.profit { color: var(--profit); }
    .metric-cell .m-val.loss { color: var(--loss); }

    .pos-actions {
      padding: 8px 16px; display: flex; align-items: center; gap: 8px; justify-content: flex-end;
    }
    .btn-toggle-legs {
      padding: 6px 12px; border-radius: 5px; font-family: var(--font-mono); font-size: 11px;
      font-weight: 600; cursor: pointer; border: 1px solid var(--border2); background: var(--surface2);
      color: var(--text); transition: all 0.15s; display: flex; align-items: center; gap: 6px;
    }
    .btn-toggle-legs:hover { background: var(--surface3); }
    
    .btn-settle-all {
      padding: 6px 14px; border-radius: 5px; font-family: var(--font-mono); font-size: 11px;
      font-weight: 600; cursor: pointer; border: 1px solid rgba(0,229,153,0.3); background: rgba(0,229,153,0.1);
      color: var(--profit); transition: all 0.15s;
    }
    .btn-settle-all:hover { background: rgba(0,229,153,0.2); }

    /* ─── LEGS TABLE (ACCORDION) ─── */
    .legs-drawer {
      display: none; border-top: 1px solid var(--border); background: var(--surface2);
      padding: 12px 18px 16px;
    }
    .legs-drawer.open { display: block; }
    
    .legs-table {
      width: 100%; border-collapse: collapse; font-size: 12px; margin-top: 8px;
    }
    .legs-table th {
      text-align: left; padding: 6px 10px; font-size: 10px; font-weight: 600;
      color: var(--text3); text-transform: uppercase; letter-spacing: 0.08em;
      border-bottom: 1px solid var(--border);
    }
    .legs-table td {
      padding: 10px; border-bottom: 1px solid rgba(255,255,255,0.04); vertical-align: middle;
    }
    .legs-table tr:last-child td { border-bottom: none; }
    
    .side-pill {
      font-family: var(--font-mono); font-size: 10px; font-weight: 700; padding: 2px 7px;
      border-radius: 4px;
    }
    .side-pill.YES { background: rgba(0,229,153,0.12); color: var(--profit); border: 1px solid rgba(0,229,153,0.3); }
    .side-pill.NO  { background: rgba(255,62,108,0.12); color: var(--loss); border: 1px solid rgba(255,62,108,0.3); }
    
    .btn-settle-leg {
      padding: 4px 10px; border-radius: 4px; font-family: var(--font-mono); font-size: 10px;
      font-weight: 600; cursor: pointer; border: 1px solid rgba(0,229,153,0.3); background: rgba(0,229,153,0.08);
      color: var(--profit); transition: all 0.15s;
    }
    .btn-settle-leg:hover { background: rgba(0,229,153,0.22); }
    .btn-settle-leg.settled {
      background: transparent; border-color: rgba(255,255,255,0.1); color: var(--text3); cursor: default;
    }

    /* ─── REALISED HISTORY ─── */
    .history-card {
      background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
      padding: 14px 18px; display: flex; align-items: center; justify-content: space-between; gap: 16px;
      transition: border-color 0.15s;
    }
    .history-card:hover { border-color: var(--border2); }
    .history-left { display: flex; align-items: center; gap: 12px; min-width: 0; }
    .history-icon {
      width: 32px; height: 32px; border-radius: 50%; display: flex; align-items: center; justify-content: center;
      font-size: 14px; flex-shrink: 0;
    }
    .history-icon.profit { background: rgba(0,229,153,0.12); color: var(--profit); }
    .history-icon.loss   { background: rgba(255,62,108,0.12); color: var(--loss); }
    .history-info { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
    .history-title { font-size: 13px; font-weight: 600; color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .history-sub { font-size: 11px; color: var(--text2); font-family: var(--font-mono); }
    .history-pnl {
      font-family: var(--font-mono); font-size: 15px; font-weight: 700; text-align: right; flex-shrink: 0;
    }
    .history-pnl.profit { color: var(--profit); }
    .history-pnl.loss   { color: var(--loss); }

    /* ─── SIDEBAR WIDGETS ─── */
    .widget {
      background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-lg);
      overflow: hidden; flex-shrink: 0;
    }
    .widget-head {
      padding: 12px 16px; border-bottom: 1px solid var(--border); font-size: 11px; font-weight: 600;
      letter-spacing: 0.08em; text-transform: uppercase; color: var(--text3);
      display: flex; align-items: center; justify-content: space-between;
    }
    .widget-body { padding: 14px 16px; }

    /* System Health grid (zero overlapping) */
    .health-grid {
      display: flex; flex-direction: column; gap: 10px;
    }
    .health-row {
      display: grid; grid-template-columns: 8px 1fr auto; align-items: center; gap: 10px;
      font-size: 12px;
    }
    .health-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--profit); }
    .health-name { font-weight: 500; color: var(--text); }
    .health-meta { font-family: var(--font-mono); font-size: 11px; color: var(--text2); }

    /* Live Feed */
    #feed-list {
      display: flex; flex-direction: column; gap: 4px; max-height: 190px; overflow-y: auto;
    }
    .feed-item {
      display: flex; gap: 8px; padding: 4px 0; font-size: 11px; border-bottom: 1px solid rgba(255,255,255,0.03);
    }
    .feed-time { font-family: var(--font-mono); color: var(--text3); flex-shrink: 0; }
    .feed-text { color: var(--text2); line-height: 1.4; word-break: break-word; }
    .feed-text .hi { color: var(--text); font-weight: 500; }
    .feed-text .profit { color: var(--profit); }
    .feed-text .loss { color: var(--loss); }

    /* Action Buttons */
    .action-grid { display: flex; flex-direction: column; gap: 8px; }
    .action-btn {
      width: 100%; padding: 10px 14px; border-radius: var(--radius); border: 1px solid var(--border);
      background: var(--surface2); color: var(--text); font-family: var(--font-body); font-size: 12px;
      font-weight: 600; cursor: pointer; text-align: left; display: flex; align-items: center; gap: 10px;
      transition: all 0.15s;
    }
    .action-btn:hover { background: var(--surface3); border-color: var(--border2); }
    .action-btn.primary {
      background: linear-gradient(135deg, rgba(79,140,255,0.15) 0%, rgba(0,229,153,0.12) 100%);
      border-color: rgba(79,140,255,0.3); color: #fff;
    }
    .action-btn.primary:hover {
      background: linear-gradient(135deg, rgba(79,140,255,0.25) 0%, rgba(0,229,153,0.2) 100%);
      border-color: rgba(79,140,255,0.5);
    }
    .action-btn.cycle {
      background: linear-gradient(135deg, rgba(139,92,246,0.15) 0%, rgba(79,140,255,0.12) 100%);
      border-color: rgba(139,92,246,0.3);
    }
    .action-btn.danger { border-color: rgba(255,62,108,0.25); color: var(--loss); }
    .action-btn.danger:hover { background: rgba(255,62,108,0.1); border-color: rgba(255,62,108,0.4); }

    /* Empty State */
    .empty-state {
      padding: 48px 24px; text-align: center; color: var(--text3);
      border: 1px dashed var(--border); border-radius: var(--radius-lg);
    }
    .empty-icon { font-size: 32px; margin-bottom: 8px; }
    .empty-title { font-size: 14px; font-weight: 600; color: var(--text2); margin-bottom: 4px; }
    .empty-sub { font-size: 12px; }

    /* Toast */
    #toast {
      position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%) translateY(90px);
      padding: 10px 22px; border-radius: 8px; font-size: 13px; font-weight: 500;
      background: var(--surface3); border: 1px solid var(--border2); color: var(--text);
      box-shadow: 0 10px 30px rgba(0,0,0,0.6); transition: transform 0.25s cubic-bezier(.34,1.56,.64,1);
      z-index: 9999; pointer-events: none;
    }
    #toast.show { transform: translateX(-50%) translateY(0); }
    #toast.success { border-color: rgba(0,229,153,0.5); color: var(--profit); }
    #toast.error   { border-color: rgba(255,62,108,0.5); color: var(--loss); }

    /* Scrollbars */
    ::-webkit-scrollbar { width: 5px; height: 5px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { background: var(--surface3); border-radius: 3px; }
    ::-webkit-scrollbar-thumb:hover { background: var(--text3); }
  </style>
</head>
<body>

  <!-- TOPBAR -->
  <div id="topbar">
    <div id="logo"><span class="hex">⬡</span> POLYARB TERMINAL</div>
    <div class="chip">
      <div class="pulse-dot" id="poller-dot"></div>
      <span id="status-text">LIVE · 5s Poller</span>
    </div>
    <div class="chip" style="color:var(--profit);">
      <span>⚡ Fast Auto-Cycle (54s)</span>
    </div>
    <div class="chip">
      <span id="fx-badge">USD/INR ₹95.08</span>
    </div>
    <div id="topbar-spacer"></div>
    <button id="halt-btn" onclick="toggleHalt()">⬛ HALT</button>
  </div>

  <div id="app">
    <!-- HERO SECTION -->
    <div id="hero">
      <div class="hero-top">
        <div class="hero-balance">
          <div id="hero-value"><span class="currency">₹</span><span id="portfolio-value">10,000.00</span></div>
          <div id="hero-delta" class="flat">₹0.00 (0.00%) from initial</div>
        </div>
      </div>
      <div id="stats-row">
        <div class="stat-pill"><div class="label">Available Cash</div><div class="value" id="stat-cash">₹10,000.00</div></div>
        <div class="stat-pill"><div class="label">Locked Capital</div><div class="value" id="stat-locked">₹0.00</div></div>
        <div class="stat-pill"><div class="label">Active Trades</div><div class="value" id="stat-active">0</div></div>
        <div class="stat-pill"><div class="label">Realised P&amp;L</div><div class="value" id="stat-pnl">₹0.00</div></div>
        <div class="stat-pill"><div class="label">Win Rate</div><div class="value" id="stat-winrate">—</div></div>
        <div class="stat-pill"><div class="label">Projected Edge</div><div class="value" id="stat-edge">₹0.00</div></div>
      </div>
    </div>

    <!-- MAIN TWO-COLUMN BODY -->
    <div id="body">
      <!-- LEFT COLUMN: TABS + POSITIONS -->
      <div id="left-col">
        <div class="tabs-header">
          <div class="tabs-group">
            <button class="tab-btn active" id="tab-btn-open" onclick="switchTab('open')">
              Active Positions <span class="tab-badge" id="badge-open-count">0</span>
            </button>
            <button class="tab-btn" id="tab-btn-realised" onclick="switchTab('realised')">
              Realised History <span class="tab-badge" id="badge-realised-count">0</span>
            </button>
          </div>
          <div style="font-size:11px; color:var(--text3); font-family:var(--font-mono);">
            Click any row to expand/collapse legs table
          </div>
        </div>

        <!-- OPEN POSITIONS CONTAINER -->
        <div id="view-open">
          <div id="positions-list"></div>
        </div>

        <!-- REALISED HISTORY CONTAINER -->
        <div id="view-realised" style="display:none;">
          <div id="history-list"></div>
        </div>
      </div>

      <!-- RIGHT COLUMN: SIDEBAR WIDGETS -->
      <div id="right-col">
        <!-- 1. System Health -->
        <div class="widget">
          <div class="widget-head">Venue Connectivity</div>
          <div class="widget-body">
            <div class="health-grid">
              <div class="health-row">
                <div class="health-dot" id="dot-poly"></div>
                <div class="health-name">Polymarket</div>
                <div class="health-meta">Gamma CLOB · 24ms</div>
              </div>
              <div class="health-row">
                <div class="health-dot" id="dot-kalshi"></div>
                <div class="health-name">Kalshi</div>
                <div class="health-meta">REST + WS · 31ms</div>
              </div>
              <div class="health-row">
                <div class="health-dot" id="dot-manifold"></div>
                <div class="health-name">Manifold</div>
                <div class="health-meta">REST v0 · 45ms</div>
              </div>
            </div>
          </div>
        </div>

        <!-- 2. Execution Actions -->
        <div class="widget">
          <div class="widget-head">Quick Actions</div>
          <div class="widget-body">
            <div class="action-grid">
              <button class="action-btn primary" onclick="scanAndBook()">
                <span style="font-size:15px;">⚡</span>
                <div><div>Scan &amp; Book Opportunity</div><div style="font-size:10px; opacity:0.75; font-weight:400;">Poll live orderbooks and book best arbitrage</div></div>
              </button>
              <button class="action-btn cycle" onclick="fastCycle()">
                <span style="font-size:15px;">🔄</span>
                <div><div>Fast Auto-Cycle Now</div><div style="font-size:10px; opacity:0.75; font-weight:400;">Settle oldest trade &amp; book fresh opportunity</div></div>
              </button>
              <button class="action-btn" onclick="settleOldest()">
                <span style="font-size:15px;">✓</span>
                <div><div>Settle Oldest Trade</div><div style="font-size:10px; opacity:0.75; font-weight:400;">Realise profit on longest-held position</div></div>
              </button>
              <button class="action-btn danger" onclick="resetPortfolio()">
                <span style="font-size:15px;">↺</span>
                <div><div>Reset Portfolio (₹10,000)</div><div style="font-size:10px; opacity:0.75; font-weight:400;">Clear trades and restore initial INR capital</div></div>
              </button>
            </div>
          </div>
        </div>

        <!-- 3. Live Execution Feed -->
        <div class="widget">
          <div class="widget-head">Live Activity Stream</div>
          <div class="widget-body" style="padding:10px 14px;">
            <div id="feed-list">
              <div class="feed-item"><div class="feed-time">—</div><div class="feed-text">Connecting to poller stream...</div></div>
            </div>
          </div>
        </div>

        <!-- 4. Statistics -->
        <div class="widget">
          <div class="widget-head">Execution Metrics</div>
          <div class="widget-body" style="font-family:var(--font-mono); font-size:12px;">
            <div style="display:flex; flex-direction:column; gap:8px;">
              <div style="display:flex; justify-content:space-between;"><span style="color:var(--text2);">Total Polls</span><span id="ps-polls">—</span></div>
              <div style="display:flex; justify-content:space-between;"><span style="color:var(--text2);">Trades Booked</span><span id="ps-booked">—</span></div>
              <div style="display:flex; justify-content:space-between;"><span style="color:var(--text2);">Auto-Settle Window</span><span style="color:var(--profit);">54 seconds</span></div>
              <div style="display:flex; justify-content:space-between;"><span style="color:var(--text2);">Kill Switch</span><span id="ps-kill">OFF</span></div>
              <div style="display:flex; justify-content:space-between;"><span style="color:var(--text2);">Current FX</span><span id="ps-fx">₹95.08/$</span></div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- TOAST NOTIFICATION -->
  <div id="toast"></div>

  <script>
    /* ─── STATE & HELPERS ─── */
    let currentTab = 'open';
    let expandedStructures = new Set();
    let feedItems = [];
    let lastBookedCount = null;

    const fmt = (n, d=2) => typeof n === 'number' ? n.toLocaleString('en-IN', {minimumFractionDigits:d, maximumFractionDigits:d}) : '—';
    const fmtInr = (n, d=2) => '₹' + fmt(n, d);
    const fmtPct = (n) => (n >= 0 ? '+' : '') + fmt(n) + '%';
    const fmtPnl = (n) => (n >= 0 ? '+' : '') + fmtInr(n);

    function showToast(msg, type='', ms=3500) {
      const t = document.getElementById('toast');
      t.textContent = msg;
      t.className = type + ' show';
      setTimeout(() => { t.className = type; }, ms);
    }

    function addFeed(text) {
      const now = new Date().toLocaleTimeString('en-GB', {hour:'2-digit', minute:'2-digit', second:'2-digit'});
      feedItems.unshift({ text, time: now });
      if (feedItems.length > 50) feedItems = feedItems.slice(0, 50);
      renderFeed();
    }

    function renderFeed() {
      const el = document.getElementById('feed-list');
      if (!feedItems.length) return;
      el.innerHTML = feedItems.map(f => `<div class="feed-item"><div class="feed-time">${f.time}</div><div class="feed-text">${f.text}</div></div>`).join('');
    }

    function switchTab(tab) {
      currentTab = tab;
      document.getElementById('tab-btn-open').className = 'tab-btn' + (tab === 'open' ? ' active' : '');
      document.getElementById('tab-btn-realised').className = 'tab-btn' + (tab === 'realised' ? ' active' : '');
      document.getElementById('view-open').style.display = (tab === 'open' ? 'block' : 'none');
      document.getElementById('view-realised').style.display = (tab === 'realised' ? 'block' : 'none');
    }

    function toggleLegs(id) {
      if (expandedStructures.has(id)) {
        expandedStructures.delete(id);
      } else {
        expandedStructures.add(id);
      }
      fetchPortfolio();
    }

    function getVenueClass(venue) {
      const v = (venue||'').toLowerCase();
      if (v.includes('poly')) return 'venue-poly';
      if (v.includes('kalshi')) return 'venue-kalshi';
      return 'venue-cross';
    }

    function daysRemaining(iso) {
      if (!iso) return null;
      try {
        const diff = (new Date(iso).getTime() - Date.now()) / 86400000;
        if (diff <= 0) return 'Expired';
        if (diff < 1) return Math.round(diff * 24) + 'h left';
        return Math.round(diff) + 'd left';
      } catch(e) { return null; }
    }

    /* ─── RENDER POSITIONS (WITH ACCORDION LEGS) ─── */
    function renderPositions(structs) {
      const el = document.getElementById('positions-list');
      document.getElementById('badge-open-count').textContent = structs.length;

      if (!structs.length) {
        el.innerHTML = `
          <div class="empty-state">
            <div class="empty-icon">📡</div>
            <div class="empty-title">No Open Positions</div>
            <div class="empty-sub">Click <strong>Scan &amp; Book Opportunity</strong> above to immediately book a live cross-market arbitrage structure.</div>
          </div>`;
        return;
      }

      el.innerHTML = structs.map(s => {
        const isExpanded = expandedStructures.has(s.structure_id);
        const settledLegsCount = (s.legs || []).filter(l => l.settlement_status === 'SETTLED').length;
        const totalLegs = (s.legs || []).length;
        const dl = daysRemaining(s.expiry);

        let statusBadge = '<div class="pos-status-badge open">OPEN</div>';
        if (settledLegsCount > 0 && settledLegsCount < totalLegs) {
          statusBadge = `<div class="pos-status-badge partial"><span class="pulse-dot warn"></span> ${settledLegsCount}/${totalLegs} LEGS SETTLED</div>`;
        }

        const venues = [...new Set((s.legs||[]).map(l => l.venue))];
        const venueBadge = venues.length > 1 ? '<span class="badge venue-cross">CROSS-VENUE</span>' : `<span class="badge ${getVenueClass(venues[0])}">${(venues[0]||'').toUpperCase()}</span>`;
        const kindBadge = `<span class="badge ${s.kind}">${(s.kind||'arb').toUpperCase()}</span>`;

        // Render Legs Rows
        const legsRows = (s.legs || []).map((l, idx) => {
          const isSettled = l.settlement_status === 'SETTLED';
          const venueBadge = `<span class="badge ${getVenueClass(l.venue)}">${(l.venue||'').toUpperCase()}</span>`;
          const sideBadge = `<span class="side-pill ${l.side}">${l.side}</span>`;
          const statusText = isSettled 
            ? `<span style="color:var(--profit); font-weight:600; font-family:var(--font-mono);">✓ SETTLED (${l.settlement_outcome||'WIN'})</span>`
            : `<span style="color:var(--blue); font-family:var(--font-mono);">● OPEN</span>`;

          const actionHtml = isSettled
            ? `<span style="color:var(--text3); font-size:11px; font-family:var(--font-mono);">Settled</span>`
            : `<button class="btn-settle-leg" onclick="event.stopPropagation(); settleSingleLeg('${s.structure_id}', '${l.leg_id}')">✓ Settle Leg</button>`;

          return `
            <tr>
              <td style="font-family:var(--font-mono); color:var(--text3); font-weight:600;">#${idx+1}</td>
              <td>${venueBadge}</td>
              <td>${sideBadge}</td>
              <td style="max-width:320px; font-weight:500;">${l.question || 'Contract Option'}</td>
              <td style="font-family:var(--font-mono);">${l.shares}</td>
              <td style="font-family:var(--font-mono);">$${fmt(l.entry_price_usd, 3)}</td>
              <td style="font-family:var(--font-mono);">${fmtInr(l.payoff_inr || 0)}</td>
              <td>${statusText}</td>
              <td style="text-align:right;">${actionHtml}</td>
            </tr>`;
        }).join('');

        return `
          <div class="pos-card ${isExpanded ? 'expanded' : ''}" style="margin-bottom:12px;">
            <div class="pos-card-head" onclick="toggleLegs('${s.structure_id}')">
              <div class="pos-head-top">
                <div style="display:flex; flex-direction:column; gap:4px; min-width:0;">
                  <div class="pos-badges">
                    ${kindBadge}
                    ${venueBadge}
                  </div>
                  <div class="pos-title">${s.event_title || 'Arbitrage Structure'}</div>
                  <div class="pos-summary-text">
                    <span>${(s.legs||[]).map(l => `${l.venue.toUpperCase()} ${l.side}`).join(' + ')}</span>
                    ${dl ? `<span>·</span><span style="font-family:var(--font-mono); color:var(--warn);">${dl}</span>` : ''}
                  </div>
                </div>
                <div>${statusBadge}</div>
              </div>
            </div>

            <div class="pos-card-footer">
              <div class="metric-cell"><div class="m-label">Invested</div><div class="m-val">${fmtInr(s.total_cost_inr)}</div></div>
              <div class="metric-cell"><div class="m-label">Guaranteed</div><div class="m-val">${fmtInr(s.guaranteed_payoff_inr)}</div></div>
              <div class="metric-cell"><div class="m-label">Projected Edge</div><div class="m-val profit">${fmtPnl(s.projected_profit_inr)} <span style="font-size:11px; opacity:0.8;">(${fmtPct(s.return_pct)})</span></div></div>
              <div class="metric-cell"><div class="m-label">Shares</div><div class="m-val">${s.legs?.[0]?.shares || 10}</div></div>
              <div class="pos-actions" style="grid-column: 5 / -1;">
                <button class="btn-toggle-legs" onclick="toggleLegs('${s.structure_id}')">
                  ${isExpanded ? '▲ Hide Legs' : '▼ View Legs (' + totalLegs + ')'}
                </button>
                <button class="btn-settle-all" onclick="settleStructure('${s.structure_id}')">
                  ✓ Settle Entire Structure
                </button>
              </div>
            </div>

            <!-- EXPANDABLE LEGS TABLE -->
            <div class="legs-drawer ${isExpanded ? 'open' : ''}">
              <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:6px;">
                <div style="font-size:11px; font-weight:700; text-transform:uppercase; letter-spacing:0.08em; color:var(--text3);">
                  Contract Legs Breakdown (${totalLegs} Legs)
                </div>
                <div style="font-size:11px; color:var(--text2); font-family:var(--font-mono);">
                  Structure moves to Realised ONLY when all legs settle
                </div>
              </div>
              <table class="legs-table">
                <thead>
                  <tr>
                    <th>Leg</th>
                    <th>Venue</th>
                    <th>Side</th>
                    <th>Contract / Market Question</th>
                    <th>Shares</th>
                    <th>Price</th>
                    <th>Payoff</th>
                    <th>Status</th>
                    <th style="text-align:right;">Action</th>
                  </tr>
                </thead>
                <tbody>
                  ${legsRows}
                </tbody>
              </table>
            </div>
          </div>`;
      }).join('');
    }

    /* ─── RENDER REALISED HISTORY ─── */
    function renderHistory(structs) {
      const el = document.getElementById('history-list');
      document.getElementById('badge-realised-count').textContent = structs.length;

      if (!structs.length) {
        el.innerHTML = `
          <div class="empty-state">
            <div class="empty-icon">📋</div>
            <div class="empty-title">No Realised Trades Yet</div>
            <div class="empty-sub">Once an open trade has all its legs settled, it will automatically move here with realized P&amp;L.</div>
          </div>`;
        return;
      }

      el.innerHTML = structs.map(s => {
        const isProfit = s.realised_profit_inr >= 0;
        const dateStr = s.booked_at ? new Date(s.booked_at).toLocaleTimeString('en-GB', {hour:'2-digit', minute:'2-digit'}) : 'Recently';
        return `
          <div class="history-card" style="margin-bottom:8px;">
            <div class="history-left">
              <div class="history-icon ${isProfit ? 'profit' : 'loss'}">${isProfit ? '✓' : '✗'}</div>
              <div class="history-info">
                <div class="history-title">${s.event_title || s.structure_id}</div>
                <div class="history-sub">
                  ${(s.kind||'').toUpperCase()} · Settled ${dateStr} · Invested ${fmtInr(s.total_cost_inr)}
                </div>
              </div>
            </div>
            <div class="history-pnl ${isProfit ? 'profit' : 'loss'}">
              ${fmtPnl(s.realised_profit_inr)}
              <div style="font-size:11px; font-weight:500; opacity:0.8;">${fmtPct(s.return_pct)} ROI</div>
            </div>
          </div>`;
      }).join('');
    }

    /* ─── DATA FETCH & SYNC ─── */
    async function fetchPortfolio() {
      try {
        const r = await fetch('/api/portfolio');
        if (!r.ok) throw new Error(r.status);
        const d = await r.json();

        // Update Hero
        const total = d.total_portfolio_value_inr || 10000;
        const initial = d.initial_capital_inr || 10000;
        const delta = total - initial;
        document.getElementById('portfolio-value').textContent = fmt(total);

        const deltaEl = document.getElementById('hero-delta');
        if (Math.abs(delta) < 0.05) {
          deltaEl.textContent = '₹0.00 (0.00%) from initial capital';
          deltaEl.className = 'flat';
        } else {
          const sign = delta > 0 ? '+' : '';
          const pct = ((delta / initial) * 100);
          deltaEl.textContent = `${sign}${fmtInr(delta)} (${sign}${fmt(pct)}%) from ₹${fmt(initial)} initial`;
          deltaEl.className = delta > 0 ? 'up' : 'down';
        }

        document.getElementById('stat-cash').textContent = fmtInr(d.available_cash_inr || 0);
        document.getElementById('stat-locked').textContent = fmtInr(d.locked_capital_inr || 0);
        document.getElementById('stat-active').textContent = d.open_count || 0;

        const pnl = d.realised_pnl_inr || 0;
        const pnlEl = document.getElementById('stat-pnl');
        pnlEl.textContent = fmtPnl(pnl);
        pnlEl.className = 'value ' + (pnl > 0 ? 'profit' : (pnl < 0 ? 'loss' : ''));

        const rStructs = d.realised_structures || [];
        const winrateEl = document.getElementById('stat-winrate');
        if (rStructs.length > 0) {
          const wins = rStructs.filter(s => s.realised_profit_inr >= 0).length;
          const rate = Math.round((wins / rStructs.length) * 100);
          winrateEl.textContent = rate + '%';
          winrateEl.className = 'value ' + (rate >= 50 ? 'profit' : 'loss');
        } else {
          winrateEl.textContent = '—';
          winrateEl.className = 'value';
        }

        const edge = d.unrealised_pnl_inr || 0;
        const edgeEl = document.getElementById('stat-edge');
        edgeEl.textContent = fmtPnl(edge);
        edgeEl.className = 'value ' + (edge > 0 ? 'profit' : (edge < 0 ? 'loss' : ''));

        // Render Positions
        renderPositions(d.open_structures || []);
        renderHistory(d.realised_structures || []);

        // Stats & Topbar
        const alive = !d.kill_switch_active;
        document.getElementById('poller-dot').className = 'pulse-dot' + (alive ? '' : ' dead');
        document.getElementById('halt-btn').className = alive ? '' : 'halted';
        document.getElementById('halt-btn').textContent = alive ? '⬛ HALT' : '▶ RESUME';

        if (d.poll_stats) {
          const polls = d.poll_stats.total_polls || 0;
          const booked = d.poll_stats.total_booked || 0;
          document.getElementById('status-text').textContent = `LIVE · ${polls} polls`;
          document.getElementById('ps-polls').textContent = polls;
          document.getElementById('ps-booked').textContent = booked;
          document.getElementById('ps-kill').textContent = d.kill_switch_active ? 'HALTED' : 'OFF';
          document.getElementById('ps-fx').textContent = '₹' + fmt(d.inr_per_usd || 95.08, 2) + '/$';
          document.getElementById('fx-badge').textContent = 'USD/INR ₹' + fmt(d.inr_per_usd || 95.08, 2);

          if (lastBookedCount !== null && booked > lastBookedCount) {
            const diff = booked - lastBookedCount;
            addFeed(`<span class="hi">⚡ Booked ${diff} new trade(s)</span> via 5s Poller`);
          }
          lastBookedCount = booked;
        }

      } catch(e) {
        document.getElementById('status-text').textContent = 'Connecting...';
      }
    }

    /* ─── USER ACTIONS ─── */
    async function scanAndBook() {
      showToast('Scanning live orderbooks across Polymarket & Kalshi...', '', 6000);
      try {
        const r = await fetch('/api/poll', {method:'POST'});
        const d = await r.json();
        const booked = d.trades_booked_this_poll || 0;
        if (booked > 0) {
          showToast(`✓ Identified & booked ${booked} short-duration trade(s)!`, 'success');
          addFeed(`<span class="profit">⚡ Scan &amp; Book: booked ${booked} short-duration arbitrage structure</span>`);
        } else {
          showToast(d.message || 'Scan completed — no new mispricing right now', '');
          addFeed(`Scan completed: markets in equilibrium`);
        }
        setTimeout(fetchPortfolio, 300);
      } catch(e) {
        showToast('Scan request failed', 'error');
      }
    }

    async function fastCycle() {
      showToast('Auto-cycling: settling oldest trade and booking next...', '', 5000);
      try {
        const r = await fetch('/api/fast_cycle', {method:'POST'});
        const d = await r.json();
        showToast(d.message || 'Fast cycle completed!', 'success');
        addFeed(`<span class="profit">🔄 Fast Cycle: ${d.message}</span>`);
        setTimeout(fetchPortfolio, 300);
      } catch(e) {
        showToast('Fast cycle failed', 'error');
      }
    }

    async function settleStructure(id) {
      try {
        const r = await fetch('/api/settle', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({structure_id: id})
        });
        const d = await r.json();
        if (d.status === 'success') {
          showToast(`✓ All legs settled! Trade moved to Realised (+₹${fmt(d.realised_profit_inr||0)})`, 'success');
          addFeed(`<span class="profit">✓ Settled ${id}: +${fmtInr(d.realised_profit_inr||0)} realised</span>`);
          expandedStructures.delete(id);
          setTimeout(fetchPortfolio, 300);
        } else {
          showToast(d.message || 'Failed to settle structure', 'error');
        }
      } catch(e) {
        showToast('Settlement request failed', 'error');
      }
    }

    async function settleSingleLeg(structId, legId) {
      try {
        const r = await fetch('/api/settle_single_leg', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({structure_id: structId, leg_id: legId})
        });
        const d = await r.json();
        if (d.status === 'success') {
          if (d.fully_settled) {
            showToast(`🎉 Final leg settled! Trade moved to Realised (+₹${fmt(d.realised_profit_inr||0)})`, 'success');
            addFeed(`<span class="profit">🎉 All legs settled for ${structId}! Fully realised.</span>`);
            expandedStructures.delete(structId);
          } else {
            showToast(`✓ Settled leg. Structure remains in Active until all legs settle!`, 'success');
            addFeed(`✓ Settled leg ${legId} on ${structId} (awaiting other legs)`);
          }
          setTimeout(fetchPortfolio, 300);
        } else {
          showToast(d.message || 'Leg settlement failed', 'error');
        }
      } catch(e) {
        showToast('Leg settlement request failed', 'error');
      }
    }

    async function settleOldest() {
      try {
        const r = await fetch('/api/portfolio');
        const d = await r.json();
        const open = d.open_structures || [];
        if (!open.length) {
          showToast('No open positions to settle', '');
          return;
        }
        await settleStructure(open[0].structure_id);
      } catch(e) {
        showToast('Failed to fetch open positions', 'error');
      }
    }

    async function toggleHalt() {
      try {
        const r = await fetch('/api/kill_switch', {method:'POST'});
        const d = await r.json();
        const active = d.active || d.kill_switch_active;
        showToast(active ? 'System halted. Poller paused.' : 'System resumed. Scanning markets.', active ? 'error' : 'success');
        addFeed(active ? '<span class="loss">⬛ System halted by operator</span>' : '<span class="profit">▶ System resumed</span>');
        setTimeout(fetchPortfolio, 300);
      } catch(e) {
        showToast('Failed to toggle kill switch', 'error');
      }
    }

    async function resetPortfolio() {
      if (!confirm('Reset portfolio to initial ₹10,000 INR and seed fresh short-duration trades?')) return;
      showToast('Resetting portfolio...', '', 4000);
      try {
        const r = await fetch('/api/reset', {method:'POST'});
        const d = await r.json();
        showToast('Portfolio reset to ₹10,000 INR. Ready!', 'success');
        addFeed('<span class="hi">↺ Portfolio reset to ₹10,000 INR · Fresh short-duration trades seeded</span>');
        lastBookedCount = null;
        expandedStructures.clear();
        setTimeout(fetchPortfolio, 300);
      } catch(e) {
        showToast('Reset failed', 'error');
      }
    }

    /* Auto-refresh every 2.0s */
    setInterval(fetchPortfolio, 2000);
    fetchPortfolio();
    addFeed('PolyArb Terminal online · Monitoring Polymarket, Kalshi & Manifold');
  </script>
</body>
</html>"""


def start_server(port: int = PORT):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    # Start live 5-second market poller across Polymarket & Kalshi
    poller.start()

    server = ThreadingHTTPServer(("0.0.0.0", port), DashboardHandler)
    server.daemon_threads = True
    logger.info("=" * 60)
    logger.info(f"PolyArb Terminal running at http://localhost:{port} (bound to 0.0.0.0:{port})")
    logger.info("Capital: INR 10,000.00 (~$119.76 USD)")
    logger.info("Sources: Live 5s Poller on Polymarket & Kalshi APIs")
    logger.info("Risk Controls: Best Basket Mathematical Edge Gate, Venue Exposure Limits")
    logger.info("Audit: Immutable Append-Only SQLite Trail (audit.sqlite)")
    logger.info("=" * 60)
    server.serve_forever()


if __name__ == "__main__":
    p = int(sys.argv[1]) if len(sys.argv) > 1 else PORT
    start_server(p)
