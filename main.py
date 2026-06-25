"""
main.py
=======
The async orchestrator. It wires together:

    BinanceFeed      -> live BTC momentum
    MarketScanner    -> the current BTC 15-min up/down target market
    OrderBookStream  -> live best bid/ask for the target's two tokens
    Strategy         -> the signal
    RiskManager      -> sizing, slippage, spread, daily stop-loss
    PolymarketExchange / paper logger -> execution
    TradeDB          -> persistence

Concurrency model (single asyncio loop):
    * binance_task : never-ending Binance trade stream.
    * scanner_task : every SCAN_INTERVAL_SEC, refresh the target market and, if
                     it changed, (re)start the order-book stream for its tokens.
    * eval_task    : every EVAL_INTERVAL_SEC, run the strategy + risk + execute.
    * risk_task    : every few seconds, evaluate the daily stop-loss.

The stop-loss is the only thing that can end the process from the inside: when
it trips, we cancel all open orders and trigger a graceful shutdown.
"""

from __future__ import annotations

import asyncio
import signal
import time

from config import PAPER_TRADE, STRAT
from binance_feed import BinanceFeed
from database import TradeDB
from exchange import OrderBookStream, PolymarketExchange
from market_scanner import MarketScanner, TargetMarket
from risk import RiskManager
from strategy import Strategy
from config import DB_PATH


