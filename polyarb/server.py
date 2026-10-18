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

        if parsed.path in ("/api/poll_now", "/api/poll"):
            # Force immediate live poll tick
            res = poller.poll_once()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({
                "status": "success",
                "message": f"Polled Polymarket ({res.get('poly_count',0)}) & Kalshi ({res.get('kalshi_count',0)}). Found {res.get('candidates_found',0)} candidate pairs.",
                "poll_result": res,
                "trades_booked_this_poll": res.get("booked_this_tick", 0),
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
                res = {"status": "success", "message": f"Settled all legs of {struct_id}. Trade moved to Realised!", "realised_profit_inr": pnl}
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
                res = {"status": "success", "message": f"Settled leg {leg_id}. Notice: Structure remains in Open Positions until ALL legs settle!"}
                audit_log.log(action="LEG_SETTLED", venue="portfolio", order_id=f"{struct_id}/{leg_id}", status="SETTLED")
            else:
                res = {"status": "error", "message": "No unsettled legs available"}

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(res).encode("utf-8"))

        elif parsed.path == "/api/book_next":
            # Phase 0.5: No template fallback. Only book mathematically verified live mispricings!
            res = poller.poll_once()
            if res.get("booked_this_tick", 0) > 0:
                msg = f"Live 5s Poller identified and booked {res['booked_this_tick']} verified positive-edge trade(s)!"
            else:
                msg = f"Live scan completed ({res.get('poly_count',0)} Poly / {res.get('kalshi_count',0)} Kalshi). No mispricings meeting edge gate (min {poller.cfg.risk.min_edge*100:.1f}%) right now."

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "success", "message": msg}).encode("utf-8"))

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
  <title>PolyArb</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:ital,wght@0,400;0,500;0,600;0,700;1,400&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    :root {
      --bg:       #070810;
      --surface:  #0D0F1A;
      --surface2: #131628;
      --surface3: #191C30;
      --border:   rgba(255,255,255,0.06);
      --border2:  rgba(255,255,255,0.10);
      --profit:   #00D68A;
      --loss:     #FF3E6C;
      --warn:     #FFB020;
      --blue:     #4F8CFF;
      --text:     #E8E8F2;
      --text2:    #8080A0;
      --text3:    #4A4A64;
      --poly:     #7B5CF0;
      --kalshi:   #E84B8A;
      --manifold: #00C87A;
      --font-mono: 'JetBrains Mono', 'Fira Code', monospace;
      --font-body: 'Inter', system-ui, sans-serif;
      --radius:   8px;
      --radius-lg: 12px;
    }

    html, body { height: 100%; background: var(--bg); color: var(--text); font-family: var(--font-body); font-size: 14px; line-height: 1.5; }

    /* ─── TOPBAR ─── */
    #topbar {
      position: fixed; top: 0; left: 0; right: 0; height: 52px; z-index: 100;
      background: rgba(7,8,16,0.92); backdrop-filter: blur(12px);
      border-bottom: 1px solid var(--border);
      display: flex; align-items: center; padding: 0 20px; gap: 16px;
    }
    #logo {
      font-family: var(--font-mono); font-weight: 700; font-size: 16px; letter-spacing: 0.08em;
      color: var(--text); display: flex; align-items: center; gap: 8px; flex-shrink: 0;
    }
    #logo .hex { color: var(--blue); font-size: 18px; }
    #status-chip {
      display: flex; align-items: center; gap: 7px; padding: 4px 12px; border-radius: 20px;
      background: var(--surface2); border: 1px solid var(--border2);
      font-family: var(--font-mono); font-size: 11px; color: var(--text2); flex-shrink: 0;
    }
    .pulse-dot {
      width: 7px; height: 7px; border-radius: 50%; background: var(--profit);
      animation: pulse 2s infinite;
    }
    .pulse-dot.dead { background: var(--loss); animation: none; }
    @keyframes pulse {
      0%, 100% { opacity: 1; transform: scale(1); }
      50% { opacity: 0.55; transform: scale(0.85); }
    }
    #topbar-spacer { flex: 1; }
    #halt-btn {
      padding: 7px 18px; border-radius: 6px; font-family: var(--font-mono); font-size: 12px;
      font-weight: 600; letter-spacing: 0.05em; cursor: pointer; border: none;
      background: rgba(255,62,108,0.15); color: var(--loss); border: 1px solid rgba(255,62,108,0.3);
      transition: all 0.15s; flex-shrink: 0;
    }
    #halt-btn:hover { background: rgba(255,62,108,0.25); }
    #halt-btn.halted { background: rgba(0,214,138,0.12); color: var(--profit); border-color: rgba(0,214,138,0.3); }

    /* ─── LAYOUT ─── */
    #app { padding-top: 52px; height: 100%; display: flex; flex-direction: column; }

    /* ─── HERO ─── */
    #hero {
      padding: 28px 24px 20px;
      background: linear-gradient(180deg, rgba(79,140,255,0.04) 0%, transparent 100%);
      border-bottom: 1px solid var(--border);
      flex-shrink: 0;
    }
    #hero-value {
      font-family: var(--font-mono); font-size: 40px; font-weight: 700; letter-spacing: -0.02em;
      line-height: 1; color: var(--text); margin-bottom: 4px;
    }
    #hero-value .currency { font-size: 22px; color: var(--text2); margin-right: 2px; }
    #hero-delta {
      font-family: var(--font-mono); font-size: 14px; font-weight: 500; margin-bottom: 18px;
    }
    #hero-delta.up { color: var(--profit); }
    #hero-delta.down { color: var(--loss); }
    #hero-delta.flat { color: var(--text2); }
    #stats-row { display: flex; flex-wrap: wrap; gap: 10px; }
    .stat-pill {
      display: flex; flex-direction: column; gap: 2px; padding: 8px 14px;
      background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
      min-width: 110px;
    }
    .stat-pill .label { font-size: 10px; font-weight: 600; letter-spacing: 0.08em; color: var(--text3); text-transform: uppercase; }
    .stat-pill .value { font-family: var(--font-mono); font-size: 15px; font-weight: 600; color: var(--text); }
    .stat-pill .value.profit { color: var(--profit); }
    .stat-pill .value.loss { color: var(--loss); }

    /* ─── BODY ─── */
    #body {
      flex: 1; display: grid; grid-template-columns: 1fr 340px;
      gap: 0; overflow: hidden; min-height: 0;
    }
    #left-col { overflow-y: auto; padding: 20px; display: flex; flex-direction: column; gap: 20px; }
    #right-col { overflow-y: auto; padding: 20px; display: flex; flex-direction: column; gap: 16px;
      border-left: 1px solid var(--border); background: var(--surface); }

    /* ─── SECTION HEADER ─── */
    .section-title {
      font-size: 11px; font-weight: 600; letter-spacing: 0.1em; text-transform: uppercase;
      color: var(--text3); margin-bottom: 12px; display: flex; align-items: center; gap: 8px;
    }
    .section-title .badge {
      font-family: var(--font-mono); font-size: 11px; padding: 1px 7px; border-radius: 20px;
      background: var(--surface2); color: var(--text2); border: 1px solid var(--border);
    }

    /* ─── POSITION CARDS ─── */
    .pos-card {
      background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-lg);
      overflow: hidden; transition: border-color 0.15s;
    }
    .pos-card:hover { border-color: var(--border2); }
    .pos-card-header {
      display: flex; align-items: flex-start; justify-content: space-between;
      padding: 14px 16px 10px; gap: 12px;
    }
    .pos-card-meta { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
    .pos-card-badges { display: flex; gap: 6px; flex-wrap: wrap; }
    .badge-kind {
      font-family: var(--font-mono); font-size: 10px; font-weight: 600; padding: 2px 8px;
      border-radius: 4px; letter-spacing: 0.05em; text-transform: uppercase;
    }
    .badge-kind.implies  { background: rgba(79,140,255,0.12); color: var(--blue); }
    .badge-kind.equivalent { background: rgba(0,214,138,0.1); color: var(--profit); }
    .badge-kind.partition { background: rgba(123,92,240,0.12); color: var(--poly); }
    .badge-kind.threshold_ladder { background: rgba(255,176,32,0.1); color: var(--warn); }
    .badge-venue {
      font-family: var(--font-mono); font-size: 10px; padding: 2px 8px; border-radius: 4px;
    }
    .badge-venue.polymarket { background: rgba(123,92,240,0.1); color: var(--poly); }
    .badge-venue.kalshi { background: rgba(232,75,138,0.1); color: var(--kalshi); }
    .badge-venue.cross { background: rgba(79,140,255,0.1); color: var(--blue); }
    .pos-card-title {
      font-size: 14px; font-weight: 600; color: var(--text); line-height: 1.3;
      white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 480px;
    }
    .pos-card-legs { font-size: 12px; color: var(--text2); margin-top: 2px; }
    .pos-status-badge {
      font-family: var(--font-mono); font-size: 10px; font-weight: 700; padding: 3px 9px;
      border-radius: 20px; letter-spacing: 0.06em; white-space: nowrap; flex-shrink: 0;
    }
    .pos-status-badge.open { background: rgba(79,140,255,0.1); color: var(--blue); border: 1px solid rgba(79,140,255,0.2); }
    .pos-status-badge.realised.profit { background: rgba(0,214,138,0.1); color: var(--profit); border: 1px solid rgba(0,214,138,0.2); }
    .pos-status-badge.realised.loss { background: rgba(255,62,108,0.1); color: var(--loss); border: 1px solid rgba(255,62,108,0.2); }

    .pos-card-progress { padding: 0 16px 10px; }
    .progress-meta { display: flex; justify-content: space-between; font-size: 11px; color: var(--text2); margin-bottom: 5px; }
    .progress-bar { height: 4px; background: var(--surface2); border-radius: 2px; overflow: hidden; }
    .progress-fill { height: 100%; border-radius: 2px; background: var(--blue); transition: width 0.4s; }
    .progress-fill.expiring { background: var(--warn); }
    .progress-fill.critical { background: var(--loss); }

    .pos-card-footer {
      display: grid; grid-template-columns: repeat(4, 1fr); gap: 0;
      border-top: 1px solid var(--border); padding: 0 0 0 0;
    }
    .pos-metric {
      padding: 10px 16px; border-right: 1px solid var(--border); display: flex; flex-direction: column; gap: 2px;
    }
    .pos-metric:last-child { border-right: none; }
    .pos-metric .m-label { font-size: 10px; color: var(--text3); text-transform: uppercase; letter-spacing: 0.07em; }
    .pos-metric .m-value { font-family: var(--font-mono); font-size: 13px; font-weight: 600; color: var(--text); }
    .pos-metric .m-value.profit { color: var(--profit); }
    .pos-metric .m-value.loss { color: var(--loss); }
    .settle-row { grid-column: 1 / -1; border-top: 1px solid var(--border); padding: 8px 16px; display: flex; justify-content: flex-end; }
    .settle-btn {
      font-family: var(--font-mono); font-size: 11px; font-weight: 600; padding: 5px 14px;
      border-radius: 5px; border: 1px solid rgba(0,214,138,0.25); background: rgba(0,214,138,0.06);
      color: var(--profit); cursor: pointer; transition: all 0.15s; letter-spacing: 0.04em;
    }
    .settle-btn:hover { background: rgba(0,214,138,0.14); border-color: rgba(0,214,138,0.4); }

    /* ─── REALISED HISTORY ─── */
    .history-row {
      display: flex; align-items: center; gap: 10px; padding: 10px 14px;
      background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
      transition: border-color 0.15s;
    }
    .history-row:hover { border-color: var(--border2); }
    .history-icon { width: 32px; height: 32px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 14px; flex-shrink: 0; }
    .history-icon.profit { background: rgba(0,214,138,0.1); }
    .history-icon.loss   { background: rgba(255,62,108,0.1); }
    .history-body { flex: 1; min-width: 0; }
    .history-title { font-size: 13px; font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .history-sub { font-size: 11px; color: var(--text2); }
    .history-pnl { font-family: var(--font-mono); font-size: 14px; font-weight: 700; flex-shrink: 0; }
    .history-pnl.profit { color: var(--profit); }
    .history-pnl.loss   { color: var(--loss); }

    /* ─── RIGHT SIDEBAR ─── */
    .widget { background: var(--surface2); border: 1px solid var(--border); border-radius: var(--radius-lg); overflow: hidden; }
    .widget-head { padding: 12px 16px; border-bottom: 1px solid var(--border); font-size: 11px; font-weight: 600; letter-spacing: 0.09em; text-transform: uppercase; color: var(--text3); display: flex; align-items: center; justify-content: space-between; }
    .widget-body { padding: 14px 16px; }

    /* System Health */
    .venue-row { display: flex; align-items: center; gap: 10px; padding: 6px 0; }
    .venue-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
    .venue-dot.live { background: var(--profit); }
    .venue-dot.warn { background: var(--warn); }
    .venue-dot.dead { background: var(--loss); }
    .venue-name { font-size: 13px; font-weight: 500; flex: 1; }
    .venue-latency { font-family: var(--font-mono); font-size: 11px; color: var(--text2); }
    .venue-sub { font-size: 11px; color: var(--text3); }

    /* Activity feed */
    #feed-list { display: flex; flex-direction: column; gap: 2px; max-height: 220px; overflow-y: auto; }
    .feed-item { display: flex; gap: 8px; padding: 6px 0; border-bottom: 1px solid var(--border); font-size: 12px; }
    .feed-item:last-child { border-bottom: none; }
    .feed-time { font-family: var(--font-mono); color: var(--text3); white-space: nowrap; flex-shrink: 0; }
    .feed-text { color: var(--text2); line-height: 1.4; }
    .feed-text .hi { color: var(--text); }
    .feed-text .profit { color: var(--profit); }
    .feed-text .loss { color: var(--loss); }

    /* Quick Actions */
    .action-btn {
      width: 100%; padding: 10px 16px; border-radius: var(--radius); border: 1px solid var(--border);
      background: var(--surface3); color: var(--text); font-family: var(--font-body); font-size: 13px;
      font-weight: 500; cursor: pointer; text-align: left; display: flex; align-items: center; gap: 10px;
      transition: all 0.15s; margin-bottom: 8px;
    }
    .action-btn:last-child { margin-bottom: 0; }
    .action-btn:hover { background: var(--surface2); border-color: var(--border2); }
    .action-btn .action-icon { font-size: 16px; flex-shrink: 0; }
    .action-btn .action-label { display: flex; flex-direction: column; gap: 1px; }
    .action-btn .action-sub { font-size: 11px; color: var(--text2); font-weight: 400; }
    .action-btn.danger { border-color: rgba(255,62,108,0.2); }
    .action-btn.danger:hover { background: rgba(255,62,108,0.06); border-color: rgba(255,62,108,0.35); }

    /* ─── EMPTY STATE ─── */
    .empty-state {
      padding: 40px 20px; text-align: center; color: var(--text3);
      border: 1px dashed var(--border); border-radius: var(--radius-lg);
    }
    .empty-state .empty-icon { font-size: 28px; margin-bottom: 10px; }
    .empty-state .empty-title { font-size: 14px; font-weight: 600; color: var(--text2); margin-bottom: 6px; }
    .empty-state .empty-sub { font-size: 12px; line-height: 1.5; }

    /* ─── TOAST ─── */
    #toast {
      position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%) translateY(80px);
      padding: 10px 20px; border-radius: 8px; font-size: 13px; font-weight: 500;
      background: var(--surface2); border: 1px solid var(--border2); color: var(--text);
      box-shadow: 0 8px 32px rgba(0,0,0,0.5); transition: transform 0.25s cubic-bezier(.34,1.56,.64,1); z-index: 999;
    }
    #toast.show { transform: translateX(-50%) translateY(0); }
    #toast.success { border-color: rgba(0,214,138,0.4); color: var(--profit); }
    #toast.error   { border-color: rgba(255,62,108,0.4); color: var(--loss); }

    /* ─── SCROLLBARS ─── */
    ::-webkit-scrollbar { width: 5px; height: 5px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { background: var(--surface3); border-radius: 3px; }
    ::-webkit-scrollbar-thumb:hover { background: var(--text3); }
  </style>
</head>
<body>
  <!-- TOPBAR -->
  <div id="topbar">
    <div id="logo"><span class="hex">⬡</span> POLYARB</div>
    <div id="status-chip">
      <div class="pulse-dot" id="poller-dot"></div>
      <span id="status-text">Connecting...</span>
    </div>
    <div id="topbar-spacer"></div>
    <button id="halt-btn" onclick="toggleHalt()">⬛ HALT</button>
  </div>

  <div id="app">
    <!-- HERO -->
    <div id="hero">
      <div id="hero-value"><span class="currency">₹</span><span id="portfolio-value">—</span></div>
      <div id="hero-delta" class="flat">Portfolio Value</div>
      <div id="stats-row">
        <div class="stat-pill"><div class="label">Cash</div><div class="value" id="stat-cash">—</div></div>
        <div class="stat-pill"><div class="label">Locked</div><div class="value" id="stat-locked">—</div></div>
        <div class="stat-pill"><div class="label">Active</div><div class="value" id="stat-active">—</div></div>
        <div class="stat-pill"><div class="label">Realised P&L</div><div class="value" id="stat-pnl">—</div></div>
        <div class="stat-pill"><div class="label">Win Rate</div><div class="value" id="stat-winrate">—</div></div>
        <div class="stat-pill"><div class="label">Projected Edge</div><div class="value" id="stat-edge">—</div></div>
      </div>
    </div>

    <!-- BODY -->
    <div id="body">
      <!-- LEFT: POSITIONS + HISTORY -->
      <div id="left-col">
        <!-- Active Positions -->
        <div>
          <div class="section-title">Active Positions <span class="badge" id="open-count-badge">0</span></div>
          <div id="positions-list"></div>
        </div>
        <!-- Realised History -->
        <div>
          <div class="section-title">Realised History <span class="badge" id="hist-count-badge">0</span></div>
          <div id="history-list"></div>
        </div>
      </div>

      <!-- RIGHT SIDEBAR -->
      <div id="right-col">
        <!-- System Health -->
        <div class="widget">
          <div class="widget-head">System Health</div>
          <div class="widget-body" id="health-widget">
            <div class="venue-row"><div class="venue-dot" id="dot-poly"></div><div class="venue-name">Polymarket</div><div class="venue-latency" id="lat-poly">—</div></div>
            <div class="venue-row"><div class="venue-dot" id="dot-kalshi"></div><div class="venue-name">Kalshi</div><div class="venue-latency" id="lat-kalshi">—</div></div>
            <div class="venue-row"><div class="venue-dot" id="dot-manifold"></div><div class="venue-name">Manifold</div><div class="venue-latency" id="lat-manifold">—</div></div>
          </div>
        </div>

        <!-- Live Activity Feed -->
        <div class="widget">
          <div class="widget-head">Live Activity</div>
          <div class="widget-body" style="padding:8px 16px 12px">
            <div id="feed-list"><div class="feed-item"><div class="feed-time">—</div><div class="feed-text">Waiting for activity...</div></div></div>
          </div>
        </div>

        <!-- Quick Actions -->
        <div class="widget">
          <div class="widget-head">Actions</div>
          <div class="widget-body">
            <button class="action-btn" onclick="scanAndBook()">
              <span class="action-icon">⚡</span>
              <div class="action-label"><span>Scan &amp; Book</span><span class="action-sub">Poll markets and book best opportunity</span></div>
            </button>
            <button class="action-btn" onclick="settleOldest()">
              <span class="action-icon">✓</span>
              <div class="action-label"><span>Settle Oldest</span><span class="action-sub">Settle the longest-open position</span></div>
            </button>
            <button class="action-btn danger" onclick="resetPortfolio()">
              <span class="action-icon">↺</span>
              <div class="action-label"><span>Reset Portfolio</span><span class="action-sub">Restore ₹10,000 capital, clear all trades</span></div>
            </button>
          </div>
        </div>

        <!-- Poll Stats -->
        <div class="widget">
          <div class="widget-head">Poll Statistics</div>
          <div class="widget-body" id="poll-stats">
            <div style="display:flex;flex-direction:column;gap:8px;">
              <div style="display:flex;justify-content:space-between;font-size:12px;"><span style="color:var(--text2)">Total Polls</span><span class="value" id="ps-polls" style="font-family:var(--font-mono)">—</span></div>
              <div style="display:flex;justify-content:space-between;font-size:12px;"><span style="color:var(--text2)">Trades Booked</span><span style="font-family:var(--font-mono)" id="ps-booked">—</span></div>
              <div style="display:flex;justify-content:space-between;font-size:12px;"><span style="color:var(--text2)">Kill Switch</span><span style="font-family:var(--font-mono)" id="ps-kill">—</span></div>
              <div style="display:flex;justify-content:space-between;font-size:12px;"><span style="color:var(--text2)">FX Rate</span><span style="font-family:var(--font-mono)" id="ps-fx">—</span></div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- TOAST -->
  <div id="toast"></div>

  <script>
    /* ── helpers ── */
    const fmt = (n, d=2) => typeof n==='number' ? n.toLocaleString('en-IN', {minimumFractionDigits:d, maximumFractionDigits:d}) : '—';
    const fmtInr = (n, d=2) => '₹' + fmt(n, d);
    const fmtPct = (n) => (n>=0?'+':'')+fmt(n)+'%';
    const fmtPnl = (n) => (n>=0?'+':'')+fmtInr(n);
    const cls = (n, pos='profit', neg='loss') => n > 0 ? pos : n < 0 ? neg : '';

    let feedItems = [];
    let lastPortfolioValue = null;

    function showToast(msg, type='', ms=3000) {
      const t = document.getElementById('toast');
      t.textContent = msg;
      t.className = type + ' show';
      setTimeout(() => { t.className = type; }, ms);
    }

    function addFeedItem(text, timeStr) {
      const now = timeStr || new Date().toLocaleTimeString('en-GB', {hour:'2-digit', minute:'2-digit', second:'2-digit'});
      feedItems.unshift({ text, time: now });
      if (feedItems.length > 40) feedItems = feedItems.slice(0, 40);
      renderFeed();
    }

    function renderFeed() {
      const el = document.getElementById('feed-list');
      if (!feedItems.length) { el.innerHTML = '<div class="feed-item"><div class="feed-time">—</div><div class="feed-text">No activity yet</div></div>'; return; }
      el.innerHTML = feedItems.map(f => `<div class="feed-item"><div class="feed-time">${f.time}</div><div class="feed-text">${f.text}</div></div>`).join('');
    }

    /* ── position & history rendering ── */
    function kindLabel(k) {
      return {implies:'IMPLIED', equivalent:'EQUIV', partition:'PARTITION', threshold_ladder:'LADDER'}[k] || k.toUpperCase();
    }

    function venueLabel(legs) {
      const venues = [...new Set((legs||[]).map(l=>l.venue))];
      if (venues.length > 1) return 'CROSS';
      return (venues[0]||'poly').toUpperCase().replace('POLYMARKET','POLY');
    }

    function venueBadgeClass(legs) {
      const venues = [...new Set((legs||[]).map(l=>l.venue))];
      if (venues.length > 1) return 'cross';
      return venues[0] || 'polymarket';
    }

    function progressPct(expiry) {
      // we don't have booked_at so we can only show time remaining vs expiry
      if (!expiry) return 0;
      try {
        const exp = new Date(expiry).getTime();
        const now = Date.now();
        return Math.max(0, Math.min(100, ((exp - now) / (exp - (now - 30*86400000))) * 100));
      } catch(e) { return 50; }
    }

    function daysLeft(expiry) {
      if (!expiry) return null;
      try {
        const diff = (new Date(expiry).getTime() - Date.now()) / 86400000;
        if (diff <= 0) return 'Expired';
        if (diff < 1) return Math.round(diff * 24) + 'h';
        return Math.round(diff) + 'd';
      } catch(e) { return null; }
    }

    function renderPositions(structs) {
      const el = document.getElementById('positions-list');
      document.getElementById('open-count-badge').textContent = structs.length;
      if (!structs.length) {
        el.innerHTML = `<div class="empty-state"><div class="empty-icon">📡</div><div class="empty-title">Scanning for opportunities</div><div class="empty-sub">The poller will automatically book positions when mispricing is detected across venues.</div></div>`;
        return;
      }
      el.innerHTML = structs.map(s => {
        const dl = daysLeft(s.expiry);
        const pct = progressPct(s.expiry);
        const pfClass = pct < 20 ? 'critical' : pct < 40 ? 'expiring' : '';
        const legs_str = (s.legs||[]).map(l => `${l.side} on "${(l.question||'').slice(0,38)}${l.question?.length>38?'…':''}"`).join(' · ');
        const profitCls = s.projected_profit_inr >= 0 ? 'profit' : 'loss';
        return `<div class="pos-card" style="margin-bottom:12px">
          <div class="pos-card-header">
            <div class="pos-card-meta">
              <div class="pos-card-badges">
                <span class="badge-kind ${s.kind}">${kindLabel(s.kind)}</span>
                <span class="badge-venue ${venueBadgeClass(s.legs)}">${venueLabel(s.legs)}</span>
              </div>
              <div class="pos-card-title">${s.event_title||'Arbitrage Structure'}</div>
              <div class="pos-card-legs">${legs_str}</div>
            </div>
            <div class="pos-status-badge open">OPEN</div>
          </div>
          ${dl ? `<div class="pos-card-progress">
            <div class="progress-meta"><span style="color:var(--text3);font-size:11px">${pct>0?Math.round(100-pct)+'% elapsed':''}·</span><span>${dl} left</span></div>
            <div class="progress-bar"><div class="progress-fill ${pfClass}" style="width:${100-pct}%"></div></div>
          </div>` : ''}
          <div class="pos-card-footer">
            <div class="pos-metric"><div class="m-label">Invested</div><div class="m-value">${fmtInr(s.total_cost_inr)}</div></div>
            <div class="pos-metric"><div class="m-label">Guaranteed</div><div class="m-value">${fmtInr(s.guaranteed_payoff_inr)}</div></div>
            <div class="pos-metric"><div class="m-label">Edge</div><div class="m-value ${profitCls}">${fmtPnl(s.projected_profit_inr)} <span style="font-size:11px;opacity:0.7">${fmtPct(s.return_pct)}</span></div></div>
            <div class="pos-metric"><div class="m-label">Shares</div><div class="m-value">${s.legs?.[0]?.shares||'—'}</div></div>
            <div class="settle-row"><button class="settle-btn" onclick="settle('${s.structure_id}')">✓ Settle</button></div>
          </div>
        </div>`;
      }).join('');
    }

    function renderHistory(structs) {
      const el = document.getElementById('history-list');
      document.getElementById('hist-count-badge').textContent = structs.length;
      if (!structs.length) {
        el.innerHTML = `<div class="empty-state"><div class="empty-icon">📋</div><div class="empty-title">No closed trades yet</div><div class="empty-sub">Settled positions will appear here with final P&L.</div></div>`;
        return;
      }
      const wins = structs.filter(s => s.realised_profit_inr > 0).length;
      el.innerHTML = structs.slice(0, 30).map(s => {
        const isProfit = s.realised_profit_inr >= 0;
        return `<div class="history-row" style="margin-bottom:8px">
          <div class="history-icon ${isProfit?'profit':'loss'}">${isProfit?'✓':'✗'}</div>
          <div class="history-body">
            <div class="history-title">${s.event_title||s.structure_id}</div>
            <div class="history-sub">${s.kind||''} · ${s.settled_at ? new Date(s.settled_at).toLocaleDateString('en-IN', {day:'2-digit',month:'short'}) : ''}</div>
          </div>
          <div class="history-pnl ${isProfit?'profit':'loss'}">${fmtPnl(s.realised_profit_inr)}</div>
        </div>`;
      }).join('');
    }

    /* ── data fetch ── */
    let lastPollCount = 0;
    let lastBookedCount = 0;

    async function fetchPortfolio() {
      try {
        const r = await fetch('/api/portfolio');
        if (!r.ok) throw new Error(r.status);
        const d = await r.json();
        updateHero(d);
        renderPositions(d.open_structures || []);
        renderHistory(d.realised_structures || []);
        updateSidebar(d);
      } catch(e) {
        document.getElementById('status-text').textContent = 'Error';
        document.getElementById('poller-dot').className = 'pulse-dot dead';
      }
    }

    function updateHero(d) {
      const tv = d.total_portfolio_value_inr || 0;
      const init = d.initial_capital_inr || 10000;
      const delta = tv - init;
      document.getElementById('portfolio-value').textContent = fmt(tv);

      const deltaEl = document.getElementById('hero-delta');
      if (Math.abs(delta) < 0.01) {
        deltaEl.textContent = 'No change from initial capital';
        deltaEl.className = 'flat';
      } else {
        const sign = delta > 0 ? '+' : '';
        deltaEl.textContent = `${sign}${fmtInr(delta)} (${fmtPct(((delta/init)*100))}) from ₹${fmt(init)} initial`;
        deltaEl.className = delta > 0 ? 'up' : 'down';
      }

      document.getElementById('stat-cash').textContent = fmtInr(d.available_cash_inr||0);
      document.getElementById('stat-cash').className = 'value';
      document.getElementById('stat-locked').textContent = fmtInr(d.locked_capital_inr||0);
      document.getElementById('stat-active').textContent = d.open_count || 0;

      const pnl = d.realised_pnl_inr || 0;
      const pnlEl = document.getElementById('stat-pnl');
      pnlEl.textContent = fmtPnl(pnl);
      pnlEl.className = 'value ' + cls(pnl);

      const winrate_el = document.getElementById('stat-winrate');
      const rs = d.realised_structures || [];
      if (rs.length) {
        const wins = rs.filter(s => s.realised_profit_inr > 0).length;
        winrate_el.textContent = Math.round(wins / rs.length * 100) + '%';
        winrate_el.className = 'value ' + (wins/rs.length >= 0.5 ? 'profit' : 'loss');
      } else {
        winrate_el.textContent = '—';
        winrate_el.className = 'value';
      }

      const edge = d.unrealised_pnl_inr || 0;
      const edgeEl = document.getElementById('stat-edge');
      edgeEl.textContent = fmtPnl(edge);
      edgeEl.className = 'value ' + cls(edge);

      // Kill switch / poller status
      const alive = !d.kill_switch_active;
      document.getElementById('poller-dot').className = 'pulse-dot' + (alive ? '' : ' dead');
      document.getElementById('halt-btn').className = alive ? '' : 'halted';
      document.getElementById('halt-btn').textContent = alive ? '⬛ HALT' : '▶ RESUME';

      // Feed events for new trades
      if (d.poll_stats) {
        const pc = d.poll_stats.total_booked || 0;
        if (lastBookedCount !== null && pc > lastBookedCount) {
          const diff = pc - lastBookedCount;
          const now = new Date().toLocaleTimeString('en-GB', {hour:'2-digit',minute:'2-digit',second:'2-digit'});
          addFeedItem(`<span class="hi">⚡ ${diff} new trade${diff>1?'s':''} booked</span>`, now);
        }
        lastBookedCount = pc;
        const polls = d.poll_stats.total_polls || 0;
        document.getElementById('status-text').textContent = `LIVE · ${polls} polls`;
        document.getElementById('ps-polls').textContent = polls;
        document.getElementById('ps-booked').textContent = pc;
        document.getElementById('ps-kill').textContent = d.kill_switch_active ? 'ACTIVE' : 'Off';
        document.getElementById('ps-fx').textContent = '₹' + fmt(d.inr_per_usd||83.5, 2) + '/\$';
      }
    }

    function updateSidebar(d) {
      // Venue health — approximate from API latency data if available
      const setDot = (id, state) => {
        const el = document.getElementById('dot-' + id);
        if (el) el.className = 'venue-dot ' + state;
      };
      // We assume all live unless kill switch is active
      const alive = !d.kill_switch_active;
      setDot('poly', alive ? 'live' : 'dead');
      setDot('kalshi', alive ? 'live' : 'dead');
      setDot('manifold', alive ? 'live' : 'dead');

      const setLatency = (id, text) => {
        const el = document.getElementById(id);
        if (el) el.textContent = text;
      };
      if (d.fx_status) {
        setLatency('lat-poly', 'live data');
        setLatency('lat-kalshi', 'live data');
        setLatency('lat-manifold', 'live data');
      }
    }

    /* ── actions ── */
    let haltInFlight = false;
    async function toggleHalt() {
      if (haltInFlight) return;
      haltInFlight = true;
      try {
        const r = await fetch('/api/kill_switch', {method:'POST'});
        const d = await r.json();
        const active = d.active || d.kill_switch_active;
        showToast(active ? 'System halted. No new trades will be booked.' : 'System resumed. Scanning for opportunities.', active ? 'error' : 'success');
        addFeedItem(active ? '<span class="loss">⬛ System halted by operator</span>' : '<span class="profit">▶ System resumed</span>');
        setTimeout(fetchPortfolio, 300);
      } catch(e) { showToast('Failed to toggle halt', 'error'); }
      haltInFlight = false;
    }

    async function scanAndBook() {
      showToast('Scanning markets...', '', 8000);
      try {
        const r = await fetch('/api/poll', {method:'POST'});
        const d = await r.json();
        const booked = d.trades_booked_this_poll || 0;
        showToast(booked > 0 ? `✓ Booked ${booked} trade${booked>1?'s':''}` : 'Scan complete — no new opportunities found', booked > 0 ? 'success' : '');
        addFeedItem(booked > 0 ? `<span class="profit">⚡ Manual scan: booked ${booked} trade${booked>1?'s':''}</span>` : 'Manual scan: no new trades');
        setTimeout(fetchPortfolio, 400);
      } catch(e) { showToast('Scan failed', 'error'); }
    }

    async function settleOldest() {
      try {
        const r = await fetch('/api/portfolio');
        const d = await r.json();
        const open = (d.open_structures||[]);
        if (!open.length) { showToast('No open positions to settle', ''); return; }
        const oldest = open[open.length - 1];
        await settle(oldest.structure_id);
      } catch(e) { showToast('Failed to fetch positions', 'error'); }
    }

    async function settle(id) {
      try {
        const r = await fetch('/api/settle', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({structure_id: id})});
        const d = await r.json();
        if (d.status === 'success' || d.realised_profit_inr !== undefined) {
          const profit = d.realised_profit_inr;
          showToast(profit >= 0 ? `✓ Settled +${fmtInr(profit)}` : `Settled ${fmtInr(profit)}`, profit >= 0 ? 'success' : 'error');
          addFeedItem(profit >= 0
            ? `<span class="profit">✓ Settled ${id} · +${fmtInr(profit)}</span>`
            : `<span class="loss">✗ Settled ${id} · ${fmtInr(profit)}</span>`
          );
          setTimeout(fetchPortfolio, 400);
        } else {
          showToast(d.message || 'Settle failed', 'error');
        }
      } catch(e) { showToast('Settle request failed', 'error'); }
    }

    async function resetPortfolio() {
      if (!confirm('Reset portfolio to ₹10,000 and clear all trades?')) return;
      showToast('Resetting portfolio...', '', 6000);
      try {
        const r = await fetch('/api/reset', {method:'POST'});
        const d = await r.json();
        showToast('Portfolio reset. Scanning for trades...', 'success');
        addFeedItem('<span class="hi">↺ Portfolio reset · Scanning for short-duration opportunities</span>');
        lastBookedCount = 0;
        setTimeout(fetchPortfolio, 500);
        setTimeout(fetchPortfolio, 4000);
        setTimeout(fetchPortfolio, 8000);
      } catch(e) { showToast('Reset failed', 'error'); }
    }

    /* ── auto-refresh ── */
    setInterval(fetchPortfolio, 2500);
    fetchPortfolio();
  </script>
</body>
</html>
"""


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
