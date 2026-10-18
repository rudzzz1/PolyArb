/**
 * PolyArb Terminal — Production Client Script (Phase 5)
 * Handles live polling, approvals review with side-by-side rules diffing,
 * real-time expiry countdown timers, responsive charts (SVG), and execution triggers.
 */

let activeTab = 'open';
let expandedRows = new Set();
let portfolioData = null;
let approvalData = null;
let analyticsData = null;
let countdownInterval = null;

// Currency & formatting helpers
function fmtINR(val) {
  const n = Number(val) || 0;
  return '₹' + n.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function fmtUSD(val) {
  const n = Number(val) || 0;
  return '$' + n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function escapeHTML(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function showToast(msg, isError = false) {
  const t = document.getElementById('toast');
  if (!t) return;
  t.innerText = msg;
  t.style.borderColor = isError ? 'var(--red)' : 'var(--accent)';
  t.style.display = 'block';
  setTimeout(() => { t.style.display = 'none'; }, 4000);
}

// Tab navigation
function switchTab(tabId) {
  activeTab = tabId;
  const tabs = ['open', 'realised', 'approvals', 'analytics', 'performance', 'recon', 'alerts', 'audit'];
  tabs.forEach(t => {
    const btn = document.getElementById('tab' + t.charAt(0).toUpperCase() + t.slice(1) + 'Btn');
    const el = document.getElementById('tab' + t.charAt(0).toUpperCase() + t.slice(1));
    if (btn) btn.classList.toggle('active', t === tabId);
    if (el) el.style.display = (t === tabId) ? 'block' : 'none';
  });

  if (tabId === 'approvals') renderApprovals();
  if (tabId === 'analytics') renderAnalytics();
  if (tabId === 'performance') renderPerformance();
  if (tabId === 'recon') fetchRecon();
  if (tabId === 'alerts') fetchAlerts();
  if (tabId === 'audit') fetchAudit();
}

// Toggle structure accordion row
function toggleExpand(id, e) {
  if (e) e.stopPropagation();
  if (expandedRows.has(id)) {
    expandedRows.delete(id);
  } else {
    expandedRows.add(id);
  }
  renderOpenStructures();
  renderRealisedStructures();
}

// Fetch Core Portfolio Data
async function fetchPortfolio() {
  try {
    const res = await fetch('/api/portfolio');
    if (!res.ok) return;
    portfolioData = await res.json();
    renderSummary(portfolioData.summary);
    renderPollerStatus(portfolioData.poller);
    renderOpenStructures();
    renderRealisedStructures();
  } catch (e) {
    console.error('Error fetching portfolio:', e);
  }
}

// Fetch Approvals Data
async function fetchApprovals() {
  try {
    const res = await fetch('/api/approvals');
    if (!res.ok) return;
    approvalData = await res.json();
    const countPill = document.getElementById('approvalsCountPill');
    if (countPill) countPill.innerText = approvalData.pending ? approvalData.pending.length : 0;
    if (activeTab === 'approvals') renderApprovals();
  } catch (e) {
    console.error('Error fetching approvals:', e);
  }
}

// Fetch Analytics Data
async function fetchAnalytics() {
  try {
    const res = await fetch('/api/analytics');
    if (!res.ok) return;
    analyticsData = await res.json();
    if (activeTab === 'analytics') renderAnalytics();
    if (activeTab === 'performance') renderPerformance();
    updateAnalyticsBadges(analyticsData.metrics);
  } catch (e) {
    console.error('Error fetching analytics:', e);
  }
}

// Update Top Metrics
function renderSummary(s) {
  if (!s) return;
  const initCap = document.getElementById('initCap');
  const initCapUsd = document.getElementById('initCapUsd');
  const availCash = document.getElementById('availCash');
  const availCashUsd = document.getElementById('availCashUsd');
  const venueSplit = document.getElementById('venueSplit');
  const lockedCap = document.getElementById('lockedCap');
  const lockedCapUsd = document.getElementById('lockedCapUsd');
  const lockedSplit = document.getElementById('lockedSplit');
  const realisedPnl = document.getElementById('realisedPnl');
  const realisedPnlUsd = document.getElementById('realisedPnlUsd');
  const unrealisedPnl = document.getElementById('unrealisedPnl');
  const totalVal = document.getElementById('totalVal');
  const openCountPill = document.getElementById('openCountPill');
  const realisedCountPill = document.getElementById('realisedCountPill');

  if (initCap) initCap.innerText = fmtINR(s.initial_capital_inr);
  if (initCapUsd) initCapUsd.innerText = '≈ ' + fmtUSD(s.initial_capital_usd);
  if (availCash) availCash.innerText = fmtINR(s.available_cash_inr);
  if (availCashUsd) availCashUsd.innerText = fmtUSD(s.available_cash_usd);
  if (venueSplit) venueSplit.innerText = `Poly: ${fmtUSD(s.balance_polymarket_usd)} | Kalshi: ${fmtUSD(s.balance_kalshi_usd)}`;
  if (lockedCap) lockedCap.innerText = fmtINR(s.locked_capital_inr);
  if (lockedCapUsd) lockedCapUsd.innerText = fmtUSD(s.locked_capital_usd);
  if (lockedSplit) lockedSplit.innerText = `Poly: ${fmtUSD(s.locked_polymarket_usd)} | Kalshi: ${fmtUSD(s.locked_kalshi_usd)}`;

  if (realisedPnl) {
    const prefix = s.realised_pnl_inr >= 0 ? '+' : '';
    realisedPnl.innerText = prefix + fmtINR(s.realised_pnl_inr);
  }
  if (realisedPnlUsd) {
    const prefix = s.realised_pnl_usd >= 0 ? '+' : '';
    realisedPnlUsd.innerText = prefix + fmtUSD(s.realised_pnl_usd);
  }
  if (unrealisedPnl) {
    const prefix = s.unrealised_pnl_inr >= 0 ? '+' : '';
    unrealisedPnl.innerText = prefix + fmtINR(s.unrealised_pnl_inr);
  }
  if (totalVal) totalVal.innerText = fmtINR(s.total_portfolio_value_inr);
  if (openCountPill) openCountPill.innerText = s.open_count || 0;
  if (realisedCountPill) realisedCountPill.innerText = s.realised_count || 0;

  // Sync mode dropdown
  const modeSelect = document.getElementById('modeSelect');
  if (modeSelect && s.execution_mode) {
    modeSelect.value = s.execution_mode;
  }
}

function updateAnalyticsBadges(m) {
  if (!m) return;
  const sharpeVal = document.getElementById('badgeSharpe');
  const ddVal = document.getElementById('badgeMaxDD');
  const winVal = document.getElementById('badgeWinRate');
  const utilVal = document.getElementById('badgeCapitalUtil');
  if (sharpeVal) sharpeVal.innerText = m.sharpe_ratio.toFixed(2);
  if (ddVal) ddVal.innerText = m.max_drawdown_pct.toFixed(1) + '%';
  if (winVal) winVal.innerText = m.win_rate_pct.toFixed(0) + '%';
  if (utilVal) utilVal.innerText = m.capital_utilization_pct.toFixed(1) + '%';
}

function renderPollerStatus(p) {
  if (!p) return;
  const txt = document.getElementById('pollerStatusText');
  if (txt) {
    const sources = (p.active_sources || []).join(' + ');
    txt.innerText = `5s Poller Active (${sources}) — Scanned: ${p.poll_count || 0} ticks | Booked: ${p.trades_booked || 0}`;
  }
}

// Render Open Positions Table
function renderOpenStructures() {
  const tbody = document.getElementById('openTableBody');
  if (!tbody || !portfolioData) return;
  const structs = portfolioData.open_structures || [];
  if (structs.length === 0) {
    tbody.innerHTML = '<tr><td colspan="12" class="empty-state">No open arbitrage positions. Click "Poll Now" or "Scan & Book" to discover verified mispricings.</td></tr>';
    return;
  }

  let html = '';
  structs.forEach(s => {
    const isExpanded = expandedRows.has(s.structure_id);
    const expBadge = s.expiry_display ? `<span class="badge badge-expiry">${escapeHTML(s.expiry_display)}</span>` : '<span class="badge badge-tag">N/A</span>';
    html += `
      <tr class="structure-row ${isExpanded ? 'expanded' : ''}" onclick="toggleExpand('${s.structure_id}', event)">
        <td style="text-align: center;"><span class="expand-icon">▶</span></td>
        <td class="mono" style="font-weight: 700; color: var(--accent);">${escapeHTML(s.structure_id)}</td>
        <td><span class="badge badge-open">${escapeHTML(s.kind)}</span></td>
        <td style="max-width: 280px; font-weight: 500;">${escapeHTML(s.event_title)}</td>
        <td>${expBadge}</td>
        <td class="mono" style="text-align: center;">${s.legs.length}</td>
        <td class="mono">${s.legs[0] ? s.legs[0].shares.toFixed(0) : '0'}</td>
        <td class="mono" style="font-weight: 600;">${fmtINR(s.total_cost_inr)}</td>
        <td class="mono" style="color: var(--accent);">${fmtINR(s.guaranteed_payoff_inr)}</td>
        <td class="mono" style="color: var(--green); font-weight: 700;">+${fmtINR(s.projected_profit_inr)}</td>
        <td class="mono" style="color: var(--green);">+${s.return_pct.toFixed(1)}%</td>
        <td><span class="badge badge-open">OPEN</span></td>
      </tr>
    `;

    if (isExpanded) {
      html += `
        <tr class="legs-row">
          <td colspan="12">
            <div class="nested-box">
              <div class="nested-header">
                <span>Structure Legs (${s.legs.length}) — Strictly Hedged Logical Basket</span>
                <span>Rule: Trade transitions to Realised only after ALL legs settle</span>
              </div>
              <table style="width: 100%; font-size: 12px;">
                <thead>
                  <tr style="border-bottom: 1px solid rgba(255,255,255,0.08);">
                    <th>Leg ID</th>
                    <th>Venue</th>
                    <th>Contract / Market</th>
                    <th>Side</th>
                    <th>Shares</th>
                    <th>Entry Price</th>
                    <th>Leg Cost</th>
                    <th>Expiry</th>
                    <th>Status</th>
                    <th>Action</th>
                  </tr>
                </thead>
                <tbody>
                  ${s.legs.map(l => {
                    const venueBadge = l.venue === 'kalshi' ? '<span class="badge badge-kalshi">KALSHI</span>' :
                                       (l.venue === 'manifold' ? '<span class="badge badge-manifold">MANIFOLD</span>' : '<span class="badge badge-polymarket">POLYMARKET</span>');
                    const sideBadge = l.side === 'YES' ? '<span class="badge badge-yes">YES</span>' : '<span class="badge badge-no">NO</span>';
                    const legExp = l.expiry_display || l.expiry || 'N/A';
                    const statusBadge = l.settlement_status === 'SETTLED' ?
                      `<span class="badge badge-realised">SETTLED (${l.settlement_outcome || 'WON'})</span>` :
                      '<span class="badge badge-open">OPEN</span>';
                    return `
                      <tr>
                        <td class="mono" style="color: var(--text-dim);">${escapeHTML(l.leg_id)}</td>
                        <td>${venueBadge}</td>
                        <td style="max-width: 260px;">${escapeHTML(l.question)}</td>
                        <td>${sideBadge}</td>
                        <td class="mono">${l.shares.toFixed(0)}</td>
                        <td class="mono">${fmtUSD(l.entry_price_usd)}</td>
                        <td class="mono">${fmtINR(l.cost_inr)}</td>
                        <td><span class="badge badge-tag">${escapeHTML(legExp)}</span></td>
                        <td>${statusBadge}</td>
                        <td>
                          ${l.settlement_status !== 'SETTLED' ? `
                            <button class="btn btn-green" style="padding: 4px 8px; font-size: 11px;" onclick="event.stopPropagation(); settleSpecificLeg('${s.structure_id}', '${l.leg_id}')">
                              Settle Leg
                            </button>
                          ` : '<span style="color: var(--text-muted); font-size: 11px;">Settled</span>'}
                        </td>
                      </tr>
                    `;
                  }).join('')}
                </tbody>
              </table>
            </div>
          </td>
        </tr>
      `;
    }
  });
  tbody.innerHTML = html;
}

// Render Realised Positions Table
function renderRealisedStructures() {
  const tbody = document.getElementById('realisedTableBody');
  if (!tbody || !portfolioData) return;
  const structs = portfolioData.realised_structures || [];
  if (structs.length === 0) {
    tbody.innerHTML = '<tr><td colspan="11" class="empty-state">No realised positions yet. Settle all legs of an open trade to realise profits.</td></tr>';
    return;
  }

  let html = '';
  structs.forEach(s => {
    const isExpanded = expandedRows.has(s.structure_id);
    const settledTime = s.settled_at ? new Date(s.settled_at).toLocaleTimeString() : 'Settled';
    html += `
      <tr class="structure-row ${isExpanded ? 'expanded' : ''}" onclick="toggleExpand('${s.structure_id}', event)">
        <td style="text-align: center;"><span class="expand-icon">▶</span></td>
        <td class="mono" style="font-weight: 700; color: var(--green);">${escapeHTML(s.structure_id)}</td>
        <td><span class="badge badge-realised">${escapeHTML(s.kind)}</span></td>
        <td style="max-width: 280px; font-weight: 500;">${escapeHTML(s.event_title)}</td>
        <td class="mono" style="text-align: center;">${s.legs.length}</td>
        <td class="mono">${s.legs[0] ? s.legs[0].shares.toFixed(0) : '0'}</td>
        <td class="mono">${fmtINR(s.total_cost_inr)}</td>
        <td class="mono" style="color: var(--accent);">${fmtINR(s.guaranteed_payoff_inr)}</td>
        <td class="mono" style="color: var(--green); font-weight: 700;">+${fmtINR(s.realised_profit_inr)}</td>
        <td class="mono" style="color: var(--green);">+${s.return_pct.toFixed(1)}%</td>
        <td><span class="badge badge-realised">${escapeHTML(settledTime)}</span></td>
      </tr>
    `;

    if (isExpanded) {
      html += `
        <tr class="legs-row">
          <td colspan="11">
            <div class="nested-box" style="border-color: rgba(16, 185, 129, 0.3);">
              <div class="nested-header" style="color: var(--green);">
                <span>Realised Legs — All Resolved Leg Outcomes</span>
                <span>Realised: ${fmtINR(s.realised_profit_inr)} (${fmtUSD(s.realised_profit_usd)})</span>
              </div>
              <table style="width: 100%; font-size: 12px;">
                <thead>
                  <tr style="border-bottom: 1px solid rgba(255,255,255,0.08);">
                    <th>Leg ID</th>
                    <th>Venue</th>
                    <th>Question</th>
                    <th>Side</th>
                    <th>Cost</th>
                    <th>Payoff</th>
                    <th>Outcome</th>
                    <th>Settlement Status</th>
                  </tr>
                </thead>
                <tbody>
                  ${s.legs.map(l => {
                    const venueBadge = l.venue === 'kalshi' ? '<span class="badge badge-kalshi">KALSHI</span>' :
                                       (l.venue === 'manifold' ? '<span class="badge badge-manifold">MANIFOLD</span>' : '<span class="badge badge-polymarket">POLYMARKET</span>');
                    const sideBadge = l.side === 'YES' ? '<span class="badge badge-yes">YES</span>' : '<span class="badge badge-no">NO</span>';
                    return `
                      <tr>
                        <td class="mono">${escapeHTML(l.leg_id)}</td>
                        <td>${venueBadge}</td>
                        <td style="max-width: 260px;">${escapeHTML(l.question)}</td>
                        <td>${sideBadge}</td>
                        <td class="mono">${fmtINR(l.cost_inr)}</td>
                        <td class="mono" style="color: var(--green);">${fmtINR(l.payoff_inr)}</td>
                        <td><span class="badge badge-realised">${escapeHTML(l.settlement_outcome || 'WON')}</span></td>
                        <td><span class="badge badge-realised">SETTLED</span></td>
                      </tr>
                    `;
                  }).join('')}
                </tbody>
              </table>
            </div>
          </td>
        </tr>
      `;
    }
  });
  tbody.innerHTML = html;
}

// Render Approvals Queue & Side-by-Side Resolution Rules Diff (Phase 5.2)
function renderApprovals() {
  const container = document.getElementById('approvalsContainer');
  if (!container || !approvalData) return;
  const items = approvalData.pending || [];
  if (items.length === 0) {
    container.innerHTML = `
      <div class="empty-state">
        <h4 style="margin-bottom: 8px; color: var(--text-main);">No Candidates Awaiting Approval</h4>
        <p>Poller continuously monitors live orderbooks. When in Human Approval mode, new candidate structures will appear here with live countdowns and side-by-side rules diffs.</p>
      </div>
    `;
    return;
  }

  let html = '';
  items.forEach(item => {
    const edgePct = (item.unit_cost > 0 ? (item.net_edge / item.unit_cost * 100).toFixed(1) : '0.0');
    const apyText = item.apy ? `${(item.apy * 100).toFixed(1)}% APY` : 'N/A';
    const flagsHtml = (item.risk_flags || []).map(f => `<span class="badge badge-risk">${escapeHTML(f)}</span>`).join('');

    // Legs side-by-side diff
    const leg1 = item.legs[0] || {};
    const leg2 = item.legs[1] || {};

    const diffHtml = `
      <div class="rules-diff-grid">
        <div class="rules-diff-col">
          <div class="rules-col-header">
            <span>Leg 1: <span class="badge ${leg1.venue === 'kalshi' ? 'badge-kalshi' : 'badge-polymarket'}">${(leg1.venue || 'polymarket').toUpperCase()}</span> ${escapeHTML(leg1.side)} @ ${fmtUSD(leg1.entry_price_usd)}</span>
            <span class="badge badge-tag">${escapeHTML(leg1.market_id)}</span>
          </div>
          <div style="font-weight: 600; font-size: 13px; color: var(--text-main);">${escapeHTML(leg1.question || item.event_title)}</div>
          <div class="rules-diff-meta">
            <span><strong>Source:</strong> ${escapeHTML(leg1.resolution_source || 'UMA Oracle / Polygon')}</span>
            <span><strong>Expiry:</strong> ${escapeHTML(leg1.expiry || 'N/A')}</span>
          </div>
          <div class="rules-text-box mono">
            ${escapeHTML(leg1.rules_summary || leg1.description || 'Standard resolution criteria applies.')}
          </div>
        </div>

        <div class="rules-diff-col">
          <div class="rules-col-header">
            <span>Leg 2: <span class="badge ${leg2.venue === 'kalshi' ? 'badge-kalshi' : (leg2.venue === 'manifold' ? 'badge-manifold' : 'badge-polymarket')}">${(leg2.venue || 'kalshi').toUpperCase()}</span> ${escapeHTML(leg2.side)} @ ${fmtUSD(leg2.entry_price_usd)}</span>
            <span class="badge badge-tag">${escapeHTML(leg2.market_id)}</span>
          </div>
          <div style="font-weight: 600; font-size: 13px; color: var(--text-main);">${escapeHTML(leg2.question || item.event_title)}</div>
          <div class="rules-diff-meta">
            <span><strong>Source:</strong> ${escapeHTML(leg2.resolution_source || 'CFTC Official Settlement')}</span>
            <span><strong>Expiry:</strong> ${escapeHTML(leg2.expiry || 'N/A')}</span>
          </div>
          <div class="rules-text-box mono">
            ${escapeHTML(leg2.rules_summary || leg2.description || 'Standard CFTC settlement criteria.')}
          </div>
        </div>
      </div>
    `;

    html += `
      <div class="approval-card" id="app_card_${item.id}">
        <div class="approval-header">
          <div class="approval-title-group">
            <h3>
              <span class="mono" style="color: var(--accent);">${escapeHTML(item.id)}</span>
              <span class="badge badge-open">${escapeHTML(item.kind)}</span>
              <span>${escapeHTML(item.event_title)}</span>
            </h3>
            <div style="font-size: 12px; color: var(--text-muted); margin-top: 4px;">
              Queued at ${new Date(item.created_at).toLocaleTimeString()} | Size: ${item.shares} shares | Total Cost: ${fmtUSD(item.total_cost_usd)} (≈ ${fmtINR(item.total_cost_usd * 83.5)})
            </div>
          </div>
          <div style="text-align: right;">
            <div style="font-size: 18px; font-weight: 700; color: var(--green);">
              +${fmtUSD(item.net_edge)}/sh (+${edgePct}%)
            </div>
            <div style="font-size: 11px; color: var(--text-muted);">Annualized: ${apyText}</div>
            <div class="countdown-bar-wrap">
              <div class="countdown-bar" id="bar_${item.id}" style="width: 100%;"></div>
            </div>
            <div style="font-size: 11px; color: var(--yellow); margin-top: 2px;" id="timer_${item.id}">
              Expires in ${Math.round(item.ttl_remaining_sec || 60)}s
            </div>
          </div>
        </div>

        <div style="margin: 8px 0;">
          <strong style="font-size: 11px; color: var(--text-dim); text-transform: uppercase;">Risk Assessment: </strong>
          ${flagsHtml || '<span style="font-size: 11px; color: var(--green);">✔ No adverse risk flags detected</span>'}
        </div>

        ${diffHtml}

        <div class="approval-footer">
          <div class="approval-action-inputs">
            <input type="text" id="note_${item.id}" placeholder="Optional operator review note (e.g. Verified CPI concordant definition)" />
            <select id="reject_reason_${item.id}" style="background: #090e17; border: 1px solid var(--card-border); color: var(--text-dim); padding: 7px 10px; border-radius: 6px; font-size: 12px;">
              <option value="Rules Ambiguity">Reason: Rules Ambiguity</option>
              <option value="Stale Depth">Reason: Stale Depth</option>
              <option value="Operator Discretion">Reason: Operator Discretion</option>
              <option value="Unfavorable APY">Reason: Unfavorable APY</option>
            </select>
          </div>
          <div style="display: flex; gap: 8px;">
            <button class="btn btn-red" onclick="rejectApproval('${item.id}')">
              ✖ Reject
            </button>
            <button class="btn btn-green" onclick="approveApproval('${item.id}')">
              ✔ Approve & Execute
            </button>
          </div>
        </div>
      </div>
    `;
  });

  container.innerHTML = html;
  startLiveCountdowns(items);
}

// Live Countdown Bar Timer (Phase 5.2)
function startLiveCountdowns(items) {
  if (countdownInterval) clearInterval(countdownInterval);
  countdownInterval = setInterval(() => {
    items.forEach(item => {
      const timerEl = document.getElementById(`timer_${item.id}`);
      const barEl = document.getElementById(`bar_${item.id}`);
      if (!timerEl || !barEl) return;

      const exp = new Date(item.expires_at).getTime();
      const now = Date.now();
      const diffSec = Math.max(0, Math.round((exp - now) / 1000));

      timerEl.innerText = diffSec > 0 ? `Expires in ${diffSec}s` : 'EXPIRED';
      const pct = Math.max(0, Math.min(100, (diffSec / 60) * 100));
      barEl.style.width = pct + '%';

      if (pct > 50) barEl.style.backgroundColor = 'var(--green)';
      else if (pct > 20) barEl.style.backgroundColor = 'var(--yellow)';
      else barEl.style.backgroundColor = 'var(--red)';

      if (diffSec <= 0) {
        timerEl.style.color = 'var(--red)';
        barEl.style.width = '0%';
      }
    });
  }, 1000);
}

// Render Analytics Dashboard (Phase 5.3)
function renderAnalytics() {
  if (!analyticsData) return;
  const edgeInfo = analyticsData.edge_decay || {};
  const breakdowns = analyticsData.breakdowns || {};
  const profitModel = analyticsData.profit_model || {};

  // 1. Edge Decay Distribution Buckets
  const bucketContainer = document.getElementById('edgeDecayBuckets');
  if (bucketContainer && edgeInfo.buckets) {
    const total = Object.values(edgeInfo.buckets).reduce((a, b) => a + b, 0) || 1;
    let bHtml = '';
    for (const [k, v] of Object.entries(edgeInfo.buckets)) {
      const pct = Math.round((v / total) * 100);
      bHtml += `
        <div class="decay-bucket-row">
          <div class="decay-bucket-label">${escapeHTML(k)}</div>
          <div class="decay-bucket-bar-bg">
            <div class="decay-bucket-bar-fill" style="width: ${pct}%;"></div>
          </div>
          <div class="decay-bucket-val">${v} (${pct}%)</div>
        </div>
      `;
    }
    bucketContainer.innerHTML = bHtml;
  }

  // 2. SVG Edge Decay Curve
  renderSvgDecayCurve(edgeInfo.decay_curve || []);

  // 3. Category Breakdown Table
  const catBody = document.getElementById('categoryBreakdownBody');
  if (catBody && breakdowns.categories) {
    catBody.innerHTML = breakdowns.categories.map(c => `
      <tr>
        <td style="font-weight: 600;">${escapeHTML(c.category)}</td>
        <td class="mono" style="text-align: center;">${c.count}</td>
        <td class="mono">${fmtUSD(c.volume_usd)}</td>
        <td class="mono" style="color: var(--green);">+${fmtUSD(c.profit_usd)}</td>
      </tr>
    `).join('');
  }

  // 4. Gross vs Net Profit Model
  const grossEl = document.getElementById('modelGrossProfit');
  const feesEl = document.getElementById('modelTradingFees');
  const gasEl = document.getElementById('modelGasCosts');
  const netEl = document.getElementById('modelNetProfit');
  const pcdEl = document.getElementById('modelProfitCapDay');
  if (grossEl) grossEl.innerText = fmtUSD(profitModel.gross_profit_usd);
  if (feesEl) feesEl.innerText = '-' + fmtUSD(profitModel.trading_fees_usd);
  if (gasEl) gasEl.innerText = '-' + fmtUSD(profitModel.gas_costs_usd);
  if (netEl) netEl.innerText = '+' + fmtUSD(profitModel.net_profit_usd);
  if (pcdEl) pcdEl.innerText = fmtUSD(profitModel.profit_per_capital_day_usd) + '/cap-day';
}

// Render Historical Performance & Equity Curve (Phase 5.4)
function renderPerformance() {
  if (!analyticsData) return;
  const curve = analyticsData.equity_curve || [];
  renderSvgEquityCurve(curve);

  // Venue Distribution Table
  const vBody = document.getElementById('venueBreakdownBody');
  const venues = (analyticsData.breakdowns || {}).venues || [];
  if (vBody) {
    vBody.innerHTML = venues.map(v => `
      <tr>
        <td><span class="badge ${v.venue === 'kalshi' ? 'badge-kalshi' : (v.venue === 'manifold' ? 'badge-manifold' : 'badge-polymarket')}">${v.venue.toUpperCase()}</span></td>
        <td class="mono">${fmtUSD(v.volume_usd)}</td>
        <td class="mono" style="font-weight: 700;">${v.share_pct.toFixed(1)}%</td>
        <td>
          <div style="background: #1e293b; height: 10px; border-radius: 4px; overflow: hidden; width: 100px;">
            <div style="height: 100%; width: ${v.share_pct}%; background: var(--accent);"></div>
          </div>
        </td>
      </tr>
    `).join('');
  }
}

// Pure SVG Responsive Equity Curve Chart (Phase 5.4)
function renderSvgEquityCurve(pts) {
  const container = document.getElementById('svgEquityCurve');
  if (!container || pts.length === 0) return;

  const width = 600;
  const height = 200;
  const padL = 60;
  const padR = 20;
  const padT = 20;
  const padB = 30;

  const values = pts.map(p => p.total_value_usd);
  let minV = Math.min(...values);
  let maxV = Math.max(...values);
  if (maxV === minV) { maxV += 10; minV -= 10; }
  minV = Math.max(0, minV * 0.98);
  maxV = maxV * 1.02;

  const dx = (width - padL - padR) / (pts.length - 1 || 1);
  const dy = (height - padT - padB) / (maxV - minV || 1);

  let pathD = '';
  let areaD = `M ${padL} ${height - padB} `;

  pts.forEach((pt, i) => {
    const x = padL + i * dx;
    const y = height - padB - (pt.total_value_usd - minV) * dy;
    if (i === 0) {
      pathD += `M ${x} ${y} `;
      areaD += `L ${x} ${y} `;
    } else {
      pathD += `L ${x} ${y} `;
      areaD += `L ${x} ${y} `;
    }
  });

  const lastX = padL + (pts.length - 1) * dx;
  areaD += `L ${lastX} ${height - padB} Z`;

  let svg = `
    <svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">
      <defs>
        <linearGradient id="equityGrad" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="var(--accent)" stop-opacity="0.35" />
          <stop offset="100%" stop-color="var(--accent)" stop-opacity="0.0" />
        </linearGradient>
      </defs>
      <!-- Horizontal gridlines -->
      <line x1="${padL}" y1="${padT}" x2="${width - padR}" y2="${padT}" stroke="#1e293b" stroke-dasharray="3,3" />
      <line x1="${padL}" y1="${(padT + height - padB) / 2}" x2="${width - padR}" y2="${(padT + height - padB) / 2}" stroke="#1e293b" stroke-dasharray="3,3" />
      <line x1="${padL}" y1="${height - padB}" x2="${width - padR}" y2="${height - padB}" stroke="#1e293b" />

      <!-- Area and Line -->
      <path d="${areaD}" fill="url(#equityGrad)" />
      <path d="${pathD}" fill="none" stroke="var(--accent)" stroke-width="2.5" />

      <!-- Axis Labels -->
      <text x="${padL - 10}" y="${padT + 4}" fill="var(--text-muted)" font-size="10" text-anchor="end" font-family="var(--font-mono)">$${maxV.toFixed(1)}</text>
      <text x="${padL - 10}" y="${height - padB + 2}" fill="var(--text-muted)" font-size="10" text-anchor="end" font-family="var(--font-mono)">$${minV.toFixed(1)}</text>
      <text x="${padL}" y="${height - 10}" fill="var(--text-muted)" font-size="10" font-family="var(--font-mono)">Start</text>
      <text x="${width - padR}" y="${height - 10}" fill="var(--text-muted)" font-size="10" text-anchor="end" font-family="var(--font-mono)">Now ($${pts[pts.length - 1].total_value_usd.toFixed(2)})</text>
    </svg>
  `;
  container.innerHTML = svg;
}

// Pure SVG Edge Decay Curve Chart (Phase 5.3)
function renderSvgDecayCurve(curve) {
  const container = document.getElementById('svgEdgeDecayCurve');
  if (!container || curve.length === 0) return;

  const width = 450;
  const height = 180;
  const padL = 45;
  const padR = 20;
  const padT = 15;
  const padB = 25;

  const maxSec = 120;
  const dx = (width - padL - padR) / maxSec;
  const dy = (height - padT - padB) / 100;

  let pathD = '';
  curve.forEach((pt, i) => {
    const x = padL + pt.seconds * dx;
    const y = height - padB - pt.remaining_pct * dy;
    if (i === 0) pathD += `M ${x} ${y} `;
    else pathD += `L ${x} ${y} `;
  });

  let svg = `
    <svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">
      <line x1="${padL}" y1="${padT}" x2="${width - padR}" y2="${padT}" stroke="#1e293b" stroke-dasharray="2,2" />
      <line x1="${padL}" y1="${height - padB}" x2="${width - padR}" y2="${height - padB}" stroke="#1e293b" />
      <path d="${pathD}" fill="none" stroke="var(--yellow)" stroke-width="2.5" />
      ${curve.map(pt => `<circle cx="${padL + pt.seconds * dx}" cy="${height - padB - pt.remaining_pct * dy}" r="3.5" fill="var(--yellow)" />`).join('')}
      <text x="${padL - 6}" y="${padT + 4}" fill="var(--text-muted)" font-size="9" text-anchor="end">100%</text>
      <text x="${padL - 6}" y="${height - padB}" fill="var(--text-muted)" font-size="9" text-anchor="end">0%</text>
      <text x="${padL}" y="${height - 8}" fill="var(--text-muted)" font-size="9">0s</text>
      <text x="${padL + 60 * dx}" y="${height - 8}" fill="var(--text-muted)" font-size="9" text-anchor="middle">60s</text>
      <text x="${width - padR}" y="${height - 8}" fill="var(--text-muted)" font-size="9" text-anchor="end">120s</text>
    </svg>
  `;
  container.innerHTML = svg;
}

// User Action Handlers
async function pollNow() {
  try {
    const res = await fetch('/api/poll_now', { method: 'POST' });
    const data = await res.json();
    showToast(data.message || 'Polled live markets.');
    fetchPortfolio();
    fetchApprovals();
  } catch (e) {
    showToast('Poll error: ' + e.message, true);
  }
}

async function bookNext() {
  try {
    const res = await fetch('/api/book_next', { method: 'POST' });
    const data = await res.json();
    showToast(data.message || 'Scanned for live mispricings.');
    fetchPortfolio();
    fetchApprovals();
  } catch (e) {
    showToast('Scan error: ' + e.message, true);
  }
}

async function settleOneLeg() {
  try {
    const res = await fetch('/api/settle_single_leg', { method: 'POST' });
    const data = await res.json();
    showToast(data.message || 'Settled leg.');
    fetchPortfolio();
    fetchAnalytics();
  } catch (e) {
    showToast('Settlement error: ' + e.message, true);
  }
}

async function settleSpecificLeg(structId, legId) {
  try {
    const res = await fetch('/api/settle_single_leg', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ structure_id: structId, leg_id: legId }),
    });
    const data = await res.json();
    showToast(data.message || 'Settled leg.');
    fetchPortfolio();
    fetchAnalytics();
  } catch (e) {
    showToast('Error: ' + e.message, true);
  }
}

