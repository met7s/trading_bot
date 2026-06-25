"""
risk.py
=======
The survival layer. Everything that decides *whether* and *how big* a trade may
be, plus the daily kill-switch.

Position sizing rule (hard-coded):
    Each trade commits exactly RISK.POSITION_SIZE_USD ($2) of USDC. On
    Polymarket an order's `size` is a number of SHARES and `price` is the per-
    share probability (0-1). So a $2 commitment at limit price p means:
        size_shares = 2.0 / p

Slippage rule:
    We submit LIMIT orders only. For a BUY we cap the limit at
        reference * (1 + MAX_SLIPPAGE)
    and also never cross above the live best_ask by more than that. If the
    cheapest available ask already exceeds the cap, we abort.

Daily stop-loss (the part everyone underestimates):
    PnL over the trailing 24h = realized + unrealized.
      realized   = for fills of markets that have RESOLVED, payout - cost.
      unrealized = for fills of markets still OPEN, (current_mid - cost) * shares.
    If that total <= DAILY_STOP_LOSS (-$15), halt.

    NOTE: precise PnL needs resolution data. record_resolution() must be fed by
    a resolution poller (a stub is provided in main.py). Until a market resolves
    its fills are marked-to-market against the live midpoint.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from config import RISK
from database import TradeDB


@dataclass
class SizedOrder:
    token_id: str
    side: str
    size_shares: float
    limit_price: float
    notional_usd: float


class RiskManager:
    def __init__(self, db: TradeDB) -> None:
        self._db = db
        self._halted = False

    @property
    def halted(self) -> bool:
        return self._halted

    # --------------------------------------------------------- sizing & price
    def quantize_price(self, price: float) -> float:
        """Round to the venue tick (default 1 cent)."""
        tick = RISK.PRICE_TICK
        return round(round(price / tick) * tick, 4)

    def build_buy_order(
        self,
        token_id: str,
        reference_price: float,
        best_ask: float,
    ) -> tuple[SizedOrder | None, str | None]:
        """
        Turn a buy intent into a concrete, slippage-capped, correctly-sized
        order, or return (None, reason) if risk rules forbid it.
        """
        # 1) Slippage cap relative to the reference (midpoint) price.
        max_price = reference_price * (1.0 + RISK.MAX_SLIPPAGE)

        # 2) We must be able to buy at or below the cap. If the best ask is
        #    already above the cap, the trade can't fill within slippage.
        if best_ask > max_price:
            return None, (
                f"best_ask {best_ask:.3f} exceeds slippage cap {max_price:.3f}"
            )

        # 3) Our limit = the best ask (marketable at top-of-book) but never
        #    above the cap. Quantize to the tick.
        limit_price = self.quantize_price(min(best_ask, max_price))

        # Guard against degenerate prices.
        if limit_price <= 0.0 or limit_price >= 1.0:
            return None, f"limit price {limit_price} out of (0,1)"

        # 4) Fixed $2 notional => shares = dollars / price.
        size_shares = round(RISK.POSITION_SIZE_USD / limit_price, 2)
        if size_shares <= 0:
            return None, "computed size is zero"

        notional = round(size_shares * limit_price, 4)
        return (
            SizedOrder(token_id, "BUY", size_shares, limit_price, notional),
            None,
        )

    # ------------------------------------------------------- spread (explicit)
    def spread_ok(self, best_bid: float, best_ask: float) -> bool:
        """True if the book is tight enough to trade."""
        return (best_ask - best_bid) <= RISK.MAX_SPREAD

    # ------------------------------------------------------------- PnL / halt
    def compute_pnl_last_24h(self, current_mids: dict[str, float]) -> float:
        """
        Sum realized + unrealized PnL across fills in the trailing window.

        current_mids: latest midpoint per token_id, for marking open positions.
        Returns a signed dollar figure (negative == loss).
        """
        since = time.time() - RISK.PNL_WINDOW_SEC
        fills = self._db.fills_since(since)

        pnl = 0.0
        for f in fills:
            token = f["token_id"]
            shares = float(f["size_shares"])
            cost_px = float(f["price"])
            side = f["side"]

            # We treat the strategy as buy-only entries; a SELL would be an
            # exit and flips the sign. Keep it general.
            sign = 1.0 if side == "BUY" else -1.0
            cost = sign * shares * cost_px

            payout = self._db.resolution_for(token)
            if payout is not None:
                # Realized: winning token pays 1.0/share, losing pays 0.0.
                value = sign * shares * payout
                pnl += value - cost
            else:
                # Unrealized: mark to current midpoint if we have one.
                mid = current_mids.get(token)
                if mid is None:
                    continue  # can't mark; skip rather than guess
                value = sign * shares * mid
                pnl += value - cost
        return round(pnl, 4)

    def check_stop_loss(self, current_mids: dict[str, float]) -> bool:
        """
        Evaluate the kill-switch. Returns True if the bot should HALT.
        Sets the internal halted flag so callers can branch on it.
        """
        if self._halted:
            return True
        pnl = self.compute_pnl_last_24h(current_mids)
        if pnl <= RISK.DAILY_STOP_LOSS:
            self._halted = True
            print(
                f"[risk] STOP-LOSS TRIPPED: 24h PnL {pnl:+.2f} "
                f"<= {RISK.DAILY_STOP_LOSS:+.2f}"
            )
            return True
        return False
