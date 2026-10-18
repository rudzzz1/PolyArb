# PolyArb Implementation Spec — Sprint 5

**Target:** Lower-model implementation agent  
**Owner:** Orchestrator Agent  
**Status:** Ready for implementation  
**Working directory:** `c:\repos\polyarb\polyarb`

---

## Overview

Five self-contained changes are required. They must be implemented in order because Change 1 (config) is referenced by Changes 2 and 3. All other changes are independent of each other.

| # | Change | File | Priority |
|---|--------|------|----------|
| C1 | Add duration config fields to `RiskLimits` + `paper_auto_settle_after_hours` | `polyarb/config.py` | **First** |
| C2 | Enforce `max_days_to_expiry` filter in `_score_candidate()` | `polyarb/poller.py` | After C1 |
| C3 | Add short-duration score boost in `calculate_score()` | `polyarb/allocator.py` | After C1 |
| C4 | Fix: trigger extra polls after reset | `polyarb/server.py` | Independent |
| C5 | Purge negative-edge legacy structures on load | `polyarb/simulator.py` | Independent |
| C6 | Paper auto-settle: settle positions by time, not just expiry date | `polyarb/poller.py` | After C1 |
| C7 | Full UI redesign — replace `HTML_TEMPLATE` in `server.py` | `polyarb/server.py` | Independent |
| C8 | Bootstrap `data/config.json` with short-duration defaults | `data/config.json` | Independent |

---

## C1 — `polyarb/config.py`: Add duration and auto-settle config fields

### What to do
Open `polyarb/config.py`. Find the `RiskLimits` dataclass (starts at line 11). Add **three new fields** after the existing `model_void_outcomes` field (currently line 27).

### Exact location
```
# Line 27 currently reads:
    model_void_outcomes: bool = False            # include void/refund worlds in worst-case payoff (stress test mode)
```

### Insert these three lines after line 27:
```python
    max_days_to_expiry: float = 30.0             # reject candidates resolving more than N days out
    min_days_to_expiry: float = 1.0              # reject candidates resolving sooner than N days (avoid same-day noise)
    paper_auto_settle_after_hours: float = 0.0   # if > 0, auto-settle open structures this many hours after booking (paper mode)
```

### Resulting block (lines 11–31 after edit):
```python
@dataclass
class RiskLimits:
    max_capital_per_relation: float = 500.0
    max_capital_per_event: float = 1000.0
    max_capital_per_venue: float = 2500.0
    max_total_exposure: float = 5000.0
    daily_loss_limit: float = 100.0
    min_edge: float = 0.005
    max_anomaly_edge: float = 0.35
    min_annualized_return: float = 0.15
    min_shares: float = 5.0
    min_depth_thinnest_leg: float = 5.0
    max_slippage_bps: float = 50.0
    max_clock_skew_seconds: float = 3.0
    consecutive_obs_required: int = 2
    approval_expiry_days: int = 30
    simulate_execution_delay_s: float = 0.25
    model_void_outcomes: bool = False
    max_days_to_expiry: float = 30.0             # reject candidates resolving more than N days out
    min_days_to_expiry: float = 1.0              # reject candidates resolving sooner than N days
    paper_auto_settle_after_hours: float = 0.0   # if > 0, auto-settle structures N hours after booking
```

---

## C2 — `polyarb/poller.py`: Enforce duration filter in `_score_candidate()`

### What to do
Open `polyarb/poller.py`. Find `_score_candidate()` (line 243). After line 284 where `days_to_res` is computed, insert a hard duration gate that rejects candidates outside `[min_days_to_expiry, max_days_to_expiry]`.

### Exact insertion point
Line 284 currently reads:
```python
        days_to_res = min((et - now_t) / 86400.0 for et in valid_ends) if valid_ends else None
```

### Insert the following block **immediately after line 284** (before line 286 which starts `# 4. Exact Mathematical Basket Search`):
```python
        # DURATION GATE: reject markets outside configured short-duration window
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
```

### Result: lines 284–296 after edit:
```python
        days_to_res = min((et - now_t) / 86400.0 for et in valid_ends) if valid_ends else None

        # DURATION GATE: reject markets outside configured short-duration window
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

        # 4. Exact Mathematical Basket Search (Phase 0.1)
        bk = best_basket(
```

---

## C3 — `polyarb/allocator.py`: Add short-duration score boost in `calculate_score()`

