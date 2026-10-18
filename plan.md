# plan.md — improvements to reduce risk and increase profitability

Ordered by what most changes the go/no-go decision. Effort: S (hours), M (days),
L (weeks). Items marked *(verify)* rest on platform details I have not confirmed.

---

## 0. Decide whether there is an edge at all (do this before building more)

1. Run `doctor`, fix field mismatches. **[S]**
2. Approve a small, high-quality set of relations. Start with date ladders and
   numeric threshold ladders inside one venue, plus a few hand-checked
   cross-venue pairs. **[S]**
3. Record 1–2 weeks with `stream`. **[S]**
4. Run `replay` and judge against gates, set *before* looking at results:

| Gate (suggested starting values)                                | Why                          |
|-----------------------------------------------------------------|------------------------------|
| ≥ 20 episodes/week after fees at your min size                  | Enough to measure anything   |
| ≥ 30% still alive 15s after detection                           | Capturable with your latency |
| Positive net profit at the 15s horizon, after fees              | Survives realistic delay     |
| Annualised return on locked capital above your hurdle           | Beats parking cash           |

**Kill criteria:** if nearly all edges die within one poll interval and none
survive fees, stop adding strategy code. The in-venue arb is being taken by
faster bots. Pivot to slower, rules-heavy cross-venue relations, or to market
making.

---

## 1. Risk reduction

### 1.1 Resolution risk (the biggest real-world killer)  **[M]**
- Store each market's resolution rules text, source, cutoff time and timezone
  in `markets.json`; show both side by side when approving.
- Auto-flag mismatches: different resolution source, "by" vs "in", different
  cutoff time, different handling of ties, delays or cancellations.
- Detect opposite polarity in the matcher (win vs lose, above vs below, "not").
- Record who approved a relation and when; expire approvals after N days.
- Model void/N-A outcomes (e.g. 50/50 or refund resolutions). The basket worst
  case should include them, as extra "worlds" in `core.worlds()`.
- Track dispute and delayed-resolution history per category; avoid categories
  where it is common.

### 1.2 Legging and execution risk  **[M]**
- Simulate leg-by-leg execution in replay: fill the thinner or slower leg first,
  then the second after a delay. Report P(second leg fills) and the loss when it
  does not.
- Fill-or-kill / limit-at-ask logic with a max-slippage cap per leg.
- Pre-plan the unwind: price the cost of exiting a half-built basket and include
  it in the required edge (edge must exceed expected legging loss).
- Require minimum depth on the *thinnest* leg, not just top of book.

### 1.3 Data integrity  **[M]**
- Handle recorded disconnect gaps in `replay` (events table already logs them):
  treat state before a gap as stale.
- Validate books: crossed books, prices outside [0,1], zero-size levels, hash
  or sequence checks where the venue provides them *(verify)*.
- Use venue timestamps alongside local receive time to quantify clock skew.
- Phantom-arb filter: require the edge in two consecutive observations, or both
  legs' timestamps within a tolerance, before counting an episode.
- Store raw websocket messages for a short window so bugs in book rebuilding can
  be replayed and diagnosed.

### 1.4 Fees and all-in costs  **[S–M]**
- Per-market fee schedules instead of one global value: some Polymarket market
  types charge taker fees *(verify)*; Kalshi's formula, rounding and maker/taker
  differences need checking against their current schedule.
- Add non-trading costs to the edge: on-chain gas, bridging, deposits and
  withdrawals, FX spread, and any tax treatment.
- Make the fee model part of `replay` output (gross vs net) so you see how much
  of the edge fees consume.

### 1.5 Capital and exposure limits  **[S]**
- Hard caps: per relation, per event, per venue, global; a daily loss limit.
- Concentration check: many "different" relations may share one underlying
  event or one resolution risk. Cap exposure per underlying.
- Capital is locked until resolution; track balances per venue and the cost of
  moving money between them.

### 1.6 Venue and counterparty risk  **[M]**
- Limit balance held at each venue; plan for withdrawal delays, trading halts
  and API outages. Cross-venue positions are only hedged if both venues pay out.
- Confirm eligibility, KYC and terms of service in your jurisdiction for each
  venue before any live step. Not legal advice; get a professional opinion.

### 1.7 Controls and monitoring  **[S–M]**
- Kill switch (file flag or env var) checked on every loop.
- Circuit breakers: stop on repeated errors, stale feeds, or abnormal edge
  sizes. Abnormally large edges are usually data bugs or rule mismatches.
- Alerts (Telegram or email) for disconnects, error spikes, and any live order.

### 1.8 Testing and engineering hygiene  **[M]**
- Property-based tests for `core.py`: random relations, compare `best_basket`
  to an independent brute-force payoff calculator.
