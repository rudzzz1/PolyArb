# polyarb — Plan of Action (v3)

> **North Star:** A system that finds real mispricings in short-term prediction
> markets, books them automatically, settles them as they expire, and compounds
> the returns — running continuously with minimal human intervention.
>
> Phases 0–4 are implemented. The critical gaps now are: **short-duration
> trade targeting**, **post-reset auto-booking**, **negative-edge legacy data**,
> and the **UI redesign**.

---

## STATUS SNAPSHOT (Oct 2026)

| Phase | Status | Key Evidence |
|-------|--------|--------------|
| 0 · Bug fixes | ✅ Done | `best_basket()` called correctly, fees included |
| 1 · Foundation | ✅ Done | Audit log, per-venue balances, structured logging |
| 2 · Live Execution | ✅ Done | Orchestrator, kill switch, approval gate, alerts |
| 3 · Profitability | ✅ Done | LP sizing, scorer/allocator, dynamic FX |
| 4 · Cross-Platform | ✅ Done | Manifold added, settlement monitor |
| **5 · UX Redesign** | ✅ Done | Clean single-page terminal, sticky topbar, hero strip, no tab clutter |
| **SHORT-DURATION** | ✅ Done | Hard duration gate [1d, 30d], 2× APY boost for <14d trades |
| **AUTO-CYCLE** | ✅ Done | Timer-scheduled polls on reset, paper auto-settle (2h config) |
| **DATA CLEANUP** | ✅ Done | Automatic purge of negative-edge & expired legacy structures |

---

## Immediate Actions (This Sprint — Days 1–3)

### A.1 · Hard cap on expiry — `poller.py` + `config.py` [S]

Add `max_days_to_expiry: float = 30.0` to `RiskLimits`. In `_score_candidate()`,
**immediately after** computing `days_to_res`:

```python
# HARD EXPIRY CAP — only short-term markets
MAX_EXPIRY_DAYS = self.cfg.risk.max_days_to_expiry  # default 30
MIN_EXPIRY_DAYS = self.cfg.risk.min_days_to_expiry  # default 1

if days_to_res is None:
    return None  # No known expiry → skip
if days_to_res > MAX_EXPIRY_DAYS:
    logger.debug(f"Candidate {cand['id']} skipped: {days_to_res:.0f}d > max {MAX_EXPIRY_DAYS}d")
    return None
if days_to_res < MIN_EXPIRY_DAYS:
    return None  # Expires today, too risky to execute
```

This one change eliminates all 2027 bookings immediately.

### A.2 · Score boost for short duration [S]

In `OpportunityScorer.calculate_score()`, add a duration multiplier:

```python
# Duration multiplier: prefer trades that resolve in < 14 days
# A 7-day trade is worth 2× the score of a 30-day trade at same APY
if days_to_res is not None and days_to_res > 0:
    duration_mult = min(2.0, 14.0 / days_to_res)
else:
    duration_mult = 0.5
score = base_score * duration_mult
```

### A.3 · Fix reset → no bookings [S]

**Root cause**: After reset, `booked_relations` is cleared but the candidates
surfaced are all long-dated (will be blocked by A.1). Once A.1 is in place,
after reset the scanner will find short-term candidates and book them.

**Additional fix**: Force `max_pages=3` on reset poll to surface more markets:

```python
# In server.py /api/reset handler, after portfolio.reset():
poller.booked_relations.clear()
poller.poll_once()          # immediate first scan
# Schedule a second scan 2s later to catch more candidates
threading.Timer(2.0, poller.poll_once).start()
```

### A.4 · Clear legacy negative-edge portfolio data [S]

The file `data/portfolio.json` contains old seed trades booked before Phase 0
fixes were applied. These show guaranteed losses and corrupt the "Projected
Edge" metric.

**Fix**: On server startup, scan `open_structures` for any with
`projected_profit_inr < 0` and automatically close/clear them:

```python
# In PortfolioManager.load() or in start_server():
def purge_invalid_structures(portfolio):
    invalid = [s for s in portfolio.open_structures if s.projected_profit_inr < 0]
    for s in invalid:
        portfolio.open_structures.remove(s)
        # Restore capital (worst-case: return cost, no profit)
        portfolio.available_cash_inr += s.total_cost_inr
        portfolio.available_cash_usd += s.total_cost_usd
    if invalid:
        portfolio.save()
        logger.warning(f"Purged {len(invalid)} invalid legacy structures")
```

### A.5 · UI redesign [M]

Replace the 8-tab, 7-button, table-heavy interface with a clean two-column
card layout. Full specification in `implementation_spec.md`.

---

## Short-Duration Strategy — Design Principles

The system's proof-of-concept window is **weeks, not months**. Every trade
must be booked, held, and settled within that window.

### Target market categories (≤30 days to resolution)

| Category | Venue | Typical duration | Why good for arb |
|----------|-------|-----------------|------------------|
| Monthly economic data | Kalshi | 2–4 weeks | CPI, NFP, Fed decisions; threshold ladders common |
| Crypto monthly/weekly prices | Polymarket + Kalshi | 1–30 days | Same underlying, different thresholds |
| Sports season/match outcomes | Polymarket + Manifold | 1–14 days | Cross-venue equivalent matches |
| Near-term political events | Polymarket + Kalshi | 1–30 days | Specific votes, hearings |
| Weekly/daily resolution markets | Kalshi | 1–7 days | Highest turnover, fastest proof |

### Why long-dated markets (2027+) are a problem