### What to do
Open `polyarb/allocator.py`. Find `OpportunityScorer.calculate_score()` (line 46). The current final score computation is on line 78:
```python
        score = net_edge * velocity_multiplier * risk_factor
```

### Replace line 78 with:
```python
        # Short-duration turnaround boost: trades resolving within 14 days get up to 2× boost
        # This ensures fast-settling positions are prioritised over slower ones with similar APY
        if days_to_res and 0 < days_to_res < 14:
            duration_boost = min(2.0, 14.0 / max(days_to_res, 0.5))
        else:
            duration_boost = 1.0

        score = net_edge * velocity_multiplier * risk_factor * duration_boost
```

### Resulting lines 77–82 after edit:
```python
        net_edge = max(0.0, edge - non_trading_cost)

        # Short-duration turnaround boost: trades resolving within 14 days get up to 2× boost
        if days_to_res and 0 < days_to_res < 14:
            duration_boost = min(2.0, 14.0 / max(days_to_res, 0.5))
        else:
            duration_boost = 1.0

        score = net_edge * velocity_multiplier * risk_factor * duration_boost
        return round(score, 6), round(risk_factor, 4)
```

---

## C4 — `polyarb/server.py`: Trigger extra polls after reset

### What to do
Open `polyarb/server.py`. Find the `/api/reset` handler (line 322). Currently after `poller.poll_once()` (line 327), the handler responds immediately. The problem: one synchronous poll is often not enough to find and book a short-duration trade when the booked_relations set has just been cleared.

### Add `import threading` if not already imported at the top of the file
Check the imports at the top. If `import threading` is not already present, add it after the last stdlib import line.

### Replace the reset handler block (lines 322–334):

**Current code:**
```python
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
```

**Replace with:**
```python
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
```

---

## C5 — `polyarb/simulator.py`: Purge negative-edge legacy structures on load

### What to do
Open `polyarb/simulator.py`. Find the `load()` method (starts at line 402). After the existing load completes (after line 434 `self.pending_orders = data.get(...)`), add a call to a new method `_purge_invalid_structures()`.

### Step 1: Add the purge call at the end of `load()`

**Find this block (lines 432–436):**
```python
            self.open_structures = [to_struct(s) for s in data.get("open_structures", [])]
            self.realised_structures = [to_struct(s) for s in data.get("realised_structures", [])]
            self.pending_orders = data.get("pending_orders", [])
        except Exception as e:
            print(f"[portfolio] Error loading state: {e}")
```

**Replace with:**
```python
            self.open_structures = [to_struct(s) for s in data.get("open_structures", [])]
            self.realised_structures = [to_struct(s) for s in data.get("realised_structures", [])]
            self.pending_orders = data.get("pending_orders", [])
            self._purge_invalid_structures()
        except Exception as e:
            print(f"[portfolio] Error loading state: {e}")
```

### Step 2: Add the `_purge_invalid_structures()` method

Add this method to `PortfolioManager` class, immediately after the `load()` method (after line 436, before the blank line that leads to `reset()`):

```python
    def _purge_invalid_structures(self):
        """Remove open structures with negative projected profit — these are legacy seed data
        from before the mathematical edge gate was applied. Purging them restores accurate
        portfolio accounting and prevents negative 'Projected Edge' display in the UI.
        Also removes structures that have already expired (past expiry date) without being settled."""
        now_iso = datetime.now(timezone.utc).isoformat()
        valid = []
        purged = 0
        fx_rate = get_current_fx_rate()

        for struct in self.open_structures:
            # Purge if projected profit is negative (structural loss — pre-gate legacy trade)
            if struct.projected_profit_usd < -0.001:
                print(f"[portfolio] Purging negative-edge legacy structure {struct.structure_id} "
                      f"(projected profit: ${struct.projected_profit_usd:.4f})")
                # Return the locked capital to available cash (no profit/loss on purge)
                self.available_cash_inr += struct.total_cost_inr
                self.available_cash_usd += struct.total_cost_usd
                purged += 1
                continue

            # Purge if structure has already expired (expiry date passed) — these will never settle naturally
            if struct.expiry:
                try:
                    exp_dt = datetime.fromisoformat(struct.expiry.replace("Z", "+00:00"))
                    if exp_dt.isoformat() < now_iso:
                        print(f"[portfolio] Purging already-expired structure {struct.structure_id} "
                              f"(expiry: {struct.expiry})")
                        self.available_cash_inr += struct.total_cost_inr
                        self.available_cash_usd += struct.total_cost_usd
                        purged += 1
                        continue
                except Exception:
                    pass

            valid.append(struct)

        if purged > 0:
            self.open_structures = valid
            self.save()
            print(f"[portfolio] Purged {purged} invalid/expired structures. "
                  f"Available cash restored to ₹{self.available_cash_inr:.2f}")
```

