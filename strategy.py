"""
strategy.py
===========
Pure decision logic. No I/O, no SDK, no sockets — it takes the current market
target, the live books, and the momentum reading, and returns either a Signal
or None plus a reason. That makes it trivially unit-testable.

The chain of reasoning:
  1. Compute 3-minute momentum on Binance spot (passed in).
  2. If |momentum| < MOMENTUM_THRESHOLD (0.2%), do nothing.
  3. Pick the favoured direction (UP if momentum > 0 else DOWN) and its token.
  4. Translate momentum into a "fair" probability for that token:
        fair = clamp(0.50 + momentum * SENSITIVITY, FAIR_MIN, FAIR_MAX)
  5. Compare fair to the token's Polymarket midpoint. If the midpoint is
     LAGGING fair by more than LAG_THRESHOLD (4 cents), there's an edge.
  6. Spread gate: if best_ask - best_bid > MAX_SPREAD (3 cents), abort — the
     book is too thin/wide to trade safely.
  7. Emit a BUY signal on the favoured token.

CALIBRATION WARNING: SENSITIVITY (config) is the load-bearing assumption. The
mapping from a 3-min spot move to a 15-min resolution probability is an
empirical question; the placeholder value is a starting point, not an edge.
"""

from __future__ import annotations

from dataclasses import dataclass

from config import RISK, STRAT
from exchange import BookSide
from market_scanner import TargetMarket


@dataclass
class Signal:
    token_id: str
    direction: str            # "UP" or "DOWN"
    side: str                 # always a BUY in this strategy
    reference_price: float    # the midpoint we consider "fair-ish" to buy at
    fair_price: float         # model fair probability
    pm_midpoint: float        # current Polymarket midpoint for the token
    edge: float               # fair - midpoint (positive == underpriced)
    best_bid: float
    best_ask: float
    momentum_pct: float


@dataclass
class Decision:
    """Either a signal (fire) or a reason we stood down (for logging)."""
    signal: Signal | None
    direction: str | None
    fair_price: float | None
    pm_midpoint: float | None
    edge: float | None
    abort_reason: str | None


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def momentum_to_fair(momentum_pct: float) -> float:
    """Map a signed momentum fraction to a fair probability in [FAIR_MIN, MAX]."""
    raw = 0.50 + momentum_pct * STRAT.SENSITIVITY
    return _clamp(raw, STRAT.FAIR_MIN, STRAT.FAIR_MAX)


class Strategy:
    def evaluate(
        self,
        target: TargetMarket,
        up_book: BookSide,
        down_book: BookSide,
        momentum_pct: float | None,
    ) -> Decision:
        """Return a Decision. `signal` is non-None only when we should trade."""

        # --- Gate 0: do we even have a momentum reading yet? ----------------
        if momentum_pct is None:
            return Decision(None, None, None, None, None, "no_momentum_yet")

        # --- Gate 1: momentum strong enough? --------------------------------
        if abs(momentum_pct) < STRAT.MOMENTUM_THRESHOLD:
            return Decision(
                None, None, None, None, None,
                f"momentum {momentum_pct:+.4%} below threshold",
            )

        # --- Choose direction + the book/token for that side ----------------
        if momentum_pct > 0:
            direction = "UP"
            token_id = target.up_token_id
            book = up_book
        else:
            direction = "DOWN"
            token_id = target.down_token_id
            book = down_book

        # --- Gate 2: do we have a two-sided book for that token? ------------
        if book.best_bid is None or book.best_ask is None:
            return Decision(
                None, direction, None, None, None, "incomplete_book"
            )

        # --- Fair value + edge ---------------------------------------------
        # For DOWN we mirror the momentum sign so a strong down-move implies a
        # high DOWN probability (fair computed on the magnitude in that side's
        # favour).
        directional_momentum = momentum_pct if direction == "UP" else -momentum_pct
        fair = momentum_to_fair(directional_momentum)
        midpoint = book.midpoint  # guaranteed non-None given Gate 2
        edge = fair - midpoint    # positive => PM underpricing this side

        # --- Gate 3: spread check (a RISK rule, applied here on the book) ----
        spread = book.best_ask - book.best_bid
        if spread > RISK.MAX_SPREAD:
            return Decision(
                None, direction, fair, midpoint, edge,
                f"spread {spread:.3f} > max {RISK.MAX_SPREAD:.3f}",
            )

        # --- Gate 4: is the midpoint lagging fair by > LAG_THRESHOLD? --------
        if edge <= STRAT.LAG_THRESHOLD:
            return Decision(
                None, direction, fair, midpoint, edge,
                f"edge {edge:+.3f} <= lag threshold {STRAT.LAG_THRESHOLD:.3f}",
            )

        # --- All gates passed: emit a BUY signal on the favoured token ------
        signal = Signal(
            token_id=token_id,
            direction=direction,
            side="BUY",
            reference_price=midpoint,
            fair_price=fair,
            pm_midpoint=midpoint,
            edge=edge,
            best_bid=book.best_bid,
            best_ask=book.best_ask,
            momentum_pct=momentum_pct,
        )
        return Decision(signal, direction, fair, midpoint, edge, None)