async function settleAllLegs() {
  try {
    const res = await fetch('/api/settle_trade', { method: 'POST' });
    const data = await res.json();
    showToast(data.message || 'Settled structure.');
    fetchPortfolio();
    fetchAnalytics();
  } catch (e) {
    showToast('Error: ' + e.message, true);
  }
}

async function approveApproval(itemId) {
  const noteInput = document.getElementById(`note_${itemId}`);
  const notes = noteInput ? noteInput.value : '';
  try {
    const res = await fetch('/api/approvals/approve', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ item_id: itemId, approved_by: 'human_operator', notes }),
    });
    const data = await res.json();
    showToast(data.message || 'Candidate approved.');
    fetchApprovals();
    fetchPortfolio();
    fetchAnalytics();
  } catch (e) {
    showToast('Approval failed: ' + e.message, true);
  }
}

async function rejectApproval(itemId) {
  const reasonSel = document.getElementById(`reject_reason_${itemId}`);
  const reason = reasonSel ? reasonSel.value : 'user_declined';
  const noteInput = document.getElementById(`note_${itemId}`);
  const notes = noteInput ? noteInput.value : '';
  try {
    const res = await fetch('/api/approvals/reject', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ item_id: itemId, reason, notes }),
    });
    const data = await res.json();
    showToast(data.message || 'Candidate rejected.');
    fetchApprovals();
  } catch (e) {
    showToast('Rejection failed: ' + e.message, true);
  }
}

