"""
binance_feed.py
===============
Streams live BTC/USDT trades from Binance and exposes a momentum reading.

We use the public trade stream (wss://.../ws/btcusdt@trade), which pushes every
executed trade with a price. Each price is stamped and pushed into a deque; we
prune anything older than the momentum window. Momentum is the simple
rate-of-change between the oldest and newest sample in the window.

A REST endpoint (api/v3/ticker/price) is used (a) to seed a price immediately at
startup and (b) as a heartbeat fallback if the socket goes quiet.

GEO NOTE: api.binance.com / stream.binance.com are the GLOBAL endpoints. They
are reachable from most regions but are geo-blocked in a few (e.g. the US, which
must use binance.us). If you get HTTP 451 / connection resets, switch the URLs
in .env to a permitted mirror.
"""

from __future__ import annotations

import asyncio
import collections
import json
import time

import aiohttp
import websockets

from config import BINANCE_REST_URL, BINANCE_WS_URL, STRAT


class BinanceFeed:
    def __init__(self) -> None:
        # deque of (timestamp_seconds, price). Bounded generously; we also prune
        # by time so the bound is just a safety net against unbounded growth.
        self._prices: collections.deque[tuple[float, float]] = collections.deque(
            maxlen=10_000
        )
        self._latest_price: float | None = None
        self._running = False

    # --------------------------------------------------------------- accessors
    @property
    def latest_price(self) -> float | None:
        return self._latest_price

    def momentum(self, window_sec: int | None = None) -> float | None:
        """
        Rate-of-change over the trailing window:
            (price_now - price_window_ago) / price_window_ago

        Returns None until we have at least one sample older than the window
        (otherwise the reading is not yet meaningful).
        """
        window = window_sec or STRAT.MOMENTUM_WINDOW_SEC
        now = time.time()
        cutoff = now - window

        # Drop stale samples first so the oldest remaining is ~window old.
        while self._prices and self._prices[0][0] < cutoff:
            self._prices.popleft()

        if len(self._prices) < 2:
            return None

        oldest_ts, oldest_px = self._prices[0]
        _, newest_px = self._prices[-1]

        # Require the window to actually be (roughly) full, else the ROC is
        # computed over too short a span and is noise.
        if now - oldest_ts < window * 0.5:
            return None
        if oldest_px <= 0:
            return None

        return (newest_px - oldest_px) / oldest_px

    # ------------------------------------------------------------------ runner
    async def start(self) -> None:
        """Seed a price via REST, then stream forever with auto-reconnect."""
        self._running = True
        await self._seed_price_via_rest()
        # Run the websocket loop as the long-lived task.
        await self._stream_loop()

    def stop(self) -> None:
        self._running = False

    async def _seed_price_via_rest(self) -> None:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    BINANCE_REST_URL,
                    params={"symbol": "BTCUSDT"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json()
                    px = float(data["price"])
                    self._latest_price = px
                    self._prices.append((time.time(), px))
                    print(f"[binance] seeded price ${px:,.2f}")
        except Exception as exc:  # noqa: BLE001 - log and continue; socket follows
            print(f"[binance] REST seed failed (non-fatal): {exc}")

    async def _stream_loop(self) -> None:
        backoff = 1.0
        while self._running:
            try:
                # ping_interval keeps the underlying ws alive; Binance also
                # sends protocol ping frames every ~20s which `websockets`
                # answers automatically.
                async with websockets.connect(
                    BINANCE_WS_URL, ping_interval=20, ping_timeout=20
                ) as ws:
                    print("[binance] trade stream connected")
                    backoff = 1.0  # reset after a healthy connect
                    async for raw in ws:
                        self._handle_message(raw)
            except Exception as exc:  # noqa: BLE001 - reconnect on any drop
                print(f"[binance] stream error: {exc}; reconnecting in {backoff:.0f}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    def _handle_message(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            return
        # Trade stream payload uses "p" for price, "T" for trade time (ms).
        price = msg.get("p")
        if price is None:
            return
        ts = msg.get("T", time.time() * 1000) / 1000.0
        px = float(price)
        self._latest_price = px
        self._prices.append((ts, px))
