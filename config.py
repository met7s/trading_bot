"""
config.py
=========
Single source of truth for:
  * secrets pulled from the environment (.env),
  * the hard-coded risk limits, and
  * the strategy parameters.

Everything else imports from here. Nothing in this file performs I/O beyond
reading environment variables, so it is safe to import anywhere.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

# Load the .env file once, at import time. The Polymarket SDK does NOT read
# .env itself, so we must do it before constructing any client.
load_dotenv()


def _get(name: str, default: str | None = None, required: bool = False) -> str:
    """Small helper: fetch an env var, optionally enforcing presence."""
    val = os.getenv(name, default)
    if required and (val is None or val == ""):
        raise RuntimeError(f"Missing required environment variable: {name}")
    return val  # type: ignore[return-value]


# ============================================================================
# 1. EXECUTION MODE
# ============================================================================
# When True, NO real orders are sent. The execution path is bypassed and the
# intended trade is written to the `paper_trades` SQLite table instead.
# Flip to False ONLY after you have watched paper mode behave for a long time.
PAPER_TRADE: bool = True


# ============================================================================
# 2. SECRETS  (only required when PAPER_TRADE is False)
# ============================================================================
# We DON'T mark these required at import time, because paper mode must run with
# an empty .env. exchange.py validates them lazily when live trading starts.
PRIVATE_KEY: str = _get("POLYMARKET_PRIVATE_KEY", "")
FUNDER_ADDRESS: str = _get("POLYMARKET_FUNDER", "")
SIGNATURE_TYPE: int = int(_get("POLYMARKET_SIGNATURE_TYPE", "1"))

CLOB_API_KEY: str = _get("CLOB_API_KEY", "")
CLOB_API_SECRET: str = _get("CLOB_API_SECRET", "")
CLOB_API_PASSPHRASE: str = _get("CLOB_API_PASSPHRASE", "")

BUILDER_API_KEY: str = _get("POLYMARKET_BUILDER_API_KEY", "")


# ============================================================================
# 3. ENDPOINTS
# ============================================================================
CLOB_HOST: str = _get("CLOB_HOST", "https://clob.polymarket.com")
GAMMA_HOST: str = _get("GAMMA_HOST", "https://gamma-api.polymarket.com")
CLOB_WS_URL: str = _get(
    "CLOB_WS_URL", "wss://ws-subscriptions-clob.polymarket.com/ws/market"
)
BINANCE_WS_URL: str = _get(
    "BINANCE_WS_URL", "wss://stream.binance.com:9443/ws/btcusdt@trade"
)
BINANCE_REST_URL: str = _get(
    "BINANCE_REST_URL", "https://api.binance.com/api/v3/ticker/price"
)
CHAIN_ID: int = int(_get("CHAIN_ID", "137"))  # Polygon mainnet


# ============================================================================
# 4. RISK LIMITS  (the rules that keep $100 alive)
# ============================================================================
@dataclass(frozen=True)
class RiskLimits:
    # Notional committed per trade, in USDC. Hard-coded. No dynamic sizing.
    POSITION_SIZE_USD: float = 2.00

    # Abort a trade if (best_ask - best_bid) exceeds this, in probability units
    # (Polymarket prices are 0.00-1.00, so 0.03 == 3 cents).
    MAX_SPREAD: float = 0.03

    # Daily kill-switch. If realized + unrealized PnL over the trailing 24h
    # reaches this (a NEGATIVE number), the bot cancels everything and exits.
    DAILY_STOP_LOSS: float = -15.00

    # Max slippage tolerated on a limit order, as a fraction (0.02 == 2%).
    # We never use market orders; the limit price is capped by this.
    MAX_SLIPPAGE: float = 0.02

    # Polymarket price tick. Most markets quote in whole cents (0.01). Inside
    # the [0.04, 0.96] band this is correct; outside it the venue may switch to
    # 0.001 — strategy.py rounds to this and notes the caveat.
    PRICE_TICK: float = 0.01

    # Trailing window over which PnL is summed for the stop-loss, in seconds.
    PNL_WINDOW_SEC: int = 24 * 60 * 60


RISK = RiskLimits()


# ============================================================================
# 5. STRATEGY PARAMETERS
# ============================================================================
@dataclass(frozen=True)
class StrategyParams:
    # --- Momentum on Binance spot ------------------------------------------
    # Rate-of-change is measured over this trailing window (3 minutes).
    MOMENTUM_WINDOW_SEC: int = 180

    # A move of at least this fraction over the window is needed to act.
    # 0.002 == 0.2%.
    MOMENTUM_THRESHOLD: float = 0.002

    # --- Mapping momentum -> a "fair" Polymarket probability ---------------
    # This is the heart of the edge AND its biggest assumption. We translate a
    # spot move into an implied probability that the 15-min market resolves in
    # that direction. `fair = 0.50 + momentum_pct * SENSITIVITY`, clamped.
    #
    # SENSITIVITY is a PLACEHOLDER. It MUST be calibrated from historical data
    # (regress realized 15-min outcomes on 3-min momentum). Shipping the wrong
    # number here is the difference between an edge and a money incinerator.
    SENSITIVITY: float = 25.0

    # We only fire if the Polymarket midpoint for the favoured side is "lagging"
    # the fair price by more than this, in probability units (0.04 == 4 cents).
    LAG_THRESHOLD: float = 0.04

    # Clamp fair probabilities into a sane band so a huge spike can't imply 0/1.
    FAIR_MIN: float = 0.05
    FAIR_MAX: float = 0.95

    # --- Market discovery ---------------------------------------------------
    # A market is a candidate if it resolves within this many seconds from now.
    RESOLVE_WITHIN_SEC: int = 15 * 60

    # ...but not if it resolves sooner than this (don't enter a market that is
    # about to lock — you won't get filled and can't exit).
    MIN_TIME_TO_RESOLVE_SEC: int = 90

    # Case-insensitive substrings used to recognise the BTC up/down series in
    # the Gamma `question`/`slug` fields. The live naming on polymarket.com
    # drifts over time, so KEEP THIS LIST IN SYNC with what you actually see.
    MARKET_NAME_PATTERNS: tuple[str, ...] = (
        "btc",
        "bitcoin",
    )
    MARKET_UPDOWN_PATTERNS: tuple[str, ...] = (
        "up or down",
        "up/down",
        "updown",
        "15-min",
        "15 min",
        "15m",
    )

    # How often the scanner re-polls Gamma for the current target market.
    SCAN_INTERVAL_SEC: int = 20

    # How often the main loop evaluates the strategy.
    EVAL_INTERVAL_SEC: float = 1.0


STRAT = StrategyParams()


# ============================================================================
# 6. STORAGE
# ============================================================================
# SQLite database path (created on first run).
DB_PATH: str = os.path.join(os.path.dirname(__file__), "data", "trades.db")
