# polyarb — Gap Analysis

> **Goal:** Make polyarb an intuitive, profitable, minimal-risk, cross-platform mispricing
> arbitrator.
>
> This document critically reviews the current codebase against that goal.
> Gaps are scored **CRITICAL / HIGH / MEDIUM / LOW** based on how much they
> block the goal.

---

## 1. Execution & Order Placement — CRITICAL

| Gap | Detail | Impact |
|-----|--------|--------|
| **No order placement code** | The README explicitly states "No order placement code and no API keys." The project cannot execute trades on any venue. | Zero real profitability; every other module is moot without this. |
| **No wallet / API-key management** | No credential store, no signing logic (Polymarket needs EIP-712 typed data; Kalshi needs HMAC). | Cannot interact with authenticated endpoints. |
| **No position management at venues** | No code to query fills, open orders, or token balances on-chain or on Kalshi. | Cannot reconcile simulated state with actual state. |
| **No cancel / amend flow** | No resting-order lifecycle: place → confirm → partial fill → cancel remainder. | Cannot implement limit orders, maker-side, or any execution beyond IOC. |

---

## 2. Profitability Gaps — CRITICAL to HIGH

| Gap | Severity | Detail |
|-----|----------|--------|
| **Poller books trades with no edge gate** | CRITICAL | `poller.py:192` books implies-type trades whenever `unit_cost ≤ 1.02`. That means it willingly pays \$1.02 for a guaranteed \$1.00 payoff — a **guaranteed loss**. The `core.best_basket()` logic is correct but is never called by the poller; it hand-rolls a loose check instead. |
| **Simulator hardcodes payoff to $1.00** | HIGH | `simulator.py:172` assumes `payoff_usd = 1.0 * shares` regardless of basket type, leg configuration, or void worlds. For partition and exclusive groups the payoff can be n−1 or 0; the simulator ignores this. |
| **No real fee deduction in the poller** | HIGH | Kalshi's `0.07·p·(1-p)` taker fee and any Polymarket taker fee are ignored when the poller sizes and books trades. All edge metrics are overstated. |
| **FX rate is hardcoded** | MEDIUM | `INR_PER_USD = 83.50` is baked in as a constant. Any live capital tracking in INR drifts from reality over time. |
| **No opportunity ranking / capital allocation** | HIGH | The poller books whichever candidate it encounters first, up to 2 per tick. There is no ranking by edge magnitude, APY, or risk-adjusted return. Capital is allocated first-come-first-served rather than to the best opportunity. |
| **Template/fallback booking** | HIGH | `server.py:129` falls back to hardcoded `ACTUAL_DATA_TEMPLATES` with stale prices when the live poller finds nothing. These stale entries create fictitious P&L. |

---

## 3. Risk Management Gaps — CRITICAL to HIGH

| Gap | Severity | Detail |
|-----|----------|--------|
| **Poller ignores all risk controls** | CRITICAL | `poller.py._evaluate_and_book_candidate()` does not check `Config.RiskLimits` — no anomaly-edge circuit breaker, no per-event exposure cap, no clock-skew check, no approval expiration, no book validation. All the risk machinery in `config.py` and `scanner.py` is bypassed when the UI poller is running. |
| **Legging risk ignored in live path** | HIGH | The poller/simulator books both legs at ask prices observed sequentially with no latency simulation between them. Slippage between the two fetches is invisible. |
| **No unwind strategy** | HIGH | If one leg fills and the second does not (or price moves), there is no logic to sell the first leg back. The system has `unwind_loss_estimate()` but it is only used in `replay`, never in live or simulated execution. |
| **Settlement assumes best case** | MEDIUM | `settle_entire_structure()` defaults to `world_outcome="YES"`, meaning every settled trade is recorded as the winning outcome. This inflates realised P&L in the dashboard. |
| **No per-venue balance tracking** | MEDIUM | `RiskLimits.max_capital_per_venue` exists but nothing enforces it. Capital is tracked as a single pool; the system doesn't know how much is on Polymarket vs. Kalshi. |
| **Kill switch not checked in poller** | HIGH | `LiveMarketPoller._evaluate_and_book_candidate()` checks kill switch only at the top of `poll_once()`, but booking and settlement continue within the same tick even if the switch is flipped mid-tick. |

---

## 4. Cross-Platform / Venue Gaps — HIGH

| Gap | Severity | Detail |
|-----|----------|--------|
| **Only 2 venues** | HIGH | Goal says "cross platform." Only Polymarket and Kalshi are supported. Other prediction markets (Metaculus, Betfair, Manifold, PredictIt where legal) are absent. |
| **Kalshi websocket not implemented** | HIGH | Kalshi is polled via REST every 5s, adding up to 5s of latency to cross-venue edges. Their websocket API exists but requires auth. |
| **No gas / bridging cost model** | MEDIUM | `Config.non_trading_cost_usd = 0.05` is a flat constant. Actual Polygon gas costs fluctuate; bridging from L1 or funding from a bank are variable. |
| **No Polymarket neg-risk group handling** | MEDIUM | Neg-risk conversion mechanics (conditional tokens framework) affect capital efficiency. The system flags neg-risk events but doesn't model the conversion/merge mechanics. |
| **Cross-venue settlement reconciliation** | HIGH | No code to monitor Polymarket on-chain settlement vs. Kalshi cash settlement and reconcile timing. A cross-venue arb is only safe if both pay out; divergent settlement windows are untracked. |

---

