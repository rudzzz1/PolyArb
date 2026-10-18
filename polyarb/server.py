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
cfg = Config()
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
            data = {
                "summary": summary,
                "poller": poller_data,
                "open_structures": [s.__dict__ | {"legs": [l.__dict__ for l in s.legs]} for s in portfolio.open_structures],
                "realised_structures": [s.__dict__ | {"legs": [l.__dict__ for l in s.legs]} for s in portfolio.realised_structures],
                "pending_orders": portfolio.pending_orders,
            }
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

        if parsed.path == "/api/poll_now":
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
            }).encode("utf-8"))

        elif parsed.path == "/api/settle_trade":
            # Settle the first open trade or specified structure
            struct_id = body.get("structure_id")
            if not struct_id and portfolio.open_structures:
                struct_id = portfolio.open_structures[0].structure_id
            if struct_id:
                portfolio.settle_entire_structure(struct_id)
                analytics_engine.record_snapshot(portfolio.get_summary())
                res = {"status": "success", "message": f"Settled all legs of {struct_id}. Trade moved to Realised!"}
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

        elif parsed.path == "/api/toggle_kill":
            mode = body.get("mode", "hard")
            status = get_kill_status("data")
            if status["active"]:
                clear_kill_switch("data", mode="all")
                res = {
                    "status": "success",
                    "kill_switch": False,
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
                    "kill_switch": True,
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
            poller.poll_once()
            res = {"status": "success", "message": "Portfolio reset to initial ₹10,000 INR. Polled live markets."}
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
  <title>PolyArb — Cross-Market Arbitrage Terminal</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg: #0b0f17;
      --card-bg: #121824;
      --card-border: #1f293d;
      --text-main: #f1f5f9;
      --text-dim: #94a3b8;
      --text-muted: #64748b;
      --accent: #38bdf8;
      --green: #10b981;
      --green-bg: rgba(16, 185, 129, 0.12);
      --red: #f43f5e;
      --red-bg: rgba(244, 63, 94, 0.12);
      --yellow: #f59e0b;
      --yellow-bg: rgba(245, 158, 11, 0.12);
      --purple: #a855f7;
      --purple-bg: rgba(168, 85, 247, 0.12);
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background-color: var(--bg);
      color: var(--text-main);
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
      padding: 24px;
      line-height: 1.5;
    }
    .mono { font-family: 'JetBrains Mono', monospace; }
    .header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 24px;
      padding-bottom: 20px;
      border-bottom: 1px solid var(--card-border);
      flex-wrap: wrap;
      gap: 16px;
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 14px;
      flex-wrap: wrap;
    }
    .brand h1 {
      font-size: 22px;
      font-weight: 700;
      letter-spacing: -0.5px;
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .badge-sim {
      font-size: 11px;
      background: rgba(56, 189, 248, 0.15);
      color: var(--accent);
      padding: 3px 8px;
      border-radius: 4px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }
    .badge-poller {
      font-size: 11px;
      background: rgba(16, 185, 129, 0.15);
      color: var(--green);
      padding: 3px 10px;
      border-radius: 20px;
      font-weight: 600;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      border: 1px solid rgba(16, 185, 129, 0.25);
    }
    .pulse-dot {
      width: 7px;
      height: 7px;
      border-radius: 50%;
      background: var(--green);
      box-shadow: 0 0 8px var(--green);
      animation: pulse 2s infinite;
    }
    @keyframes pulse {
      0% { opacity: 1; transform: scale(1); }
      50% { opacity: 0.4; transform: scale(0.85); }
      100% { opacity: 1; transform: scale(1); }
    }
    .header-controls {
      display: flex;
      gap: 10px;
      align-items: center;
      flex-wrap: wrap;
    }
    .btn {
      background: #1e293b;
      color: var(--text-main);
      border: 1px solid var(--card-border);
      padding: 8px 14px;
      border-radius: 6px;
      font-size: 13px;
      font-weight: 600;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      transition: all 0.15s ease;
    }
    .btn:hover { background: #334155; }
    .btn-green { background: #065f46; color: #a7f3d0; border-color: #047857; }
    .btn-green:hover { background: #047857; }
    .btn-red { background: #881337; color: #fecdd3; border-color: #be123c; }
    .btn-red:hover { background: #be123c; }
    .btn-accent { background: rgba(56, 189, 248, 0.15); color: var(--accent); border-color: rgba(56, 189, 248, 0.3); }
    .btn-accent:hover { background: rgba(56, 189, 248, 0.25); }

    /* Metrics Grid */
    .metrics {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 16px;
      margin-bottom: 28px;
    }
    .metric-card {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 8px;
      padding: 16px;
    }
    .metric-label {
      font-size: 12px;
      font-weight: 600;
      color: var(--text-dim);
      text-transform: uppercase;
      letter-spacing: 0.5px;
      margin-bottom: 6px;
    }
    .metric-value {
      font-size: 22px;
      font-weight: 700;
    }
    .metric-sub {
      font-size: 11px;
      color: var(--text-muted);
      margin-top: 4px;
    }

    /* Tabs */
    .tab-bar {
      display: flex;
      gap: 8px;
      border-bottom: 1px solid var(--card-border);
      margin-bottom: 20px;
    }
    .tab-btn {
      background: transparent;
      border: none;
      color: var(--text-dim);
      font-size: 15px;
      font-weight: 600;
      padding: 10px 18px;
      cursor: pointer;
      border-bottom: 2px solid transparent;
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .tab-btn.active {
      color: var(--accent);
      border-bottom-color: var(--accent);
    }
    .pill {
      font-size: 11px;
      padding: 2px 7px;
      border-radius: 10px;
      background: #1e293b;
      color: var(--text-main);
    }
    .tab-btn.active .pill {
      background: rgba(56, 189, 248, 0.2);
      color: var(--accent);
    }

    /* Table Container */
    .table-container {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 8px;
      overflow: hidden;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      text-align: left;
      font-size: 13px;
    }
    th {
      background: #0f172a;
      padding: 12px 16px;
      font-size: 11px;
      font-weight: 600;
      color: var(--text-dim);
      text-transform: uppercase;
      letter-spacing: 0.5px;
      border-bottom: 1px solid var(--card-border);
    }
    td {
      padding: 14px 16px;
      border-bottom: 1px solid var(--card-border);
      vertical-align: middle;
    }
    tr.structure-row {
      cursor: pointer;
      transition: background 0.12s;
    }
    tr.structure-row:hover {
      background: rgba(255, 255, 255, 0.03);
    }
    tr.structure-row.expanded {
      background: rgba(56, 189, 248, 0.04);
      border-bottom: none;
    }
    .expand-icon {
      font-size: 11px;
      color: var(--text-muted);
      display: inline-block;
      width: 16px;
      transition: transform 0.15s ease;
    }
    tr.structure-row.expanded .expand-icon {
      transform: rotate(90deg);
      color: var(--accent);
    }

    /* Badges */
    .badge {
      display: inline-block;
      padding: 3px 8px;
      border-radius: 4px;
      font-size: 11px;
      font-weight: 600;
      text-transform: uppercase;
    }
    .badge-open { background: rgba(56, 189, 248, 0.15); color: #38bdf8; }
    .badge-realised { background: var(--green-bg); color: var(--green); }
    .badge-partial { background: var(--yellow-bg); color: var(--yellow); }
    .badge-polymarket { background: rgba(99, 102, 241, 0.15); color: #818cf8; }
    .badge-kalshi { background: rgba(236, 72, 153, 0.15); color: #f472b6; }
    .badge-manifold { background: rgba(16, 185, 129, 0.15); color: #34d399; }
    .badge-yes { background: var(--green-bg); color: var(--green); }
    .badge-no { background: var(--purple-bg); color: #c084fc; }
    .badge-expiry { background: rgba(56, 189, 248, 0.1); color: #7dd3fc; border: 1px solid rgba(56, 189, 248, 0.2); }

    /* Nested Accordion Sub-table */
    tr.legs-row {
      background: #0d131f;
    }
    tr.legs-row td {
      padding: 0 16px 16px 16px;
      border-bottom: 1px solid var(--card-border);
    }
    .nested-box {
      background: #090e17;
      border: 1px solid rgba(56, 189, 248, 0.2);
      border-radius: 6px;
      padding: 12px;
      margin-top: 6px;
    }
    .nested-header {
      font-size: 11px;
      font-weight: 600;
      color: var(--accent);
      text-transform: uppercase;
      letter-spacing: 0.5px;
      margin-bottom: 10px;
      display: flex;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 8px;
    }
    .nested-table {
      font-size: 12px;
    }
    .nested-table th {
      background: transparent;
      padding: 8px 12px;
      font-size: 10px;
    }
    .nested-table td {
      padding: 8px 12px;
      border-bottom: 1px solid rgba(255, 255, 255, 0.05);
    }
    .empty-state {
      padding: 40px;
      text-align: center;
      color: var(--text-muted);
      font-size: 14px;
    }
    .toast {
      position: fixed;
      bottom: 24px;
      right: 24px;
      background: #1e293b;
      border: 1px solid var(--accent);
      color: var(--text-main);
      padding: 12px 18px;
      border-radius: 6px;
      font-size: 13px;
      box-shadow: 0 8px 24px rgba(0,0,0,0.5);
      display: none;
      z-index: 1000;
    }
  </style>
</head>
<body>

  <!-- Header -->
  <div class="header">
    <div class="brand">
      <h1>PolyArb Terminal <span class="badge-sim">Paper Simulator (₹10k INR)</span></h1>
      <span class="badge-poller" id="pollerBadge">
        <span class="pulse-dot"></span>
        <span id="pollerStatusText">5s Poller Active (Polymarket + Kalshi)</span>
      </span>
    </div>
    <div class="header-controls">
      <div style="display: flex; align-items: center; gap: 6px;">
        <span style="font-size: 11px; color: var(--text-muted); font-weight: 600;">MODE:</span>
        <select id="modeSelect" onchange="changeMode(this.value)" style="background: var(--card-bg); color: var(--accent); border: 1px solid rgba(56, 189, 248, 0.4); padding: 6px 10px; border-radius: 6px; font-size: 12px; font-weight: 600; cursor: pointer;">
          <option value="PAPER">Paper Simulation</option>
          <option value="HUMAN_APPROVAL">Human Approval Gate</option>
          <option value="AUTO">Automated Execution</option>
        </select>
      </div>
      <button class="btn btn-accent" onclick="pollNow()" title="Trigger immediate live scan across Polymarket & Kalshi">
        ⚡ Poll Now (5s)
      </button>
      <button class="btn btn-green" onclick="settleOneLeg()" title="Test rule: settle 1 leg to verify trade remains in Open tab">
        ⚡ Settle 1 Leg (Test Rule)
      </button>
      <button class="btn btn-green" onclick="settleAllLegs()" title="Settle all legs to realize trade">
        ✔ Settle All Legs (Move to Realised)
      </button>
      <button class="btn" onclick="bookNext()">
        ➕ Scan & Book Live Mispricing
      </button>
      <button class="btn" id="softKillBtn" onclick="toggleKill('soft')" style="border-color: var(--yellow); color: var(--yellow);" title="Stop opening new positions, let existing ones ride to settlement">
        ⏸ Soft Kill
      </button>
      <button class="btn btn-red" id="hardKillBtn" onclick="toggleKill('hard')" title="Emergency halt: cancel orders and halt execution">
        🛑 Hard Kill
      </button>
      <button class="btn" onclick="resetPortfolio()" title="Reset portfolio back to ₹10,000">
        ↺ Reset
      </button>
    </div>
  </div>

  <!-- Metric Summary Cards -->
  <div class="metrics">
    <div class="metric-card">
      <div class="metric-label">Initial Capital</div>
      <div class="metric-value mono" id="initCap">₹10,000.00</div>
      <div class="metric-sub" id="initCapUsd">≈ $119.76 USD</div>
    </div>
    <div class="metric-card">
      <div class="metric-label">Available Cash</div>
      <div class="metric-value mono" style="color: var(--accent);" id="availCash">₹0.00</div>
      <div class="metric-sub" id="availCashUsd">$0.00 USD</div>
      <div class="metric-sub" id="venueSplit" style="font-size: 10px; color: var(--accent);">Poly: $0.00 | Kalshi: $0.00</div>
    </div>
    <div class="metric-card">
      <div class="metric-label">Locked in Structures</div>
      <div class="metric-value mono" style="color: var(--yellow);" id="lockedCap">₹0.00</div>
      <div class="metric-sub" id="lockedCapUsd">$0.00 USD</div>
      <div class="metric-sub" id="lockedSplit" style="font-size: 10px; color: var(--yellow);">Poly: $0.00 | Kalshi: $0.00</div>
    </div>
    <div class="metric-card">
      <div class="metric-label">Realised P&L</div>
      <div class="metric-value mono" style="color: var(--green);" id="realisedPnl">+₹0.00</div>
      <div class="metric-sub" id="realisedPnlUsd">+$0.00 USD</div>
    </div>
    <div class="metric-card">
      <div class="metric-label">Projected Edge</div>
      <div class="metric-value mono" id="unrealisedPnl">+₹0.00</div>
      <div class="metric-sub">Guaranteed by logic</div>
    </div>
    <div class="metric-card">
      <div class="metric-label">Portfolio Value</div>
      <div class="metric-value mono" id="totalVal">₹10,000.00</div>
      <div class="metric-sub" id="totalValSub">Cash + Locked + Realised</div>
    </div>
  </div>

  <!-- Tabs Navigation -->
  <div class="tab-bar">
    <button class="tab-btn active" id="tabOpenBtn" onclick="switchTab('open')">
      Open Positions & Orders
      <span class="pill" id="openCountPill">0</span>
    </button>
    <button class="tab-btn" id="tabRealisedBtn" onclick="switchTab('realised')">
      Realised Positions (All Legs Settled)
      <span class="pill" id="realisedCountPill">0</span>
    </button>
    <button class="tab-btn" id="tabApprovalsBtn" onclick="switchTab('approvals')">
      Approval Queue
      <span class="pill" id="approvalsCountPill" style="background: rgba(245, 158, 11, 0.2); color: var(--yellow);">0</span>
    </button>
    <button class="tab-btn" id="tabReconBtn" onclick="switchTab('recon')">
      Fill Reconciliation & Slippage
      <span class="pill" id="reconCountPill">0</span>
    </button>
    <button class="tab-btn" id="tabAlertsBtn" onclick="switchTab('alerts')">
      System Alerts
      <span class="pill" id="alertsCountPill">0</span>
    </button>
    <button class="tab-btn" id="tabAuditBtn" onclick="switchTab('audit')">
      Audit Trail (Immutable)
      <span class="pill" id="auditCountPill">0</span>
    </button>
  </div>

  <!-- Open Tab -->
  <div id="tabOpen" class="table-container">
    <table>
      <thead>
        <tr>
          <th style="width: 30px;"></th>
          <th>ID</th>
          <th>Strategy</th>
          <th>Event & Market Structure</th>
          <th>Expiry</th>
          <th>Legs</th>
          <th>Shares</th>
          <th>Invested (₹)</th>
          <th>Guaranteed Payoff (₹)</th>
          <th>Projected Profit (₹)</th>
          <th>Return</th>
          <th>Status</th>
        </tr>
      </thead>
      <tbody id="openTableBody">
        <tr><td colspan="12" class="empty-state">Loading open positions...</td></tr>
      </tbody>
    </table>
  </div>

  <!-- Realised Tab -->
  <div id="tabRealised" class="table-container" style="display: none;">
    <table>
      <thead>
        <tr>
          <th style="width: 30px;"></th>
          <th>ID</th>
          <th>Strategy</th>
          <th>Event & Market Structure</th>
          <th>Expiry</th>
          <th>Legs</th>
          <th>Shares</th>
          <th>Invested (₹)</th>
          <th>Settled Payoff (₹)</th>
          <th>Realised Profit (₹)</th>
          <th>Return</th>
          <th>Settled At</th>
          <th>Status</th>
        </tr>
      </thead>
      <tbody id="realisedTableBody">
        <tr><td colspan="13" class="empty-state">No realised positions yet. Trades move here only once ALL constituent legs are settled.</td></tr>
      </tbody>
    </table>
  </div>

  <!-- Approvals Tab -->
  <div id="tabApprovals" class="table-container" style="display: none;">
    <table>
      <thead>
        <tr>
          <th>ID</th>
          <th>Created</th>
          <th>Strategy</th>
          <th>Event Title</th>
          <th>Legs</th>
          <th>Cost ($)</th>
          <th>Payoff ($)</th>
          <th>Net Edge ($)</th>
          <th>Shares</th>
          <th>APY</th>
          <th>Risk Flags</th>
          <th>Expires In</th>
          <th>Action</th>
        </tr>
      </thead>
      <tbody id="approvalsTableBody">
        <tr><td colspan="13" class="empty-state">No pending trade approvals in queue.</td></tr>
      </tbody>
    </table>
  </div>

  <!-- Reconciliation Tab -->
  <div id="tabRecon" class="table-container" style="display: none;">
    <div style="padding: 12px 16px; background: rgba(56, 189, 248, 0.05); border-bottom: 1px solid var(--card-border); display: flex; justify-content: space-between; align-items: center;">
      <span style="font-size: 13px; font-weight: 600;">Fill Slippage & Venue Balance Reconciliation</span>
      <button class="btn btn-accent" onclick="reconcileVenueBalances()">⚡ Reconcile Venue Balances</button>
    </div>
    <table>
      <thead>
        <tr>
          <th>Reconciliation ID</th>
          <th>Timestamp (UTC)</th>
          <th>Venue</th>
          <th>Order ID</th>
          <th>Side</th>
          <th>Expected Price ($)</th>
          <th>Actual Fill Price ($)</th>
          <th>Slippage (bps)</th>
          <th>Filled Qty</th>
          <th>Fee Paid ($)</th>
        </tr>
      </thead>
      <tbody id="reconTableBody">
        <tr><td colspan="10" class="empty-state">No fills reconciled yet.</td></tr>
      </tbody>
    </table>
  </div>

  <!-- Alerts Tab -->
  <div id="tabAlerts" class="table-container" style="display: none;">
    <table>
      <thead>
        <tr>
          <th>Timestamp (UTC)</th>
          <th>Level</th>
          <th>Category</th>
          <th>Title</th>
          <th>Message</th>
        </tr>
      </thead>
      <tbody id="alertsTableBody">
        <tr><td colspan="5" class="empty-state">No system alerts recorded.</td></tr>
      </tbody>
    </table>
  </div>

  <!-- Audit Trail Tab -->
  <div id="tabAudit" class="table-container" style="display: none;">
    <table>
      <thead>
        <tr>
          <th>ID</th>
          <th>Timestamp (UTC)</th>
          <th>Action</th>
          <th>Venue</th>
          <th>Order / Structure</th>
          <th>Side</th>
          <th>Price ($)</th>
          <th>Size</th>
          <th>Status</th>
          <th>Response SHA256</th>
        </tr>
      </thead>
      <tbody id="auditTableBody">
        <tr><td colspan="10" class="empty-state">Loading audit events...</td></tr>
      </tbody>
    </table>
  </div>

  <div id="toast" class="toast"></div>

  <script>
    let activeTab = 'open';
    const expandedIds = new Set();
    let lastData = null;

    function showToast(msg) {
      const t = document.getElementById('toast');
      t.innerText = msg;
      t.style.display = 'block';
      setTimeout(() => { t.style.display = 'none'; }, 4000);
    }

    function switchTab(tab) {
      activeTab = tab;
      const tabs = ['open', 'realised', 'approvals', 'recon', 'alerts', 'audit'];
      tabs.forEach(t => {
        const btn = document.getElementById(`tab${t.charAt(0).toUpperCase() + t.slice(1)}Btn`);
        const el = document.getElementById(`tab${t.charAt(0).toUpperCase() + t.slice(1)}`);
        if (btn) btn.classList.toggle('active', tab === t);
        if (el) el.style.display = (tab === t) ? 'block' : 'none';
      });
      if (tab === 'audit') fetchAuditLog();
      if (tab === 'approvals') fetchApprovals();
      if (tab === 'recon') fetchReconciliation();
      if (tab === 'alerts') fetchAlerts();
    }

    function toggleExpand(id) {
      if (expandedIds.has(id)) {
        expandedIds.delete(id);
      } else {
        expandedIds.add(id);
      }
      renderData(lastData);
    }

    async function fetchPortfolio() {
      try {
        const res = await fetch('/api/portfolio');
        const data = await res.json();
        lastData = data;
        renderData(data);
      } catch (err) {
        console.error(err);
      }
    }

    async function fetchAuditLog() {
      try {
        const res = await fetch('/api/audit');
        const events = await res.json();
        renderAuditEvents(events);
      } catch (err) {
        console.error(err);
      }
    }

    async function fetchApprovals() {
      try {
        const res = await fetch('/api/approvals');
        const data = await res.json();
        renderApprovals(data);
      } catch (e) { console.error(e); }
    }

    function renderApprovals(data) {
      const tbody = document.getElementById('approvalsTableBody');
      const items = data.pending || [];
      document.getElementById('approvalsCountPill').innerText = items.length;
      if (items.length === 0) {
        tbody.innerHTML = '<tr><td colspan="13" class="empty-state">No pending trade approvals in queue. Switch mode to Human Approval to queue candidate trades.</td></tr>';
        return;
      }
      tbody.innerHTML = items.map(item => `
        <tr>
          <td class="mono" style="color: var(--accent); font-weight:600;">${item.id}</td>
          <td class="mono" style="font-size: 11px;">${item.created_at ? item.created_at.substring(11, 19) : ''}</td>
          <td><span class="pill mono">${item.kind}</span></td>
          <td style="font-weight: 500;">${item.event_title}</td>
          <td class="mono">${item.legs.length} legs</td>
          <td class="mono">$${item.unit_cost.toFixed(3)}</td>
          <td class="mono">$${item.expected_payoff.toFixed(3)}</td>
          <td class="mono" style="color: var(--green); font-weight: 600;">+$${item.net_edge.toFixed(3)}</td>
          <td class="mono">${item.shares}</td>
          <td class="mono">${item.apy ? item.apy.toFixed(1) + '%' : '-'}</td>
          <td>${item.risk_flags.length ? item.risk_flags.map(f => `<span class="badge badge-partial" style="font-size:10px;">${f}</span>`).join(' ') : '<span style="color:var(--green);font-size:11px;">✓ Clean</span>'}</td>
          <td class="mono" style="font-size:11px; color: var(--yellow);">${item.expires_at ? item.expires_at.substring(11, 19) : ''}</td>
          <td>
            <div style="display: flex; gap: 4px;">
              <button class="btn btn-green" style="padding: 4px 8px; font-size: 11px;" onclick="approveItem('${item.id}')">✔ Approve</button>
              <button class="btn btn-red" style="padding: 4px 8px; font-size: 11px;" onclick="rejectItem('${item.id}')">✖ Reject</button>
            </div>
          </td>
        </tr>
      `).join('');
    }

    async function approveItem(id) {
      showToast('Executing approved trade via Orchestrator...');
      const res = await fetch('/api/approvals/approve', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ item_id: id, approved_by: 'operator' }),
      });
      const data = await res.json();
      showToast(data.message);
      fetchApprovals();
      fetchPortfolio();
      if (activeTab === 'audit') fetchAuditLog();
    }

    async function rejectItem(id) {
      const res = await fetch('/api/approvals/reject', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ item_id: id, reason: 'user_declined' }),
      });
      const data = await res.json();
      showToast(data.message);
      fetchApprovals();
      if (activeTab === 'audit') fetchAuditLog();
    }

    async function changeMode(newMode) {
      const res = await fetch('/api/approvals/mode', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode: newMode }),
      });
      const data = await res.json();
      showToast(data.message);
      fetchPortfolio();
    }

    async function fetchReconciliation() {
      try {
        const res = await fetch('/api/reconciliation');
        const data = await res.json();
        renderReconciliation(data);
      } catch (e) { console.error(e); }
    }

    function renderReconciliation(data) {
      const tbody = document.getElementById('reconTableBody');
      const fills = data.recent_fills || [];
      document.getElementById('reconCountPill').innerText = fills.length;
      if (fills.length === 0) {
        tbody.innerHTML = '<tr><td colspan="10" class="empty-state">No fills reconciled yet.</td></tr>';
        return;
      }
      tbody.innerHTML = fills.map(f => `
        <tr>
          <td class="mono" style="color: var(--text-dim);">${f.order_id}</td>
          <td class="mono" style="font-size: 11px;">${f.timestamp ? f.timestamp.substring(11, 19) : ''}</td>
          <td><span class="badge ${f.venue === 'kalshi' ? 'badge-kalshi' : 'badge-polymarket'}">${f.venue}</span></td>
          <td class="mono">${f.order_id}</td>
          <td><span class="badge badge-open">BUY</span></td>
          <td class="mono">$${(f.expected_price||0).toFixed(3)}</td>
          <td class="mono">$${(f.actual_price||0).toFixed(3)}</td>
          <td class="mono" style="color: ${f.slippage_bps > 20 ? 'var(--yellow)' : 'var(--green)'}; font-weight:600;">${f.slippage_bps > 0 ? '+' : ''}${f.slippage_bps.toFixed(1)}</td>
          <td class="mono">-</td>
          <td class="mono">-</td>
        </tr>
      `).join('');
    }

    async function reconcileVenueBalances() {
      showToast('Reconciling venue balances against live APIs...');
      const res = await fetch('/api/reconcile_balances', { method: 'POST' });
      const data = await res.json();
      showToast('Balance check completed!');
      fetchReconciliation();
      fetchPortfolio();
    }

    async function fetchAlerts() {
      try {
        const res = await fetch('/api/alerts');
        const alerts = await res.json();
        renderAlerts(alerts);
      } catch (e) { console.error(e); }
    }

    function renderAlerts(alerts) {
      const tbody = document.getElementById('alertsTableBody');
      document.getElementById('alertsCountPill').innerText = alerts.length;
      if (!alerts || alerts.length === 0) {
        tbody.innerHTML = '<tr><td colspan="5" class="empty-state">No system alerts recorded.</td></tr>';
        return;
      }
      tbody.innerHTML = alerts.map(a => `
        <tr>
          <td class="mono" style="font-size: 11px;">${a.timestamp ? a.timestamp.substring(11, 19) : ''}</td>
          <td><span class="badge ${a.level === 'CRITICAL' ? 'badge-no' : (a.level === 'WARNING' ? 'badge-partial' : 'badge-open')}">${a.level}</span></td>
          <td><span class="pill mono">${a.category}</span></td>
          <td style="font-weight:600;">${a.title}</td>
          <td style="color: var(--text-dim);">${a.message}</td>
        </tr>
      `).join('');
    }

    async function toggleKill(mode) {
      const res = await fetch('/api/toggle_kill', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode: mode }),
      });
      const data = await res.json();
      showToast(data.message);
      fetchPortfolio();
      if (activeTab === 'alerts') fetchAlerts();
      if (activeTab === 'audit') fetchAuditLog();
    }

    function renderAuditEvents(events) {
      const tbody = document.getElementById('auditTableBody');
      document.getElementById('auditCountPill').innerText = events.length;
      if (!events || events.length === 0) {
        tbody.innerHTML = '<tr><td colspan="10" class="empty-state">No audit records logged yet.</td></tr>';
        return;
      }
      tbody.innerHTML = events.map(e => `
        <tr>
          <td class="mono" style="color: var(--text-dim);">${e.id}</td>
          <td class="mono" style="font-size: 11px;">${e.ts ? e.ts.substring(11, 19) : ''}</td>
          <td><span class="badge ${e.action.includes('PLACED') ? 'badge-open' : (e.action.includes('SETTLED') ? 'badge-realised' : 'badge-partial')}">${e.action}</span></td>
          <td><span class="badge ${e.venue === 'kalshi' ? 'badge-kalshi' : 'badge-polymarket'}">${e.venue}</span></td>
          <td class="mono">${e.order_id || e.market_id || ''}</td>
          <td>${e.side ? `<span class="badge ${e.side === 'YES' ? 'badge-yes' : 'badge-no'}">${e.side}</span>` : '-'}</td>
          <td class="mono">${e.price > 0 ? '$' + e.price.toFixed(3) : '-'}</td>
          <td class="mono">${e.size > 0 ? e.size : '-'}</td>
          <td><span class="badge badge-open">${e.status}</span></td>
          <td class="mono" style="font-size: 10px; color: var(--text-muted);">${e.raw_response_hash ? e.raw_response_hash.substring(0, 12) + '...' : '-'}</td>
        </tr>
      `).join('');
    }

    function renderData(data) {
      if (!data) return;
      const s = data.summary;
      const poller = data.poller || {};

      document.getElementById('initCap').innerText = '₹' + s.initial_capital_inr.toLocaleString('en-IN', {minimumFractionDigits: 2});
      const fxRate = s.inr_per_usd ? s.inr_per_usd.toFixed(2) : '83.50';
      const fxSrc = (s.fx_status && s.fx_status.source) ? ` [${s.fx_status.source}]` : '';
      document.getElementById('initCapUsd').innerText = `≈ $${s.initial_capital_usd.toFixed(2)} USD (@ ₹${fxRate}${fxSrc})`;
      document.getElementById('availCash').innerText = '₹' + s.available_cash_inr.toLocaleString('en-IN', {minimumFractionDigits: 2});
      document.getElementById('availCashUsd').innerText = '$' + s.available_cash_usd.toFixed(2) + ' USD';
      document.getElementById('venueSplit').innerText = `Poly: $${(s.balance_polymarket_usd||0).toFixed(2)} | Kalshi: $${(s.balance_kalshi_usd||0).toFixed(2)}`;

      document.getElementById('lockedCap').innerText = '₹' + s.locked_capital_inr.toLocaleString('en-IN', {minimumFractionDigits: 2});
      document.getElementById('lockedCapUsd').innerText = '$' + s.locked_capital_usd.toFixed(2) + ' USD';
      document.getElementById('lockedSplit').innerText = `Poly: $${(s.locked_polymarket_usd||0).toFixed(2)} | Kalshi: $${(s.locked_kalshi_usd||0).toFixed(2)}`;

      const rColor = s.realised_pnl_inr >= 0 ? 'var(--green)' : 'var(--red)';
      const rPrefix = s.realised_pnl_inr >= 0 ? '+₹' : '-₹';
      document.getElementById('realisedPnl').style.color = rColor;
      document.getElementById('realisedPnl').innerText = rPrefix + Math.abs(s.realised_pnl_inr).toFixed(2);
      document.getElementById('realisedPnlUsd').innerText = (s.realised_pnl_usd >= 0 ? '+$' : '-$') + Math.abs(s.realised_pnl_usd).toFixed(2) + ' USD';

      document.getElementById('unrealisedPnl').innerText = '+₹' + s.unrealised_pnl_inr.toFixed(2);
      document.getElementById('totalVal').innerText = '₹' + s.total_portfolio_value_inr.toLocaleString('en-IN', {minimumFractionDigits: 2});

      document.getElementById('openCountPill').innerText = data.open_structures.length;
      document.getElementById('realisedCountPill').innerText = data.realised_structures.length;

      // Update Poller Status Text
      if (poller.last_poll_time) {
        document.getElementById('pollerStatusText').innerText = `5s Poller Active (${poller.last_poll_time}) | Edge Gate ≥ ${(poller.min_edge*100).toFixed(1)}% | Booked: ${poller.trades_booked}`;
      }

      // Update Kill Switch Buttons
      const hardBtn = document.getElementById('hardKillBtn');
      const softBtn = document.getElementById('softKillBtn');
      const ks = s.kill_status || {};
      if (ks.hard_kill) {
        if (hardBtn) { hardBtn.innerText = '⚠ RESUME HARD'; hardBtn.className = 'btn btn-red'; }
        if (softBtn) { softBtn.style.opacity = '0.5'; }
      } else if (ks.soft_kill) {
        if (softBtn) { softBtn.innerText = '⚠ RESUME SOFT'; softBtn.className = 'btn btn-red'; }
        if (hardBtn) { hardBtn.innerText = '🛑 Hard Kill'; hardBtn.className = 'btn btn-red'; }
      } else {
        if (hardBtn) { hardBtn.innerText = '🛑 Hard Kill'; hardBtn.className = 'btn btn-red'; }
        if (softBtn) { softBtn.innerText = '⏸ Soft Kill'; softBtn.className = 'btn'; softBtn.style.opacity = '1'; }
      }

      // Update Mode select
      const modeSelect = document.getElementById('modeSelect');
      if (modeSelect && s.execution_mode) {
        modeSelect.value = s.execution_mode;
      }
      if (document.getElementById('approvalsCountPill')) {
        document.getElementById('approvalsCountPill').innerText = s.approvals_pending_count || 0;
      }

      // Render Open Structures
      const openBody = document.getElementById('openTableBody');
      if (data.open_structures.length === 0) {
        openBody.innerHTML = '<tr><td colspan="12" class="empty-state">No active open positions. Live 5s poller is scanning Polymarket & Kalshi for verified positive-edge mispricings...</td></tr>';
      } else {
        openBody.innerHTML = data.open_structures.map(st => renderStructureRow(st, false)).join('');
      }

      // Render Realised Structures
      const realisedBody = document.getElementById('realisedTableBody');
      if (data.realised_structures.length === 0) {
        realisedBody.innerHTML = '<tr><td colspan="13" class="empty-state">No realised positions yet. Trades move here only once ALL constituent legs are settled.</td></tr>';
      } else {
        realisedBody.innerHTML = data.realised_structures.map(st => renderStructureRow(st, true)).join('');
      }
    }

    function renderStructureRow(st, isRealised) {
      const isExp = expandedIds.has(st.structure_id);
      const settledLegs = st.legs.filter(l => l.settlement_status === 'SETTLED').length;
      let statusBadge;
      if (isRealised) {
        statusBadge = '<span class="badge badge-realised">REALISED</span>';
      } else if (settledLegs > 0) {
        statusBadge = `<span class="badge badge-partial">SETTLING (${settledLegs}/${st.legs.length})</span>`;
      } else {
        statusBadge = '<span class="badge badge-open">OPEN</span>';
      }

      const pnlVal = isRealised ? st.realised_profit_inr : st.projected_profit_inr;
      const pnlColor = pnlVal >= 0 ? 'var(--green)' : 'var(--red)';
      const pnlPrefix = pnlVal >= 0 ? '+₹' : '-₹';

      let rowHtml = `
        <tr class="structure-row ${isExp ? 'expanded' : ''}" onclick="toggleExpand('${st.structure_id}')">
          <td><span class="expand-icon">▶</span></td>
          <td class="mono" style="font-weight: 600; color: var(--accent);">${st.structure_id}</td>
          <td><span class="pill mono">${st.kind}</span></td>
          <td>
            <div style="font-weight: 600;">${st.event_title}</div>
            <div style="font-size: 11px; color: var(--text-muted);">${st.note || ''}</div>
          </td>
          <td>
            <span class="badge badge-expiry mono">${st.expiry_display || 'N/A'}</span>
          </td>
          <td class="mono">${st.legs.length} legs</td>
          <td class="mono">${st.legs[0] ? st.legs[0].shares : 0}</td>
          <td class="mono">₹${st.total_cost_inr.toFixed(2)}</td>
          <td class="mono">₹${(isRealised ? (st.total_cost_inr + st.realised_profit_inr) : st.guaranteed_payoff_inr).toFixed(2)}</td>
          <td class="mono" style="color: ${pnlColor}; font-weight: 600;">${pnlPrefix}${Math.abs(pnlVal).toFixed(2)}</td>
          <td class="mono" style="color: ${pnlColor};">${st.return_pct > 0 ? '+' : ''}${st.return_pct.toFixed(1)}%</td>
          ${isRealised ? `<td class="mono" style="font-size: 11px; color: var(--text-muted);">${(st.settled_at || '').substring(11, 19)}</td>` : ''}
          <td>${statusBadge}</td>
        </tr>
      `;

      if (isExp) {
        rowHtml += `
          <tr class="legs-row">
            <td colspan="${isRealised ? 13 : 12}">
              <div class="nested-box">
                <div class="nested-header">
                  <span>Structure Legs Breakdown (${st.legs.length} Contracts) — Expiry & Cutoff Times</span>
                  <span>Strict Settlement Gate: All legs must settle to realize trade</span>
                </div>
                <table class="nested-table">
                  <thead>
                    <tr>
                      <th>Leg ID</th>
                      <th>Venue</th>
                      <th>Contract Question</th>
                      <th>Side</th>
                      <th>Shares</th>
                      <th>Entry Price</th>
                      <th>Cost (₹)</th>
                      <th>Current Price</th>
                      <th>Contract Expiry</th>
                      <th>Settled Payoff</th>
                      <th>Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    ${st.legs.map(l => `
                      <tr>
                        <td class="mono" style="color: var(--text-dim);">${l.leg_id}</td>
                        <td><span class="badge ${l.venue === 'kalshi' ? 'badge-kalshi' : (l.venue === 'manifold' ? 'badge-manifold' : 'badge-polymarket')}">${l.venue}</span></td>
                        <td style="max-width: 300px;">${l.question}</td>
                        <td><span class="badge ${l.side === 'YES' ? 'badge-yes' : 'badge-no'}">${l.side}</span></td>
                        <td class="mono">${l.shares}</td>
                        <td class="mono">$${l.entry_price_usd.toFixed(3)}</td>
                        <td class="mono">₹${l.cost_inr.toFixed(2)}</td>
                        <td class="mono">$${l.current_price_usd.toFixed(3)}</td>
                        <td><span class="badge badge-expiry mono">${l.expiry_display || 'N/A'}</span></td>
                        <td class="mono">${l.settlement_status === 'SETTLED' ? `₹${l.payoff_inr.toFixed(2)} ($${l.payoff_usd.toFixed(2)})` : 'Pending'}</td>
                        <td>
                          <span class="badge ${l.settlement_status === 'SETTLED' ? 'badge-realised' : 'badge-open'}">
                            ${l.settlement_status === 'SETTLED' ? `SETTLED (${l.settlement_outcome})` : 'OPEN'}
                          </span>
                        </td>
                      </tr>
                    `).join('')}
                  </tbody>
                </table>
              </div>
            </td>
          </tr>
        `;
      }

      return rowHtml;
    }

    async function pollNow() {
      showToast('Polling Polymarket & Kalshi live...');
      const res = await fetch('/api/poll_now', { method: 'POST' });
      const data = await res.json();
      showToast(data.message);
      fetchPortfolio();
      if (activeTab === 'audit') fetchAuditLog();
    }

    async function settleOneLeg() {
      const res = await fetch('/api/settle_single_leg', { method: 'POST' });
      const data = await res.json();
      showToast(data.message);
      fetchPortfolio();
      if (activeTab === 'audit') fetchAuditLog();
    }

    async function settleAllLegs() {
      const res = await fetch('/api/settle_trade', { method: 'POST' });
      const data = await res.json();
      showToast(data.message);
      fetchPortfolio();
      if (activeTab === 'audit') fetchAuditLog();
    }

    async function bookNext() {
      const res = await fetch('/api/book_next', { method: 'POST' });
      const data = await res.json();
      showToast(data.message);
      fetchPortfolio();
      if (activeTab === 'audit') fetchAuditLog();
    }

    async function toggleKillSwitch() {
      const res = await fetch('/api/toggle_kill', { method: 'POST' });
      const data = await res.json();
      showToast(data.message);
      fetchPortfolio();
      if (activeTab === 'audit') fetchAuditLog();
    }

    async function resetPortfolio() {
      if (confirm('Reset portfolio back to ₹10,000 INR?')) {
        const res = await fetch('/api/reset', { method: 'POST' });
        const data = await res.json();
        showToast(data.message);
        fetchPortfolio();
        if (activeTab === 'audit') fetchAuditLog();
      }
    }

    // Auto-refresh every 2.5 seconds
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