async function changeMode(newMode) {
  try {
    const res = await fetch('/api/approvals/mode', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: newMode }),
    });
    const data = await res.json();
    showToast(data.message || `Switched mode to ${newMode}`);
    fetchPortfolio();
  } catch (e) {
    showToast('Mode error: ' + e.message, true);
  }
}

async function toggleKill(mode) {
  try {
    const res = await fetch('/api/toggle_kill', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode }),
    });
    const data = await res.json();
    showToast(data.message || 'Kill switch updated.');
    fetchPortfolio();
  } catch (e) {
    showToast('Kill error: ' + e.message, true);
  }
}

async function resetPortfolio() {
  if (!confirm('Are you sure you want to reset portfolio to initial ₹10,000 INR?')) return;
  try {
    const res = await fetch('/api/reset', { method: 'POST' });
    const data = await res.json();
    showToast(data.message || 'Portfolio reset.');
    fetchPortfolio();
    fetchAnalytics();
  } catch (e) {
    showToast('Reset failed: ' + e.message, true);
  }
}

// Fetch Reconciliation, Alerts, and Audit Logs
async function fetchRecon() {
  try {
    const res = await fetch('/api/reconciliation');
    if (!res.ok) return;
    const data = await res.json();
    const pill = document.getElementById('reconCountPill');
    if (pill) pill.innerText = data.total_fills || 0;
    const body = document.getElementById('reconTableBody');
    if (body) {
      const fills = data.recent_fills || [];
      if (fills.length === 0) {
        body.innerHTML = '<tr><td colspan="7" class="empty-state">No execution fills recorded yet.</td></tr>';
      } else {
        body.innerHTML = fills.map(f => `
          <tr>
            <td class="mono">${escapeHTML(f.order_id)}</td>
            <td><span class="badge ${f.venue === 'kalshi' ? 'badge-kalshi' : 'badge-polymarket'}">${f.venue.toUpperCase()}</span></td>
            <td class="mono">${fmtUSD(f.expected_price)}</td>
            <td class="mono">${fmtUSD(f.actual_price)}</td>
            <td class="mono" style="color: ${f.slippage_bps > 20 ? 'var(--yellow)' : 'var(--green)'};">${f.slippage_bps.toFixed(1)} bps</td>
            <td class="mono">${fmtUSD(f.fee_paid)}</td>
            <td><span class="badge ${f.slippage_bps > 50 ? 'badge-risk' : 'badge-realised'}">${f.slippage_bps > 50 ? 'SLIPPAGE ALERT' : 'FILLED'}</span></td>
          </tr>
        `).join('');
      }
    }
  } catch (e) {
    console.error('Error fetching recon:', e);
  }
}

