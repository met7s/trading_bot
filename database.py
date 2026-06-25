"""
database.py
===========
Thin synchronous SQLite layer. SQLite calls are fast and local, but they DO
block, so in main.py every call is dispatched through asyncio.to_thread to keep
the event loop responsive.

Tables
------
paper_trades : intended trades logged when PAPER_TRADE is True (no execution).
live_trades  : orders actually submitted to the CLOB.
fills        : confirmed fills (used to build cost basis for PnL).
signals      : every strategy signal, fired or not, for later analysis.
resolutions  : recorded 0/1 outcomes of markets, for realized PnL.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any


class TradeDB:
    def __init__(self, path: str) -> None:
        # check_same_thread=False because to_thread may touch it from a worker
        # thread. We serialise all access ourselves (single bot, low rate).
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    # ------------------------------------------------------------------ setup
    def _init_schema(self) -> None:
        cur = self._conn.cursor()
        cur.executescript(
            """
            CREATE TABLE IF NOT EXISTS paper_trades (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                ts            REAL    NOT NULL,   -- unix seconds
                market_slug   TEXT,
                token_id      TEXT    NOT NULL,
                side          TEXT    NOT NULL,   -- BUY / SELL
                size_shares   REAL    NOT NULL,
                price         REAL    NOT NULL,   -- limit price (0-1)
                notional_usd  REAL    NOT NULL,
                momentum_pct  REAL,
                edge          REAL,
                reason        TEXT
            );

            CREATE TABLE IF NOT EXISTS live_trades (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                ts            REAL    NOT NULL,
                market_slug   TEXT,
                token_id      TEXT    NOT NULL,
                side          TEXT    NOT NULL,
                size_shares   REAL    NOT NULL,
                price         REAL    NOT NULL,
                notional_usd  REAL    NOT NULL,
                order_id      TEXT,
                status        TEXT,
                raw_response  TEXT
            );

            CREATE TABLE IF NOT EXISTS fills (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                ts            REAL    NOT NULL,
                token_id      TEXT    NOT NULL,
                side          TEXT    NOT NULL,
                size_shares   REAL    NOT NULL,
                price         REAL    NOT NULL,   -- avg fill price (0-1)
                market_slug   TEXT
            );

            CREATE TABLE IF NOT EXISTS signals (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                ts            REAL    NOT NULL,
                market_slug   TEXT,
                token_id      TEXT,
                direction     TEXT,
                momentum_pct  REAL,
                fair_price    REAL,
                pm_midpoint   REAL,
                edge          REAL,
                fired         INTEGER,            -- 1 if it led to a trade
                abort_reason  TEXT
            );

            CREATE TABLE IF NOT EXISTS resolutions (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                ts            REAL    NOT NULL,
                token_id      TEXT    NOT NULL,
                payout        REAL    NOT NULL    -- 1.0 if this token won else 0.0
            );
            """
        )
        self._conn.commit()

    # ------------------------------------------------------------- write paths
    def log_paper_trade(self, **kw: Any) -> None:
        self._conn.execute(
            """INSERT INTO paper_trades
               (ts, market_slug, token_id, side, size_shares, price,
                notional_usd, momentum_pct, edge, reason)
               VALUES (:ts,:market_slug,:token_id,:side,:size_shares,:price,
                       :notional_usd,:momentum_pct,:edge,:reason)""",
            kw,
        )
        self._conn.commit()

    def log_live_trade(self, **kw: Any) -> None:
        self._conn.execute(
            """INSERT INTO live_trades
               (ts, market_slug, token_id, side, size_shares, price,
                notional_usd, order_id, status, raw_response)
               VALUES (:ts,:market_slug,:token_id,:side,:size_shares,:price,
                       :notional_usd,:order_id,:status,:raw_response)""",
            kw,
        )
        self._conn.commit()

    def record_fill(self, **kw: Any) -> None:
        self._conn.execute(
            """INSERT INTO fills
               (ts, token_id, side, size_shares, price, market_slug)
               VALUES (:ts,:token_id,:side,:size_shares,:price,:market_slug)""",
            kw,
        )
        self._conn.commit()

    def log_signal(self, **kw: Any) -> None:
        self._conn.execute(
            """INSERT INTO signals
               (ts, market_slug, token_id, direction, momentum_pct,
                fair_price, pm_midpoint, edge, fired, abort_reason)
               VALUES (:ts,:market_slug,:token_id,:direction,:momentum_pct,
                       :fair_price,:pm_midpoint,:edge,:fired,:abort_reason)""",
            kw,
        )
        self._conn.commit()

    def record_resolution(self, token_id: str, payout: float) -> None:
        self._conn.execute(
            "INSERT INTO resolutions (ts, token_id, payout) VALUES (?,?,?)",
            (time.time(), token_id, payout),
        )
        self._conn.commit()

    # -------------------------------------------------------------- read paths
    def fills_since(self, since_ts: float) -> list[sqlite3.Row]:
        """All fills (paper or live) in the trailing window, for PnL."""
        cur = self._conn.execute(
            "SELECT * FROM fills WHERE ts >= ? ORDER BY ts ASC", (since_ts,)
        )
        return cur.fetchall()

    def resolution_for(self, token_id: str) -> float | None:
        cur = self._conn.execute(
            "SELECT payout FROM resolutions WHERE token_id = ? "
            "ORDER BY ts DESC LIMIT 1",
            (token_id,),
        )
        row = cur.fetchone()
        return None if row is None else float(row["payout"])

    def close(self) -> None:
        self._conn.close()