class TradingBot:
    def __init__(self) -> None:
        self.db = TradeDB(DB_PATH)
        self.feed = BinanceFeed()
        self.scanner = MarketScanner()
        self.strategy = Strategy()
        self.risk = RiskManager(self.db)
        self.exchange = PolymarketExchange()

        # Mutable shared state guarded by the single-loop model (no locks
        # needed because all mutation happens on the event loop thread).
        self.target: TargetMarket | None = None
        self.book_stream: OrderBookStream | None = None
        self._book_task: asyncio.Task | None = None

        # Avoid spamming orders: at most one live entry per token per market.
        self._traded_tokens: set[str] = set()

        self._shutdown = asyncio.Event()

    # ====================================================================== run
    async def run(self) -> None:
        mode = "PAPER" if PAPER_TRADE else "LIVE"
        print(f"=== BTC 15-min bot starting in {mode} mode ===")

        # Authenticate the exchange only when we intend to trade for real.
        if not PAPER_TRADE:
            await self.exchange.connect()

        # Launch the long-lived feed.
        binance_task = asyncio.create_task(self.feed.start(), name="binance")
        scanner_task = asyncio.create_task(self._scanner_loop(), name="scanner")
        eval_task = asyncio.create_task(self._eval_loop(), name="eval")
        risk_task = asyncio.create_task(self._risk_loop(), name="risk")

        # Wire OS signals (Ctrl-C / kill) to a clean shutdown.
        self._install_signal_handlers()

        await self._shutdown.wait()  # block until something asks us to stop

        # ---- graceful teardown ------------------------------------------
        print("[main] shutting down...")
        for t in (binance_task, scanner_task, eval_task, risk_task):
            t.cancel()
        if self._book_task:
            self._book_task.cancel()
        self.feed.stop()
        if self.book_stream:
            self.book_stream.stop()

        # Best-effort: pull any resting orders before we exit.
        if not PAPER_TRADE:
            await self.exchange.cancel_all()

        await self.scanner.close()
        self.db.close()
        print("[main] clean exit.")

    # ============================================================ scanner loop
    async def _scanner_loop(self) -> None:
        """Refresh the target market; (re)subscribe the book stream on change."""
        while not self._shutdown.is_set():
            try:
                now = time.time()
                found = await self.scanner.find_active_15m_market(now)

                # Detect a change of target (new slug) or first acquisition.
                changed = found is not None and (
                    self.target is None or found.slug != self.target.slug
                )
                if changed:
                    self.target = found
                    self._traded_tokens.clear()
                    await self._restart_book_stream(found)
                elif found is None and self.target is not None:
                    # Current target rolled off (resolved). Drop it.
                    if self.target.seconds_to_resolve(now) <= 0:
                        print("[scanner] target resolved; clearing.")
                        self.target = None
                        await self._stop_book_stream()
            except Exception as exc:  # noqa: BLE001 - never kill the loop
                print(f"[scanner] loop error: {exc}")
            await asyncio.sleep(STRAT.SCAN_INTERVAL_SEC)

    async def _restart_book_stream(self, target: TargetMarket) -> None:
        await self._stop_book_stream()
        self.book_stream = OrderBookStream(
            [target.up_token_id, target.down_token_id]
        )
        self._book_task = asyncio.create_task(
            self.book_stream.run(), name="book"
        )
        print(f"[main] book stream (re)started for '{target.slug}'")

    async def _stop_book_stream(self) -> None:
        if self.book_stream:
            self.book_stream.stop()
        if self._book_task:
            self._book_task.cancel()
            self._book_task = None
        self.book_stream = None

    # =============================================================== eval loop
    async def _eval_loop(self) -> None:
        """Run the strategy on a fixed cadence and act on any signal."""
        while not self._shutdown.is_set():
            await asyncio.sleep(STRAT.EVAL_INTERVAL_SEC)

            if self.risk.halted:
                continue
            if self.target is None or self.book_stream is None:
                continue

            up_book = self.book_stream.books.get(self.target.up_token_id)
            down_book = self.book_stream.books.get(self.target.down_token_id)
            if up_book is None or down_book is None:
                continue

            momentum = self.feed.momentum(STRAT.MOMENTUM_WINDOW_SEC)

            decision = self.strategy.evaluate(
                self.target, up_book, down_book, momentum
            )

            # Log every evaluation that had a directional view (for analysis).
            if decision.direction is not None:
                await asyncio.to_thread(
                    self.db.log_signal,
                    ts=time.time(),
                    market_slug=self.target.slug,
                    token_id=(decision.signal.token_id if decision.signal else None),
                    direction=decision.direction,
                    momentum_pct=momentum,
                    fair_price=decision.fair_price,
                    pm_midpoint=decision.pm_midpoint,
                    edge=decision.edge,
                    fired=1 if decision.signal else 0,
                    abort_reason=decision.abort_reason,
                )

            if decision.signal is None:
                continue

            await self._handle_signal(decision)

    async def _handle_signal(self, decision) -> None:
        sig = decision.signal
        # One entry per token per market — don't pyramid into the same side.
        if sig.token_id in self._traded_tokens:
            return

        # Build the risk-checked, slippage-capped, $2-sized order.
        order, reason = self.risk.build_buy_order(
            token_id=sig.token_id,
            reference_price=sig.reference_price,
            best_ask=sig.best_ask,
        )
        if order is None:
            print(f"[risk] order rejected: {reason}")
            return

        print(
            f"[signal] {sig.direction} {self.target.slug} | "
            f"mom={sig.momentum_pct:+.3%} fair={sig.fair_price:.3f} "
            f"mid={sig.pm_midpoint:.3f} edge={sig.edge:+.3f} -> "
            f"BUY {order.size_shares}@{order.limit_price} (${order.notional_usd})"
        )

        if PAPER_TRADE:
            await self._paper_execute(order, sig)
        else:
            await self._live_execute(order, sig)

        self._traded_tokens.add(sig.token_id)

    # ----------------------------------------------------------- execution: paper
    async def _paper_execute(self, order, sig) -> None:
        """Bypass the venue. Log the intended trade to `paper_trades`."""
        ts = time.time()
        await asyncio.to_thread(
            self.db.log_paper_trade,
            ts=ts,
            market_slug=self.target.slug,
            token_id=order.token_id,
            side=order.side,
            size_shares=order.size_shares,
            price=order.limit_price,
            notional_usd=order.notional_usd,
            momentum_pct=sig.momentum_pct,
            edge=sig.edge,
            reason=f"{sig.direction} fair={sig.fair_price:.3f}",
        )
        # In paper mode we OPTIMISTICALLY assume a full fill at the limit price
        # so PnL bookkeeping has a cost basis. Real fills suffer adverse
        # selection — treat paper PnL as an upper bound, not a forecast.
        await asyncio.to_thread(
            self.db.record_fill,
            ts=ts,
            token_id=order.token_id,
            side=order.side,
            size_shares=order.size_shares,
            price=order.limit_price,
            market_slug=self.target.slug,
        )
        print("[paper] logged intended trade + simulated fill")

    # ------------------------------------------------------------ execution: live
    async def _live_execute(self, order, sig) -> None:
        """Submit a real GTC limit order and persist the result."""
        ts = time.time()
        try:
            resp = await self.exchange.place_limit_order(
                token_id=order.token_id,
                side=self.exchange.buy_side(),
                size_shares=order.size_shares,
                price=order.limit_price,
            )
        except Exception as exc:  # noqa: BLE001 - record failure, keep running
            print(f"[live] order error: {exc}")
            return

        order_id = (resp or {}).get("orderID") or (resp or {}).get("orderId")
        status = (resp or {}).get("status")
        await asyncio.to_thread(
            self.db.log_live_trade,
            ts=ts,
            market_slug=self.target.slug,
            token_id=order.token_id,
            side=order.side,
            size_shares=order.size_shares,
            price=order.limit_price,
            notional_usd=order.notional_usd,
            order_id=order_id,
            status=status,
            raw_response=str(resp),
        )
        # Record a fill optimistically for PnL tracking. In a fuller build you
        # would confirm fills via the CLOB *user* websocket channel instead.
        await asyncio.to_thread(
            self.db.record_fill,
            ts=ts,
            token_id=order.token_id,
            side=order.side,
            size_shares=order.size_shares,
            price=order.limit_price,
            market_slug=self.target.slug,
        )
        print(f"[live] order submitted: id={order_id} status={status}")

    # =============================================================== risk loop
    async def _risk_loop(self) -> None:
        """Evaluate the daily stop-loss; halt the whole bot if tripped."""
        while not self._shutdown.is_set():
            await asyncio.sleep(5)
            mids = self._current_mids()
            if self.risk.check_stop_loss(mids):
                # Cancel everything first (live), then trigger shutdown.
                if not PAPER_TRADE:
                    await self.exchange.cancel_all()
                print("[risk] halt requested; stopping bot.")
                self._shutdown.set()
                return

    def _current_mids(self) -> dict[str, float]:
        """Latest midpoints for any tokens we might hold, for marking PnL."""
        mids: dict[str, float] = {}
        if self.book_stream:
            for token, book in self.book_stream.books.items():
                if book.midpoint is not None:
                    mids[token] = book.midpoint
        return mids

    # ============================================================ os signals
    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig_name in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig_name, self._shutdown.set)
            except NotImplementedError:
                # add_signal_handler is unavailable on some platforms (Windows).
                pass


def main() -> None:
    bot = TradingBot()
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        print("\n[main] interrupted.")


if __name__ == "__main__":
    main()
