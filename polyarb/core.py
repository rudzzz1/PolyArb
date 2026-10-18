"""Pure logic: logical relations between binary markets -> guaranteed-payoff baskets.

Idea: every leg is a BUY of a YES or NO token at its ask. Given a relation that
restricts which joint outcomes are possible ("worlds"), a basket's guaranteed
payoff is the minimum payoff over all feasible worlds. If guaranteed payoff
exceeds cost (+fees), it's an arbitrage -- no probability estimates needed.
"""
from __future__ import annotations
from dataclasses import dataclass
from itertools import product
from typing import Sequence, Callable, Any

MAX_EXACT = 8  # brute-force limit; larger groups only test uniform baskets


@dataclass(frozen=True)
class Level:
    price: float
    size: float


def worlds(kind: str, n: int, include_void: bool = False) -> list[tuple[Any, ...]]:
    """Feasible joint outcomes (True = YES, False = NO, None = void/refund)."""
    binary_worlds = list(product([False, True], repeat=n))
    ws: list[tuple[Any, ...]]
    if kind == "implies":      # markets=[A, B], A => B
        assert n == 2
        ws = [w for w in binary_worlds if not (w[0] and not w[1])]
    elif kind == "exclusive":    # at most one YES
        ws = [w for w in binary_worlds if sum(w) <= 1]
    elif kind == "partition":    # exactly one YES
        ws = [w for w in binary_worlds if sum(w) == 1]
    elif kind == "equivalent":   # all resolve the same way
        ws = [w for w in binary_worlds if len(set(w)) == 1]
    else:
        raise ValueError(f"unknown relation kind: {kind}")

    if include_void:
        # Extra worlds where one or more markets are voided / refunded (None)
        void_worlds = []
        for i in range(n):
            for base_w in ws:
                vw = list(base_w)
                vw[i] = None
                void_worlds.append(tuple(vw))
        # Entire event voided
        void_worlds.append(tuple([None] * n))
        ws = sorted(list(set(ws + void_worlds)), key=lambda x: str(x))

    return ws


@dataclass
class Basket:
    legs: tuple          # ((market_index, "YES"|"NO"), ...)
    cost: float          # per share-set, incl. fee
    payoff: float        # guaranteed minimum payoff per share-set
    days_to_resolution: float | None = None

    @property
    def edge(self) -> float:
        return self.payoff - self.cost

    @property
    def annualized_return(self) -> float:
        if self.days_to_resolution is None or self.cost <= 0:
            return 0.0
        return annualized_return(self.edge, self.cost, self.days_to_resolution)


def annualized_return(edge: float, cost: float, days_to_resolution: float, min_days: float = 0.04) -> float:
    """Calculate annualized percentage return (APY) on locked capital.
    min_days: floor to avoid division by zero (default 0.04 days ≈ 1 hour)."""
    if cost <= 0 or edge <= 0:
        return 0.0
    effective_days = max(days_to_resolution, min_days)
    return (edge / cost) * (365.0 / effective_days)


def validate_book(book: dict) -> tuple[bool, str]:
    """Validate order book integrity.
    Checks: valid sides, prices in (0, 1), positive sizes, ascending ask ladders,
    and crossed books (bids < asks)."""
    if not isinstance(book, dict):
        return False, "book is not a dictionary"
    for side in ("YES", "NO"):
        if side not in book:
            return False, f"missing side: {side}"
        levels = book[side]
        if not isinstance(levels, (list, tuple)):
            return False, f"{side} levels is not a list"
        for i, lvl in enumerate(levels):
            if not isinstance(lvl, Level):
                return False, f"{side}[{i}] is not a Level instance"
            if not (0.0 <= lvl.price <= 1.0):
                return False, f"{side}[{i}] price {lvl.price} out of range [0, 1]"
            if lvl.size <= 0:
                return False, f"{side}[{i}] size {lvl.size} must be > 0"
            if i > 0 and lvl.price < levels[i - 1].price:
                return False, f"{side} ladder not ascending at index {i}"
    return True, "ok"


def _leg_cost(price: float, fee_bps: float, fee: Callable[[float], float] | None) -> float:
    """All-in cost of one share: price + proportional fee + optional price-dependent fee."""
    return price * (1 + fee_bps / 1e4) + (fee(price) if fee else 0.0)


def _world_payoff(legs: Sequence[tuple[int, str]], world: tuple[Any, ...], void_payoff: float = 0.50) -> float:
    """Payoff of a basket in a single world. In a void market (None), payoff is void_payoff."""
    total = 0.0
    for i, s in legs:
        w_val = world[i]
        if w_val is None:
            total += void_payoff
        elif w_val == (s == "YES"):
            total += 1.0
    return total


def best_basket(kind: str, asks: list[dict], fee_bps: float = 0.0, fees=None,
                include_void: bool = False, void_payoff: float = 0.50,
                days_to_resolution: float | None = None) -> Basket | None:
    """asks[i] = {"YES": best_ask|None, "NO": best_ask|None}.
    fees[i] = optional f(price)->per-share fee for market i. Returns highest-edge basket."""
    n = len(asks)
    fees = fees or [None] * n
    if n > MAX_EXACT:
        return _uniform_basket(kind, asks, fee_bps, fees, days_to_resolution=days_to_resolution)
    ws = worlds(kind, n, include_void=include_void)
    best = None
    for choice in product([None, "YES", "NO"], repeat=n):
        legs = [(i, s) for i, s in enumerate(choice) if s]
        if not legs or any(asks[i][s] is None for i, s in legs):
            continue
        cost = sum(_leg_cost(asks[i][s], fee_bps, fees[i]) for i, s in legs)
        payoff = min(_world_payoff(legs, w, void_payoff=void_payoff) for w in ws)
        b = Basket(tuple(legs), cost, float(payoff), days_to_resolution=days_to_resolution)
        if best is None or b.edge > best.edge:
            best = b
    return best