- Save real API responses as fixtures; add parsing tests so API drift is caught.
- Prune stale relations when a market closes (currently errors on missing
  markets after `discover`).
- Data retention: roll old `snaps` into Parquet; add a size cap and a
  compaction job.
- Central config file, structured logging, CI running the tests.

---

## 2. Profitability improvements

### 2.1 Rank by annualised return, not raw edge  **[S]**  ← high impact
A 2-cent edge locked for six months is worse than 0.5 cents locked for two
days. Add `days_to_resolution` from end dates; report and sort by
`edge / cost × 365 / days`. Use it as the sizing and selection filter.

### 2.2 Wider relation coverage  **[M]**
- Numeric threshold ladders parsed from titles ("above 100k" ⇒ "above 90k"),
  inside and across venues.
- Nested events (winning a primary ⊇ winning the office, a team winning the
  title ⊆ reaching the final).
- Multi-leg baskets of 3+ markets (date × threshold grids).
- Neg-risk conversion groups, where the platform's own mechanics matter *(verify)*.
- Prioritise categories with many structurally related markets (elections,
  crypto thresholds, sports futures).

### 2.3 Better sizing: LP and portfolio view  **[M]**
Replace equal-share baskets with a linear program that chooses leg weights to
maximise guaranteed profit subject to depth, capital and per-venue limits. It
can also net multiple relations that share legs.

### 2.4 Better matching  **[M]**
- Use structured fields (series and event identifiers, tags, categories) before
  free text.
- Embedding similarity or an LLM judge as a *ranking* aid, always followed by
  human approval.
- Normalise dates and entities (names, tickers, thresholds, units).

### 2.5 Maker-side execution  **[L]**
Post a resting order on the less liquid leg and take the other leg only when it
fills. This can save spread and earn rewards *(verify)*, at the cost of
adverse selection and legging risk. Needs trade-print recording and a queue-aware
fill simulator to evaluate honestly.

### 2.6 Record more data  **[M]**
- Store bids and trade prints (`last_trade_price` events), not just asks. This
  enables realistic fill simulation and a market-making backtest.
- Kalshi websocket (needs an API key and request signing) to remove its polling
  bottleneck on cross-venue edges.

### 2.7 Latency reduction (only if edges are fast)  **[L]**
Parallel leg submission, pre-signed orders, run near the venue's servers.
Do not invest here until `replay` shows edges that survive sub-second delays.

### 2.8 Capital efficiency  **[M]**
Use venue mechanics that release capital early (merging or converting
positions, closing hedged pairs) *(verify)*. Prefer short-dated relations.

### 2.9 Second strategy: market making  **[L]**
Quote both sides on liquid, mid-priced markets using recorded bids. Measure
post-fill price drift; if fills are consistently followed by adverse moves,
there is no edge. Shares the recorder and replay infrastructure.

### 2.10 Analytics  **[S–M]**
Edge-decay curves, episodes by category and relation type, profit per
capital-day, gross vs net of fees, and a simple dashboard.

---

## 3. Suggested sequence

| Phase | Work                                                                   | Outcome                      | Status |
|-------|------------------------------------------------------------------------|------------------------------|--------|
| A     | doctor, approve a few relations, stream 1–2 weeks, replay (§0)         | Is there an edge? Go/kill.   | Ready  |
| B     | 2.1 annualised ranking, 1.3 gap handling and phantom filter, 1.4 fees  | Trustworthy numbers          | **DONE** (Implemented & tested) |
| C     | 1.1 resolution tooling, 2.2 threshold ladders, 2.4 polarity matching   | More, safer relations        | **DONE** (Implemented & tested) |
| D     | 1.2 legging simulation, 2.6 full books (bids/asks), 2.3 sizing limits  | Realistic paper PnL          | **DONE** (Implemented & tested) |
| E     | 1.5–1.7 exposure caps, kill switch, circuit breakers, approval expiry  | Safe to consider tiny live   | **DONE** (Implemented & tested) |
| F     | 2.5 maker execution or 2.9 market making                               | Second source of edge        | Backlog |

## 4. Gate for any live trading

All of the following, in order, with nothing skipped:
1. Several weeks of recorded data with a positive net result after fees,
   latency and simulated legging loss, across more than one market regime.
2. A written resolution review for every approved relation.
3. Confirmed legal and ToS eligibility for each venue.
4. Hard exposure limits, a kill switch and alerts in place and tested.
5. Start at a size you can afford to lose entirely; compare live fills against
   what `replay` predicted before scaling anything.

Profit is not guaranteed. Most retail attempts lose to faster participants,
fees and resolution surprises; the point of this project is to find out cheaply
whether this one does too.
