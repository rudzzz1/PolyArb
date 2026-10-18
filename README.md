# polyarb

Paper-trading research toolkit for **logical arbitrage on prediction markets**
(Polymarket, Kalshi, and across the two). It finds baskets of positions whose
payoff is guaranteed by logic, measures how long those edges last in recorded
data, and logs paper opportunities. **No order placement code and no API keys.**

> Status: groundwork. Logic and storage are unit-tested (18 tests). Network code
> was written against public docs and has **not** been run live yet. Run
> `python -m polyarb doctor` first. See `plan.md` for the roadmap and risks.

---

## 1. Core idea

Every leg is a **buy** of a YES or NO token at its ask. A *relation* says which
joint outcomes are possible. A basket's guaranteed payoff is its **worst case
over all possible outcomes**. If that exceeds cost plus fees, it is an arb. No
probability estimates are needed.

Example: market A ("X by March") implies market B ("X by June").
Buy NO on A at 0.45 and YES on B at 0.50. At least one of them must pay $1, so
you pay 0.95 for a guaranteed 1.00 (before fees).

| Relation     | Meaning                    | Typical basket           |
|--------------|----------------------------|--------------------------|
| `implies`    | A true ⇒ B true            | NO(A) + YES(B)           |
| `exclusive`  | at most one YES            | all NO (or none)         |
| `partition`  | exactly one YES            | all YES                  |
| `equivalent` | same question, two markets | YES(one) + NO(other)     |

The search is brute force over 3^n leg choices (n ≤ 8). Larger exclusive or
partition groups test only the all-YES and all-NO baskets.

---

## 2. Setup

- Python 3.10+
- Standard library only, except `pip install websockets` (needed for `stream`
  and the websocket check in `doctor`).
- Run everything from the project root (the folder containing `polyarb/`).

---

## 3. Steps to execute

```bash
# 0. Verify live API field names (do this first, paste failures back for fixes)
python -m polyarb doctor

# 1. Discover markets and propose relations (Polymarket + Kalshi + cross-venue)
python -m polyarb discover --kalshi

# 2. HUMAN STEP: open data/candidates.json, read BOTH markets' resolution rules,
#    and set "approved": true only for relations you trust.

# 3. Record order books (leave running for days)
python -m polyarb stream          # websocket for Polymarket, polling for Kalshi
#   or: python -m polyarb record   # pure REST polling, simpler, slower

# 4. Analyse: how many arbs, how long they lasted, did they survive latency
python -m polyarb replay

# 5. Optional: live paper scanner with a simulated latency re-check
python -m polyarb scan
```

Re-running `discover` keeps your `approved` flags for relations that still exist.

### Command reference

| Command      | Flags (defaults)                                                                                     |
|--------------|------------------------------------------------------------------------------------------------------|
| `doctor`     | none                                                                                                 |
| `discover`   | `--kalshi` include Kalshi, cross-venue matching, and numeric threshold ladders                       |
| `candidates` | `--approved-only`, `--unapproved-only` list relations with auto-flagged risk warnings               |
| `approve`    | `<relation_id> [--user alice] [--days 30]` approve relation with audit trail and expiration          |
| `record`     | `--interval 5` `--depth 10` `--workers 8` `--all-candidates`                                         |
| `stream`     | `--depth 10` `--flush 0.1` `--chunk 100` `--kalshi-interval 5` `--all-candidates`                    |
| `replay`     | `--horizons 5,15,60` `--fee-bps 0` `--min-edge 0.005` `--min-shares 5` `--min-apy 0.15` `--exec-delay 0.25` |
| `scan`       | `--interval 30` `--fee-bps 0` `--min-edge 0.005` `--min-shares 5` `--latency 0.5`                    |
| `kill`       | `--reason "..."` trigger emergency kill switch to immediately halt all active loops                   |
| `unkill`     | remove kill switch to resume operations                                                              |

`--all-candidates` includes unapproved relations, which is useful for gathering
data before you have reviewed them. Never treat that data as tradable.

---

## 4. Architecture

```
 Gamma API ─┐                          ┌─────────────┐
 Kalshi API ┴─► discover ─► markets.json, candidates.json (human approves)
                                        │
        ┌───────────────────────────────┴───────────────┐
        ▼                                               ▼
   record / stream                                    scan
 (REST poll / websocket)                    (live books → baskets → sizing
        │                                    → latency re-check → log)
        ▼                                               ▼
   books.sqlite  ──► replay ──► episodes, decay stats   opps.sqlite
   (snaps, events)
```

