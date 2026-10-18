"""Local HTTP Server hosting the Arbitrage Trading & Simulation Dashboard.
Zero external dependencies; standard library http.server only.
"""
from __future__ import annotations
import json
import os
import sys
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

from .simulator import PortfolioManager, seed_initial_simulator_data, ACTUAL_DATA_TEMPLATES
from .config import is_kill_switch_active, trigger_kill_switch

PORT = 8085
portfolio = PortfolioManager(initial_capital_inr=10000.0)
seed_initial_simulator_data(portfolio)


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Quiet standard logging to avoid console spam
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_TEMPLATE.encode("utf-8"))
        elif parsed.path == "/api/portfolio":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            summary = portfolio.get_summary()
            data = {
                "summary": summary,
                "open_structures": [s.__dict__ | {"legs": [l.__dict__ for l in s.legs]} for s in portfolio.open_structures],
                "realised_structures": [s.__dict__ | {"legs": [l.__dict__ for l in s.legs]} for s in portfolio.realised_structures],
                "pending_orders": portfolio.pending_orders,
            }
            self.wfile.write(json.dumps(data).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length).decode("utf-8")) if length > 0 else {}

        if parsed.path == "/api/settle_trade":
            # Settle the first open trade or specified structure
            struct_id = body.get("structure_id")
            if not struct_id and portfolio.open_structures:
                struct_id = portfolio.open_structures[0].structure_id
            if struct_id:
                portfolio.settle_entire_structure(struct_id, world_outcome="YES")
                res = {"status": "success", "message": f"Settled all legs of {struct_id}. Trade moved to Realised!"}
            else:
                res = {"status": "error", "message": "No open structures to settle"}

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(res).encode("utf-8"))

        elif parsed.path == "/api/settle_single_leg":
            # Settle only ONE leg of an open structure (to demonstrate that trade STAYS open until all legs settle)
            struct_id = body.get("structure_id")
            leg_id = body.get("leg_id")
            if not struct_id and portfolio.open_structures:
                struct_id = portfolio.open_structures[0].structure_id
                open_legs = [l for l in portfolio.open_structures[0].legs if l.settlement_status != "SETTLED"]
                if open_legs:
                    leg_id = open_legs[0].leg_id

            if struct_id and leg_id:
                portfolio.settle_single_leg(struct_id, leg_id, outcome="YES")
                res = {"status": "success", "message": f"Settled single leg {leg_id}. Structure remains in Open Positions until all legs settle."}
            else:
                res = {"status": "error", "message": "No unsettled legs available"}

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(res).encode("utf-8"))

        elif parsed.path == "/api/book_next":
            # Book another structure from templates
            open_count = len(portfolio.open_structures)
            tpl = ACTUAL_DATA_TEMPLATES[open_count % len(ACTUAL_DATA_TEMPLATES)]
            booked = portfolio.book_structure(tpl["kind"], tpl["event_title"], tpl["relation_id"], tpl["legs"], tpl["note"])
            if booked:
                res = {"status": "success", "message": f"Booked structure {booked.structure_id} ({booked.kind})"}
            else:
                res = {"status": "error", "message": "Insufficient capital or kill switch active"}

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(res).encode("utf-8"))

        elif parsed.path == "/api/toggle_kill":
            if is_kill_switch_active("data"):
                p = Path("data/KILL_SWITCH")
                if p.exists():
                    p.unlink()
                if "POLYARB_KILL_SWITCH" in os.environ:
                    del os.environ["POLYARB_KILL_SWITCH"]
                res = {"status": "success", "kill_switch": False, "message": "Kill switch deactivated. Operations resumed."}
            else:
                trigger_kill_switch("Manual UI toggle", data_dir="data")
                res = {"status": "success", "kill_switch": True, "message": "Kill switch activated. Operations halted."}

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(res).encode("utf-8"))

        elif parsed.path == "/api/reset":
            portfolio.reset()
            seed_initial_simulator_data(portfolio)
            res = {"status": "success", "message": "Portfolio reset to initial ₹10,000 INR."}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(res).encode("utf-8"))

        else:
            self.send_response(404)
            self.end_headers()