- Capital locked for months → opportunity cost is enormous
- Cannot validate strategy correctness within test window
- APY looks attractive but raw return per capital-day is tiny
- Resolution risk compounds over time

### Configuration for short-duration mode

```json
// data/config.json (new values)
{
  "risk": {
    "max_days_to_expiry": 30,
    "min_days_to_expiry": 1,
    "min_annualized_return": 0.50,
    "min_edge": 0.005,
    "max_anomaly_edge": 0.35
  }
}
```

With `min_annualized_return = 50%` and `max_days_to_expiry = 30`, a 7-day
trade needs only 0.96% net edge to qualify. A 30-day trade needs 4.1%.
This naturally filters out marginal long-dated opportunities.

---

## Auto-Cycle Portfolio Growth

### How it should work (target state)

```
POLL every 5s
  ↓ Find positive-edge trades with ≤30d expiry
  ↓ Book via orchestrator (LP sized)
  ↓ Log to audit trail
  ↓ Wait for expiry
SETTLEMENT MONITOR checks every poll
  ↓ Detect expired structures
  ↓ Settle → credit capital
  ↓ Capital now available for next trade
REPEAT indefinitely until stopped
```

### Current gap in the cycle

The settlement monitor calls `_check_natural_expiry_settlements()` only when
`now >= expiry`. For paper simulation, we want to simulate settlement at
a fixed delay after booking (e.g., 1 hour for 1-day markets, 1 day for 7-day
markets) so we can see the full cycle within a test session.

**Add a paper simulation settlement mode**:

```python
# In config.py
paper_auto_settle_after_hours: float = 0.0  # 0 = only settle at real expiry
                                              # >0 = settle N hours after booking
```

For live demonstration: set `paper_auto_settle_after_hours = 1.0`. The
settlement monitor checks if `now >= booked_at + 1 hour` and settles
automatically. This lets you see the full cycle in an hour.

---

## Phase 5 — UX Redesign (Now Urgent)

> **Gate:** A non-technical operator can understand the system's health,
> see positions, and take action — all within 5 seconds of opening the page.

### 5.1 Remove the 8-tab layout [M]

**Replace** tabs with a single scrollable page with clearly separated sections:
1. Hero strip (portfolio value + key stats)
2. Active Positions (card grid)
3. Realised History (compact list)
4. (Side panel) System health + Live feed + Actions

**No more horizontal-scrolling tab bar.**

### 5.2 Remove the 7-button header [M]

The current header has: Mode dropdown, Poll Now, Settle 1 Leg, Settle All,
Scan & Book, Soft Kill, Hard Kill, Reset = **8 controls competing for attention**.

**Replace with:**
- Left: Logo + System status chip
- Right: Single "HALT" button (shows "RESUME" when halted)
- Actions moved to: sidebar "Quick Actions" widget + contextual buttons on cards

### 5.3 Replace table with position cards [M]

Dense tables are hard to scan. Each position should be a card showing:
- Strategy type + venue badge
- Market name
- Time-to-expiry progress bar
- 4 key metrics: Invested / Guaranteed Payoff / Edge / Shares
- Contextual "Settle" button

### 5.4 Add live activity feed [S]

A scrolling feed in the sidebar showing recent events:
- "17:42:15 Scanned 127 markets, 4 candidates"
- "17:42:10 Booked STR-8842 (+₹57.20 projected)"
- "17:42:00 Settled STR-8841 → +₹44.30 realised"

This makes the system feel alive even between bookings.

### 5.5 Smart empty states [S]

Instead of "No open arbitrage positions", show:
- What the scanner found in the last poll
- Why no trade was booked (no edge, or max expiry filter)
- A visible "Scan Now" CTA

---

## Risk Register (Updated)

| Risk | Status | Mitigation |
|------|--------|------------|
| No short-term mispricings exist | 🔴 Unknown until A.1 ships | After filter: if still no trades in 7 days, expand coverage to weekly sports/crypto markets |
| Legacy negative-edge data corrupting UI | 🔴 Active | A.4 purges on startup |
| Capital locked in long-dated trades | 🔴 Active | A.1 hard cap prevents future locking; existing ones ride to settlement |
| Reset → no auto-booking | 🔴 Active | A.1 + A.3 together fix this |
| Edges consumed by faster bots | 🟡 Unknown | Measure fill rate at 5s poll; if <30% captured, reduce to 1s poll or websocket |
| Resolution risk on cross-venue | 🟢 Mitigated | Risk flags, mandatory approval, side-by-side rules |
| ISP/firewall blocking APIs | 🟢 Mitigated | TLS split transport, VPN documentation |

---

## Immediate Next Steps

1. **Implement A.1** (30-minute task): Add `max_days_to_expiry = 30` filter to `poller._score_candidate()`
2. **Implement A.3** (15-minute task): Force two polls after reset
3. **Implement A.4** (30-minute task): Purge negative-edge structures on load
4. **Implement A.5** (3-4 hour task): Replace HTML_TEMPLATE in server.py with new design
5. **Run for 48 hours**: Observe if short-term bookings happen. If yes, proceed.
6. **If no bookings in 48h**: The market currently has no liquid short-term arb opportunities in the scanned universe. Expand market fetching to `max_pages=5` and add specific short-term market categories.

> **Key question to answer in 48 hours:** Do short-term (≤30d) mispricings
> exist in the current market? If yes, the system works and we scale.
> If no, we need to expand coverage or accept that the current market
> is efficient at this horizon.