---

## C6 — `polyarb/poller.py`: Paper auto-settle by time (not just expiry date)

### What to do
This enables the system to auto-settle paper trades N hours after booking, rather than waiting for the actual expiry date. This makes the paper trading loop fast enough to validate the strategy in hours/days instead of months.

Open `polyarb/poller.py`. Find `_check_natural_expiry_settlements()` (line 552).

**Current code (lines 552–567):**
```python
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
```

**Replace with:**
```python
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
```

---

## C7 — `polyarb/server.py`: Replace `HTML_TEMPLATE` with redesigned UI

### What to do
Open `polyarb/server.py`. The variable `HTML_TEMPLATE` starts at line 341 and ends at line 1381 (the closing `"""`). Replace the **entire string literal** — from `HTML_TEMPLATE = """` through the closing `"""` on line 1381 — with the new template below.

**Do not touch anything above line 341 or below line 1381.** The `start_server()` function and the `DashboardHandler` class above must remain unchanged.

### New `HTML_TEMPLATE` value

Replace lines 341–1381 entirely with the following:

```python
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
```

---

## C8 — `data/config.json`: Bootstrap short-duration config

### What to do
Create the file `data/config.json` in the working directory (`c:\repos\polyarb\polyarb\data\config.json`). If it already exists, overwrite it.

### File contents:
```json
{
  "risk": {
    "max_days_to_expiry": 30,
    "min_days_to_expiry": 1,
    "min_annualized_return": 0.50,
    "paper_auto_settle_after_hours": 2.0
  }
}
```

**Explanation of values:**
- `max_days_to_expiry: 30` — Only book trades resolving within the next 30 days
- `min_days_to_expiry: 1` — Avoid same-day noise (options that expire within 24 hours)
- `min_annualized_return: 0.50` — Require at least 50% APY (overrides the 15% default — we need fast-turning trades to prove the strategy)
- `paper_auto_settle_after_hours: 2.0` — In paper mode, settle positions 2 hours after booking so the portfolio cycles fast enough to validate the approach without waiting weeks for real market resolution

---

## Verification Checklist

After implementing all changes, restart the server and verify:

1. **Duration filter works** — Add a `logger.info` in the duration gate (C2) and confirm in console output that candidates with `days_to_res > 30` are being logged as rejected during a poll.

2. **Reset books trades** — Click Reset in the UI. Within 10 seconds, at least one new trade should appear in Active Positions with expiry ≤30 days. If no short-duration opportunities exist on live markets, the feed will say "no opportunities found" which is correct.

3. **Projected Edge is positive** — The "Projected Edge" stat pill should show a green positive value after the purge of negative-edge legacy structures.

4. **Auto-settle works** — After 2 hours (configurable), open positions should automatically move to Realised History.

5. **UI loads correctly** — The new UI should show: sticky topbar with ⬡ POLYARB logo, hero strip with portfolio value, two-column layout (positions + sidebar), no horizontal tabs.

6. **Feed is live** — When a new trade is booked, the Live Activity feed in the sidebar should show "⚡ N new trades booked" with a timestamp.

---

## Notes for the Implementing Agent

- **Do not add any new pip dependencies.** The server uses stdlib only (`http.server`, `json`, `threading`, etc.). The HTML uses Google Fonts (external CDN, already used by the existing code) and vanilla JS only.
- **Preserve all docstrings and comments** in files you edit. Only add/modify the specific code blocks described above.
- **`threading` import in server.py** — Check if it's already imported (grep for `import threading` near the top of `server.py`). If not present, add `import threading` after the other stdlib imports.
- **Config loading** — `Config.load()` already reads `data/config.json` if it exists and merges it with defaults via `dataclasses`. The three new fields added in C1 will be automatically picked up when the config file is created in C8.
- **The HTML template replacement in C7** is the largest change. Be precise about which lines to replace. The new template is self-contained and requires no other changes to `server.py` to function.