| Module        | Role                                                                        |
|---------------|-----------------------------------------------------------------------------|
| `core.py`     | Relations, worst-case payoff, best basket search, depth-aware `sweep` sizing, per-leg fees. Pure functions, no I/O. |
| `gamma.py`    | Polymarket discovery, parsing, ask ladders, date-ladder and neg-risk candidate generation. |
| `kalshi.py`   | Kalshi discovery and books. Kalshi books are bids only, so YES ask = 1 − best NO bid. Fee estimate. |
| `venues.py`   | One interface over both venues for books and fees.                          |
| `matcher.py`  | Cross-venue `equivalent` proposals: close end dates, identical numbers, text overlap. |
| `recorder.py` | REST polling recorder. Stores a row only when a book changes.               |
| `stream.py`   | Websocket recorder. Rebuilds Polymarket books from snapshots plus deltas; Kalshi polled in a thread. |
| `replay.py`   | Replays stored books through the same logic as `scan`. Reports episodes and survival at 5/15/60s. |
| `scanner.py`  | Live paper scanner. Logs opportunities and what remains after simulated latency. |
| `doctor.py`   | Live API field and format checker.                                          |

### Data files (`data/`)

| File               | Contents                                                       |
|--------------------|----------------------------------------------------------------|
| `markets.json`     | All parsed binary markets, both venues                         |
| `candidates.json`  | Proposed relations with `approved` flags (your decisions)      |
| `books.sqlite`     | `snaps(ts, market_id, yes, no)` ask ladders; `events` for connects/disconnects |
| `opps.sqlite`      | `opps` table from `scan`: shares and profit at detection vs after latency |

---

## 5. Reading the results

`replay` prints, per run: number of episodes, median duration, profit at
detection, and for each horizon how many edges were still alive and the profit
that remained. Rules of thumb:

- Many one-poll blips and few survivors at 15s and 60s: mostly noise, or edges
  you cannot capture with this stack.
- A few long-lived, sizeable episodes that survive fees: worth investigating.
- Recording resolution is your poll interval (or the 0.1s flush for Polymarket
  websocket data). You cannot measure decay faster than that.
- Cross-venue legs are fetched at slightly different times, so some edges are
  timing artefacts.

---

## 6. Tests

Run all production test suites:
```bash
python tests/run_all.py
```

Covers:
- `test_core.py`: Core logic, sizing, fees, Kalshi bid-to-ask conversion, matching, recording, replay, websocket router (18 unit tests).
- `test_risk_and_payoff.py`: Property-based independent brute-force solver comparison across random scenarios, void/refund worlds, orderbook validation, annualized return APY, and depth sizing limits.
- `test_matcher_polarity.py`: Polarity detection (win/lose, above/below, negation), resolution mismatch auto-flagging, numeric threshold ladders, and approval lifecycle/expiry.
- `test_replay_and_legging.py`: Disconnect gap handling, phantom-arb filtering, simulated leg-by-leg execution, slippage, and gross vs net fee breakdown.
- `test_limits_and_controls.py`: Kill switch (env var and file), anomaly circuit breakers, approval expiration, and capital/event exposure caps.
- `test_fixtures.py`: Polymarket & Kalshi API response fixtures and parser regression tests.

---

## 7. Risk controls & Safeguards

- **Kill Switch**: Immediate halt via `python -m polyarb kill` or `POLYARB_KILL_SWITCH=1`.
- **Anomaly Circuit Breaker**: Blocks abnormally large edges (> $0.35/share) which invariably indicate bad quotes or rule mismatches.
- **Exposure Caps**: Hard limits per relation ($500), per underlying event ($1,000), and total portfolio exposure ($5,000).
- **Legging Simulation**: Realistic delay modeling (e.g. 250ms) between legs to measure fill probability and unwind cost.
- **Resolution Risk Tooling**: Auto-flags different resolution sources, cutoff time discrepancies (> 6h/24h), "by" vs "in" timing semantics, and opposite polarity phrasing.
- **Approval Expiration**: Human approvals expire after 30 days to force periodic re-validation.

---

## 8. Safety

- This is research code. Paper trading only.
- Resolution rules differ between markets and venues; a "guaranteed" arb can
  lose if the rules differ. Always read both before approving.
- Never put private keys or wallet seeds in this project or any third-party bot
  repo.
- Confirm eligibility and terms of service for each venue in your jurisdiction
  before any live trading. This is not legal or financial advice.