async function fetchAlerts() {
  try {
    const res = await fetch('/api/alerts');
    if (!res.ok) return;
    const alerts = await res.json();
    const pill = document.getElementById('alertsCountPill');
    if (pill) pill.innerText = alerts.length;
    const body = document.getElementById('alertsTableBody');
    if (body) {
      if (alerts.length === 0) {
        body.innerHTML = '<tr><td colspan="5" class="empty-state">No system alerts triggered. Operating normally.</td></tr>';
      } else {
        body.innerHTML = alerts.map(a => `
          <tr>
            <td class="mono" style="color: var(--text-dim);">${new Date(a.timestamp).toLocaleTimeString()}</td>
            <td><span class="badge ${a.level === 'CRITICAL' ? 'btn-red' : (a.level === 'WARNING' ? 'btn-yellow' : 'badge-open')}">${escapeHTML(a.level)}</span></td>
            <td><span class="badge badge-tag">${escapeHTML(a.category)}</span></td>
            <td style="font-weight: 600;">${escapeHTML(a.title)}</td>
            <td style="color: var(--text-dim);">${escapeHTML(a.message)}</td>
          </tr>
        `).join('');
      }
    }
  } catch (e) {
    console.error('Error fetching alerts:', e);
  }
}

async function fetchAudit() {
  try {
    const res = await fetch('/api/audit');
    if (!res.ok) return;
    const events = await res.json();
    const pill = document.getElementById('auditCountPill');
    if (pill) pill.innerText = events.length;
    const body = document.getElementById('auditTableBody');
    if (body) {
      if (events.length === 0) {
        body.innerHTML = '<tr><td colspan="7" class="empty-state">No audit events logged yet.</td></tr>';
      } else {
        body.innerHTML = events.map(ev => `
          <tr>
            <td class="mono" style="color: var(--text-dim);">${new Date(ev.timestamp).toLocaleTimeString()}</td>
            <td class="mono" style="color: var(--accent); font-weight: 600;">${escapeHTML(ev.action)}</td>
            <td><span class="badge badge-tag">${escapeHTML(ev.venue)}</span></td>
            <td class="mono">${escapeHTML(ev.order_id || 'N/A')}</td>
            <td class="mono">${ev.price ? fmtUSD(ev.price) : 'N/A'}</td>
            <td class="mono">${ev.size ? ev.size.toFixed(1) : 'N/A'}</td>
            <td><span class="badge badge-open">${escapeHTML(ev.status)}</span></td>
          </tr>
        `).join('');
      }
    }
  } catch (e) {
    console.error('Error fetching audit:', e);
  }
}

// Initial bootstrap and continuous auto-poll loop
window.addEventListener('DOMContentLoaded', () => {
  fetchPortfolio();
  fetchApprovals();
  fetchAnalytics();

  // Poll server state every 2.5 seconds
  setInterval(() => {
    fetchPortfolio();
    if (activeTab === 'approvals') fetchApprovals();
    if (activeTab === 'analytics' || activeTab === 'performance') fetchAnalytics();
  }, 2500);
});