## 5. Intuitiveness / UX Gaps — HIGH

| Gap | Severity | Detail |
|-----|----------|--------|
| **Dashboard is paper-only and misleading** | HIGH | The UI labels reads "Paper Simulator (₹10k INR)" but the poller books trades without real edge verification (see §2). The P&L shown is fiction. A user would believe they are profitable when the system is booking guaranteed losses. |
| **No approval workflow in the UI** | MEDIUM | Approving relations requires `python -m polyarb approve <id>` on the command line. The dashboard has no UI for reviewing, approving, or rejecting candidate relations. |
| **No alerts / notifications** | MEDIUM | No Telegram, email, Slack, or desktop notifications. Plan.md lists this (§1.7) but nothing is implemented. |
| **No historical analytics / charts** | MEDIUM | No time-series charts, edge-decay curves, or performance graphs. The user sees only current-state tables. |
| **Monolithic HTML blob** | LOW | 870 lines of HTML/CSS/JS are embedded as a Python string literal inside `server.py`. Any UI change requires editing Python, and there's no hot-reload, bundling, or component structure. |
| **No mobile / responsive design** | LOW | The CSS `grid-template-columns` is auto-fit but the tables are very wide and not scrollable on mobile viewports. |

---

## 6. Data & Storage Gaps — MEDIUM

| Gap | Severity | Detail |
|-----|----------|--------|
| **SQLite for everything** | MEDIUM | `books.sqlite` grows without bound. No compaction, no retention policy, no archival to Parquet. Plan.md notes this (§1.8) but it's not done. |
| **No raw message logging** | MEDIUM | Websocket messages are parsed and discarded. If the book-rebuilding logic has a bug, there's no replay of raw frames. |
| **No trade print / last-trade data** | MEDIUM | Only asks (and recently bids for Polymarket) are recorded. Trade prints (`last_trade_price`) are available from both APIs but not stored. |
| **portfolio.json is authoritative state** | MEDIUM | All simulated portfolio state is a single JSON file with no locking, no backups, and no journal. A crash during `save()` can corrupt it. |
| **Schema migration** | LOW | The `opps` table schema changed (9 → 11 columns), handled by a bare `except sqlite3.OperationalError` fallback in `scanner.py:143`. No formal migration system. |

---

## 7. Engineering & Code Quality Gaps — MEDIUM to LOW

| Gap | Severity | Detail |
|-----|----------|--------|
| **No dependency management** | MEDIUM | No `pyproject.toml`, `setup.py`, `requirements.txt`, or `poetry.lock`. The only external dep (`websockets`) is mentioned in a comment. |
| **No structured logging** | MEDIUM | All output is `print()`. No log levels, no log files, no correlation IDs. |
| **No CI / CD** | MEDIUM | Tests exist but no GitHub Actions, no pre-commit hooks, no lint. |
| **TLS bypass transport** | MEDIUM | `transport.py` implements manual TLS ClientHello splitting to bypass ISP deep packet inspection. This is fragile, undocumented from a security perspective, and may violate network policies. |
| **Hardcoded fallback IPs** | LOW | `_FALLBACK_IPS` in `transport.py` will silently route to stale IPs if DNS fails. |
| **Global mutable state in server.py** | MEDIUM | `portfolio` and `poller` are module-level singletons instantiated at import time. This makes testing, reloading, and running multiple instances impossible. |
| **No type checking** | LOW | Type hints exist but `mypy` is not configured or run. Several signatures use `Any` or untyped dicts. |
| **Test runner uses `runpy`** | LOW | Tests are run via `runpy.run_path()` rather than `pytest` or `unittest discover`. No test isolation, no fixtures, no coverage reporting. |

---

## 8. Legal / Compliance Gaps — HIGH

| Gap | Severity | Detail |
|-----|----------|--------|
| **No jurisdiction gating** | HIGH | The system will attempt to access Polymarket and Kalshi from any geography. Both venues are restricted in certain jurisdictions (e.g., US for Polymarket, non-US for Kalshi). No geo-check or terms-of-service acceptance flow. |
| **No audit trail for live trades** | HIGH | If the system ever places real orders, there is no immutable audit log of what was sent, confirmed, or rejected by the venue. The simulator's JSON file is overwritten in place. |
| **No tax lot tracking** | MEDIUM | No FIFO/LIFO cost basis tracking, no wash-sale detection, no exportable tax report. |

---

## Summary Heatmap

| Category | CRITICAL | HIGH | MEDIUM | LOW |
|----------|----------|------|--------|-----|
| Execution & Order Placement | 4 | — | — | — |
| Profitability | 1 | 3 | 1 | — |
| Risk Management | 1 | 3 | 2 | — |
| Cross-Platform / Venue | — | 3 | 2 | — |
| Intuitiveness / UX | — | 2 | 2 | 2 |
| Data & Storage | — | — | 4 | 1 |
| Engineering | — | — | 5 | 3 |
| Legal / Compliance | — | 2 | 1 | — |
| **TOTAL** | **6** | **13** | **17** | **6** |

> **Verdict:** The core research logic (`core.py`, `replay.py`, `scanner.py`) is
> solid and well-tested. But the live path (poller → simulator → UI) has
> fundamental correctness bugs (booking guaranteed losses, ignoring all risk
> controls, fictitious settlement), and the complete absence of order placement
> code means the system cannot generate real profit. The gap between "research
> toolkit" and "intuitive, profitable, minimal-risk arbitrator" is large but
> well-scoped.