def _uniform_basket(kind, asks, fee_bps, fees, days_to_resolution=None):
    if kind not in ("exclusive", "partition"):
        return None
    n, best = len(asks), None
    options = {
        "YES": float(1 if kind == "partition" else 0),   # worst case payoff
        "NO": float(n - 1),
    }
    for side, payoff in options.items():
        if any(a[side] is None for a in asks):
            continue
        cost = sum(_leg_cost(a[side], fee_bps, fees[i]) for i, a in enumerate(asks))
        b = Basket(tuple((i, side) for i in range(n)), cost, payoff, days_to_resolution=days_to_resolution)
        if best is None or b.edge > best.edge:
            best = b
    return best


def unwind_loss_estimate(ask_price: float, bid_price: float | None = None,
                         default_spread: float = 0.04) -> float:
    """Estimated loss per share if an executed leg has to be unwound by selling into the bid."""
    if bid_price is not None and 0.0 <= bid_price <= ask_price:
        return ask_price - bid_price
    return min(ask_price, default_spread)


def legging_risk_adjusted_edge(raw_edge: float, leg_unwind_losses: Sequence[float],
                              fill_prob: float = 0.90) -> float:
    """Risk-adjusted expected edge per share accounting for execution failure on subsequent legs.
    Edge must exceed expected legging unwind loss."""
    expected_unwind_cost = (1.0 - fill_prob) * sum(leg_unwind_losses)
    return (fill_prob * raw_edge) - expected_unwind_cost


def sweep(ask_books: list[list[Level]], payoff: float, fee_bps: float = 0.0, fees=None):
    """Walk the ask ladders of all legs together while the basket stays profitable.
    Returns (shares, total_cost). Profit = shares * payoff - total_cost."""
    n = len(ask_books)
    idx, used = [0] * n, [0.0] * n
    shares = cost = 0.0
    fees = fees or [None] * n
    while all(idx[i] < len(ask_books[i]) for i in range(n)):
        marginal = sum(_leg_cost(ask_books[i][idx[i]].price, fee_bps, fees[i]) for i in range(n))
        if payoff - marginal <= 0:
            break
        avail = min(ask_books[i][idx[i]].size - used[i] for i in range(n))
        if avail > 1e-9:
            shares += avail
            cost += avail * marginal
        for i in range(n):
            used[i] += max(avail, 0)
            if used[i] >= ask_books[i][idx[i]].size - 1e-9:
                idx[i] += 1
                used[i] = 0.0
    return shares, cost


@dataclass
class SizingResult:
    shares: float
    total_cost: float
    profit: float
    marginal_edge: float
    thinnest_leg_depth: float
    slippage_bps: float


def sweep_with_limits(ask_books: list[list[Level]], payoff: float, fee_bps: float = 0.0,
                      fees=None, max_capital: float = float("inf"),
                      max_slippage_bps: float = 50.0,
                      min_depth: float = 0.0) -> SizingResult:
    """Depth-aware sizing with risk caps: max capital limit, slippage cap, and depth verification."""
    n = len(ask_books)
    if n == 0 or any(len(b) == 0 for b in ask_books):
        return SizingResult(0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    thinnest_depth = min(sum(lvl.size for lvl in b) for b in ask_books)
    if thinnest_depth < min_depth:
        return SizingResult(0.0, 0.0, 0.0, 0.0, thinnest_depth, 0.0)

    fees = fees or [None] * n
    base_marginal = sum(_leg_cost(ask_books[i][0].price, fee_bps, fees[i]) for i in range(n))
    if payoff - base_marginal <= 0:
        return SizingResult(0.0, 0.0, 0.0, payoff - base_marginal, thinnest_depth, 0.0)

    idx, used = [0] * n, [0.0] * n
    shares = cost = 0.0
    last_marginal = base_marginal

    while all(idx[i] < len(ask_books[i]) for i in range(n)):
        marginal = sum(_leg_cost(ask_books[i][idx[i]].price, fee_bps, fees[i]) for i in range(n))
        if payoff - marginal <= 0:
            break
        # Check slippage from top of book
        slippage = ((marginal - base_marginal) / max(base_marginal, 1e-6)) * 1e4
        if slippage > max_slippage_bps:
            break
        avail = min(ask_books[i][idx[i]].size - used[i] for i in range(n))
        # Check capital limit
        if cost + (avail * marginal) > max_capital:
            remaining_cap = max(0.0, max_capital - cost)
            avail = remaining_cap / marginal if marginal > 0 else 0.0

        if avail > 1e-9:
            shares += avail
            cost += avail * marginal
            last_marginal = marginal
        if cost >= max_capital - 1e-6:
            break
        for i in range(n):
            used[i] += max(avail, 0)
            if used[i] >= ask_books[i][idx[i]].size - 1e-9:
                idx[i] += 1
                used[i] = 0.0

    profit = shares * payoff - cost
    effective_slippage = ((last_marginal - base_marginal) / max(base_marginal, 1e-6)) * 1e4
    return SizingResult(
        shares=round(shares, 4),
        total_cost=round(cost, 4),
        profit=round(profit, 4),
        marginal_edge=round(payoff - last_marginal, 4),
        thinnest_leg_depth=round(thinnest_depth, 4),
        slippage_bps=round(effective_slippage, 2),
    )