def run_background_market_fluctuations():
    """Simulates realistic micro-market fluctuations on open legs."""
    while True:
        try:
            time.sleep(3.0)
            if portfolio.open_structures:
                for s in portfolio.open_structures:
                    for l in s.legs:
                        if l.settlement_status == "OPEN":
                            # subtle realistic tick +/- 0.005
                            drift = round(random.uniform(-0.005, 0.005), 3)
                            l.current_price_usd = max(0.01, min(0.99, round(l.current_price_usd + drift, 3)))
        except Exception:
            pass


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
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 12px;
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
      padding: 2px 8px;
      border-radius: 4px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }
    .header-controls {
      display: flex;
      gap: 10px;
      align-items: center;
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
    .badge-yes { background: var(--green-bg); color: var(--green); }
    .badge-no { background: var(--purple-bg); color: #c084fc; }

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
      <h1>PolyArb Terminal <span class="badge-sim">Paper Simulator</span></h1>
    </div>
    <div class="header-controls">
      <button class="btn btn-green" onclick="settleOneLeg()">
        ⚡ Settle 1 Leg (Test Rule)
      </button>
      <button class="btn btn-green" onclick="settleAllLegs()">
        ✔ Settle All Legs (Move to Realised)
      </button>
      <button class="btn" onclick="bookNext()">
        ➕ Book Next Structure
      </button>
      <button class="btn" id="killBtn" onclick="toggleKillSwitch()">
        🛑 Kill Switch
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
    </div>
    <div class="metric-card">
      <div class="metric-label">Locked in Structures</div>
      <div class="metric-value mono" style="color: var(--yellow);" id="lockedCap">₹0.00</div>
      <div class="metric-sub" id="lockedCapUsd">$0.00 USD</div>
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
      Open Positions & Structures
      <span class="pill" id="openCountPill">0</span>
    </button>
    <button class="tab-btn" id="tabRealisedBtn" onclick="switchTab('realised')">
      Realised Positions (All Legs Settled)
      <span class="pill" id="realisedCountPill">0</span>
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
        <tr><td colspan="11" class="empty-state">Loading open positions...</td></tr>
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
        <tr><td colspan="12" class="empty-state">No realised positions yet.</td></tr>
      </tbody>
    </table>
  </div>

  <div id="toast" class="toast"></div>

  <script>
    let activeTab = 'open';
    const expandedIds = new Set();

    function showToast(msg) {
      const t = document.getElementById('toast');
      t.innerText = msg;
      t.style.display = 'block';
      setTimeout(() => { t.style.display = 'none'; }, 3500);
    }

    function switchTab(tab) {
      activeTab = tab;
      document.getElementById('tabOpenBtn').classList.toggle('active', tab === 'open');
      document.getElementById('tabRealisedBtn').classList.toggle('active', tab === 'realised');
      document.getElementById('tabOpen').style.display = tab === 'open' ? 'block' : 'none';
      document.getElementById('tabRealised').style.display = tab === 'realised' ? 'block' : 'none';
    }

    function toggleExpand(id) {
      if (expandedIds.has(id)) {
        expandedIds.delete(id);
      } else {
        expandedIds.add(id);
      }
      renderData(lastData);
    }

    let lastData = null;

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

    function renderData(data) {
      if (!data) return;
      const s = data.summary;

      document.getElementById('initCap').innerText = '₹' + s.initial_capital_inr.toLocaleString('en-IN', {minimumFractionDigits: 2});
      document.getElementById('initCapUsd').innerText = '≈ $' + s.initial_capital_usd.toFixed(2) + ' USD';
      document.getElementById('availCash').innerText = '₹' + s.available_cash_inr.toLocaleString('en-IN', {minimumFractionDigits: 2});
      document.getElementById('availCashUsd').innerText = '$' + s.available_cash_usd.toFixed(2) + ' USD';
      document.getElementById('lockedCap').innerText = '₹' + s.locked_capital_inr.toLocaleString('en-IN', {minimumFractionDigits: 2});
      document.getElementById('lockedCapUsd').innerText = '$' + s.locked_capital_usd.toFixed(2) + ' USD';

      const rColor = s.realised_pnl_inr >= 0 ? 'var(--green)' : 'var(--red)';
      const rPrefix = s.realised_pnl_inr >= 0 ? '+₹' : '-₹';
      document.getElementById('realisedPnl').style.color = rColor;
      document.getElementById('realisedPnl').innerText = rPrefix + Math.abs(s.realised_pnl_inr).toFixed(2);
      document.getElementById('realisedPnlUsd').innerText = (s.realised_pnl_usd >= 0 ? '+$' : '-$') + Math.abs(s.realised_pnl_usd).toFixed(2) + ' USD';

      document.getElementById('unrealisedPnl').innerText = '+₹' + s.unrealised_pnl_inr.toFixed(2);
      document.getElementById('totalVal').innerText = '₹' + s.total_portfolio_value_inr.toLocaleString('en-IN', {minimumFractionDigits: 2});

      document.getElementById('openCountPill').innerText = data.open_structures.length;
      document.getElementById('realisedCountPill').innerText = data.realised_structures.length;

      const killBtn = document.getElementById('killBtn');
      if (s.kill_switch_active) {
        killBtn.innerText = '⚠ RESUME (KILLED)';
        killBtn.className = 'btn btn-red';
      } else {
        killBtn.innerText = '🛑 Kill Switch';
        killBtn.className = 'btn';
      }

      // Render Open Structures
      const openBody = document.getElementById('openTableBody');
      if (data.open_structures.length === 0) {
        openBody.innerHTML = '<tr><td colspan="11" class="empty-state">No active open positions. Click "Book Next Structure" to deploy capital.</td></tr>';
      } else {
        openBody.innerHTML = data.open_structures.map(st => renderStructureRow(st, false)).join('');
      }

      // Render Realised Structures
      const realisedBody = document.getElementById('realisedTableBody');
      if (data.realised_structures.length === 0) {
        realisedBody.innerHTML = '<tr><td colspan="12" class="empty-state">No realised positions yet. Settle trades to see them here once all legs finish.</td></tr>';
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
            <td colspan="${isRealised ? 12 : 11}">
              <div class="nested-box">
                <div class="nested-header">
                  <span>Structure Legs Breakdown (${st.legs.length} Contracts)</span>
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
                      <th>Settled Payoff</th>
                      <th>Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    ${st.legs.map(l => `
                      <tr>
                        <td class="mono" style="color: var(--text-dim);">${l.leg_id}</td>
                        <td><span class="badge ${l.venue === 'kalshi' ? 'badge-kalshi' : 'badge-polymarket'}">${l.venue}</span></td>
                        <td style="max-width: 320px;">${l.question}</td>
                        <td><span class="badge ${l.side === 'YES' ? 'badge-yes' : 'badge-no'}">${l.side}</span></td>
                        <td class="mono">${l.shares}</td>
                        <td class="mono">$${l.entry_price_usd.toFixed(3)}</td>
                        <td class="mono">₹${l.cost_inr.toFixed(2)}</td>
                        <td class="mono">$${l.current_price_usd.toFixed(3)}</td>
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

    async function settleOneLeg() {
      const res = await fetch('/api/settle_single_leg', { method: 'POST' });
      const data = await res.json();
      showToast(data.message);
      fetchPortfolio();
    }

    async function settleAllLegs() {
      const res = await fetch('/api/settle_trade', { method: 'POST' });
      const data = await res.json();
      showToast(data.message);
      fetchPortfolio();
    }

    async function bookNext() {
      const res = await fetch('/api/book_next', { method: 'POST' });
      const data = await res.json();
      showToast(data.message);
      fetchPortfolio();
    }

    async function toggleKillSwitch() {
      const res = await fetch('/api/toggle_kill', { method: 'POST' });
      const data = await res.json();
      showToast(data.message);
      fetchPortfolio();
    }

    async function resetPortfolio() {
      if (confirm('Reset portfolio back to ₹10,000 INR?')) {
        const res = await fetch('/api/reset', { method: 'POST' });
        const data = await res.json();
        showToast(data.message);
        fetchPortfolio();
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
    # Start market fluctuation thread
    t = threading.Thread(target=run_background_market_fluctuations, daemon=True)
    t.start()

    server = HTTPServer(("127.0.0.1", port), DashboardHandler)
    print("\n" + "=" * 56)
    print(f"[ONLINE] PolyArb Trading Terminal running at http://localhost:{port}")
    print(f"         Initial Capital: INR 10,000 (~$119.76 USD)")
    print(f"         Open & Realised Tabs with Accordion Breakdown")
    print(f"         Strict Rule: Trade moves to Realised ONLY when all legs settle")
    print("=" * 56 + "\n")
    server.serve_forever()


if __name__ == "__main__":
    p = int(sys.argv[1]) if len(sys.argv) > 1 else PORT
    start_server(p)
