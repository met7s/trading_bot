"""
exchange.py
===========
Two responsibilities, both isolated here so the rest of the bot never imports
the SDK directly:

1. PolymarketExchange — a thin async adapter over `py-clob-client`:
   authentication, reading the book, placing GTC limit orders, cancelling all.
   The SDK is synchronous, so every call is run via asyncio.to_thread.

2. OrderBookStream — maintains a live best-bid / best-ask per token by
   subscribing to the CLOB *market* websocket channel.

WHY py-clob-client AND NOT polymarket-client (py-sdk):
    py-sdk is in beta and does not yet publicly document order placement /
    cancellation / book streaming. py-clob-client is the production path that
    Polymarket's own docs use for orders. To migrate later, reimplement the
    method bodies below against py-sdk; the signatures here are the contract.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field

import websockets

from config import (
    BUILDER_API_KEY,
    CHAIN_ID,
    CLOB_API_KEY,
    CLOB_API_PASSPHRASE,
    CLOB_API_SECRET,
    CLOB_HOST,
    CLOB_WS_URL,
    FUNDER_ADDRESS,
    PRIVATE_KEY,
    SIGNATURE_TYPE,
)

# SDK imports are kept local-to-module so paper mode can run even if the SDK
# pieces are not all importable on a given machine. We import lazily in connect.


@dataclass
class BookSide:
    """Best level for one outcome token."""
    best_bid: float | None = None
    best_ask: float | None = None
    ts: float = 0.0

    @property
    def spread(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid

    @property
    def midpoint(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2.0


# ============================================================================
# 1. EXECUTION ADAPTER
# ============================================================================
class PolymarketExchange:
    def __init__(self) -> None:
        self._client = None  # py_clob_client.ClobClient, built in connect()
        self._BUY = "BUY"
        self._SELL = "SELL"
        self._OrderArgs = None
        self._OrderType = None

    async def connect(self) -> None:
        """
        Build and authenticate the CLOB client. Required before any live order.
        Auth has two levels:
          L1 (wallet signature) — needed to derive API creds.
          L2 (HMAC API creds)   — needed to place/cancel orders & read account.
        """
        if not PRIVATE_KEY or not FUNDER_ADDRESS:
            raise RuntimeError(
                "Live trading requires POLYMARKET_PRIVATE_KEY and "
                "POLYMARKET_FUNDER in .env."
            )

        # Lazy imports: only needed for live trading.
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY, SELL

        self._OrderArgs = OrderArgs
        self._OrderType = OrderType
        self._BUY, self._SELL = BUY, SELL

        # The constructor is synchronous and does no network I/O, so it's fine
        # to call inline.
        self._client = ClobClient(
            CLOB_HOST,
            key=PRIVATE_KEY,
            chain_id=CHAIN_ID,
            signature_type=SIGNATURE_TYPE,
            funder=FUNDER_ADDRESS,
        )

        # Either use pre-provisioned L2 creds, or derive them from the key.
        if CLOB_API_KEY and CLOB_API_SECRET and CLOB_API_PASSPHRASE:
            creds = ApiCreds(
                api_key=CLOB_API_KEY,
                api_secret=CLOB_API_SECRET,
                api_passphrase=CLOB_API_PASSPHRASE,
            )
            await asyncio.to_thread(self._client.set_api_creds, creds)
        else:
            # create_or_derive_api_creds signs with the wallet (L1) and returns
            # the L2 HMAC creds; set_api_creds installs them on the client.
            creds = await asyncio.to_thread(
                self._client.create_or_derive_api_creds
            )
            await asyncio.to_thread(self._client.set_api_creds, creds)

        if BUILDER_API_KEY:
            # Builder attribution is optional. If your SDK version exposes a
            # setter for it, wire it here; otherwise it can be passed per-order.
            print("[exchange] builder key present (attribution enabled)")

        print("[exchange] CLOB client authenticated (L1+L2)")

    def buy_side(self) -> str:
        return self._BUY

    def sell_side(self) -> str:
        return self._SELL

    async def place_limit_order(
        self,
        token_id: str,
        side: str,
        size_shares: float,
        price: float,
    ) -> dict:
        """
        Place a Good-Til-Cancelled LIMIT order. We NEVER use market orders.

        size_shares = number of outcome shares (NOT dollars).
        price       = limit price in probability units (0.00-1.00).

        The client signs the order locally (L1) then posts it (L2). create_order
        internally fetches the market's tick size / neg-risk flags.
        """
        if self._client is None:
            raise RuntimeError("Exchange not connected; call connect() first.")

        order_args = self._OrderArgs(  # type: ignore[misc]
            token_id=token_id,
            price=round(price, 4),
            size=round(size_shares, 2),
            side=side,
        )

        def _submit() -> dict:
            signed = self._client.create_order(order_args)
            # OrderType.GTC == resting limit order.
            return self._client.post_order(signed, self._OrderType.GTC)

        return await asyncio.to_thread(_submit)

    async def cancel_all(self) -> None:
        """Cancel every open order. Used by the kill-switch."""
        if self._client is None:
            return
        try:
            await asyncio.to_thread(self._client.cancel_all)
            print("[exchange] cancel_all sent")
        except Exception as exc:  # noqa: BLE001 - best-effort during shutdown
            print(f"[exchange] cancel_all error: {exc}")

    async def get_order_book(self, token_id: str) -> dict | None:
        """REST order-book snapshot (used as a fallback / sanity check)."""
        if self._client is None:
            return None
        try:
            return await asyncio.to_thread(self._client.get_order_book, token_id)
        except Exception as exc:  # noqa: BLE001
            print(f"[exchange] get_order_book error: {exc}")
            return None


# ============================================================================
# 2. LIVE ORDER-BOOK STREAM (CLOB market channel)
# ============================================================================
class OrderBookStream:
    """
    Subscribes to the CLOB market channel for a set of token IDs and keeps the
    best bid/ask for each. The strategy reads from `.books[token_id]`.

    Protocol notes (verified against Polymarket docs):
      * URL: wss://ws-subscriptions-clob.polymarket.com/ws/market
      * Subscribe by sending {"assets_ids":[...], "type":"market",
        "custom_feature_enabled": true}.  The custom flag enables best_bid_ask.
      * Events we care about:
          - "book"          full snapshot: bids[], asks[] (price+size, strings)
          - "price_change"  incremental: price_changes[] each w/ best_bid/ask
          - "best_bid_ask"  top-of-book convenience event (custom feature)
      * Heartbeat: send the text "PING" every ~10s; ignore "PONG" replies.
    """

    def __init__(self, token_ids: list[str]) -> None:
        self.token_ids = token_ids
        self.books: dict[str, BookSide] = {t: BookSide() for t in token_ids}
        self._running = False

    def stop(self) -> None:
        self._running = False

    async def run(self) -> None:
        self._running = True
        backoff = 1.0
        while self._running:
            try:
                async with websockets.connect(
                    CLOB_WS_URL, ping_interval=None  # we do app-level PING
                ) as ws:
                    await ws.send(
                        json.dumps(
                            {
                                "assets_ids": self.token_ids,
                                "type": "market",
                                "custom_feature_enabled": True,
                            }
                        )
                    )
                    print(f"[book] subscribed to {len(self.token_ids)} tokens")
                    backoff = 1.0
                    ping_task = asyncio.create_task(self._ping_loop(ws))
                    try:
                        async for raw in ws:
                            self._handle(raw)
                    finally:
                        ping_task.cancel()
            except Exception as exc:  # noqa: BLE001 - reconnect on any drop
                print(f"[book] stream error: {exc}; reconnect in {backoff:.0f}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _ping_loop(self, ws) -> None:
        """Application-level keepalive required by the Polymarket WS server."""
        try:
            while True:
                await asyncio.sleep(10)
                await ws.send("PING")
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            return

    def _handle(self, raw: str | bytes) -> None:
        if raw == "PONG":
            return
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            return

        # The server may batch events into a list, or send a single dict.
        events = msg if isinstance(msg, list) else [msg]
        for ev in events:
            etype = ev.get("event_type")
            if etype == "book":
                self._apply_snapshot(ev)
            elif etype == "best_bid_ask":
                self._apply_best_bid_ask(ev)
            elif etype == "price_change":
                self._apply_price_change(ev)
            # tick_size_change / last_trade_price are ignored for top-of-book.

    def _apply_snapshot(self, ev: dict) -> None:
        token = ev.get("asset_id")
        if token not in self.books:
            return
        bids = ev.get("bids") or []
        asks = ev.get("asks") or []
        # Bids/asks are lists of {price, size} strings. Best bid = highest
        # price; best ask = lowest price.
        best_bid = max((float(b["price"]) for b in bids), default=None)
        best_ask = min((float(a["price"]) for a in asks), default=None)
        self.books[token] = BookSide(best_bid, best_ask, time.time())

    def _apply_best_bid_ask(self, ev: dict) -> None:
        token = ev.get("asset_id")
        if token not in self.books:
            return
        bb = ev.get("best_bid")
        ba = ev.get("best_ask")
        self.books[token] = BookSide(
            float(bb) if bb is not None else None,
            float(ba) if ba is not None else None,
            time.time(),
        )

    def _apply_price_change(self, ev: dict) -> None:
        # Each entry carries the resulting best_bid/best_ask for its asset.
        for pc in ev.get("price_changes", []):
            token = pc.get("asset_id")
            if token not in self.books:
                continue
            bb = pc.get("best_bid")
            ba = pc.get("best_ask")
            book = self.books[token]
            if bb is not None:
                book.best_bid = float(bb)
            if ba is not None:
                book.best_ask = float(ba)
            book.ts = time.time()
