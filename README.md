# BTC 15-Minute Up/Down — Polymarket Trading Bot

A fully automated, async Python bot that scans Polymarket for short-dated
"Bitcoin Up/Down" markets, watches live Binance momentum, and places small,
slippage-capped limit orders when the Polymarket midpoint lags a momentum
signal — all behind a hard daily stop-loss. Ships in **paper mode by default**.

> **Read the "Reality check" section before you risk a cent.** The plumbing is
> sound; the *edge* is an assumption you must validate.

---

## Directory structure

```
btc_polymarket_bot/
├── .env.example          # copy to .env and fill in secrets (never commit .env)
├── requirements.txt      # dependencies (note the SDK decision below)
├── README.md             # this file
├── config.py             # env vars + ALL risk limits + strategy params
├── database.py           # sqlite3 logging (paper_trades, live_trades, fills, …)
├── binance_feed.py       # Binance trade websocket + 3-min momentum buffer
├── market_scanner.py     # Gamma API discovery of the BTC 15-min market
├── exchange.py           # py-clob-client adapter + CLOB order-book websocket
├── strategy.py           # momentum math, fair-value mapping, signal generation
├── risk.py               # position sizing, slippage cap, spread gate, stop-loss
├── main.py               # async event loop tying it all together
└── data/
    └── trades.db         # created on first run
```

---

## Setup

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then edit .env
```

Run (paper mode is the default in `config.py`):

```bash
python main.py
```

To go live, set `PAPER_TRADE = False` in `config.py` **after** you have funded
the Polymarket account with USDC on Polygon and watched paper mode behave.

---

## Which SDK this uses, and why

You asked for the **unified `py-sdk`**. That package exists — it installs as
`polymarket-client` (imported as `polymarket`) — but it is currently published
with a **beta** badge, its public API is described as unstable, and its README
only documents *public market reads* (`get_market`). It does **not** yet
publicly document order placement, cancellation, or order-book streaming.

For a bot that signs orders and holds money, that's disqualifying for now. So
this codebase is built on **`py-clob-client`**, the production CLOB SDK that
Polymarket's own docs use for orders, **isolated behind an adapter in
`exchange.py`**. When `polymarket-client` stabilises its trading surface, you
reimplement the bodies of `PolymarketExchange.*` against it without touching
`strategy.py`, `risk.py`, or `main.py`.

---

## How the pieces map to your spec

| Requirement | Where |
|---|---|
| Poll for active BTC 15-min markets | `market_scanner.py` (Gamma `/markets`, pattern + end-time filter) |
| Stream live order book (YES/NO) | `exchange.py::OrderBookStream` (CLOB `ws/market`, `book`/`best_bid_ask`/`price_change`) |
| 1s Binance price + 3-min momentum | `binance_feed.py` (`btcusdt@trade` stream, ROC over window) |
| Signal on >0.2% momentum & >4¢ lag | `strategy.py` |
| $2 fixed size | `risk.py::build_buy_order` (`shares = 2 / price`) |
| Spread > 3¢ aborts | `risk.py::spread_ok` + `strategy.py` gate |
| −$15 / 24h stop → cancel all → exit | `risk.py::check_stop_loss` + `main.py::_risk_loop` |
| Limit orders, ≤2% slippage, no market orders | `risk.py` (price cap) + `exchange.py` (`OrderType.GTC`) |
| `PAPER_TRADE` logs to `paper_trades` | `config.py` flag + `main.py::_paper_execute` |
| `.env` secrets | `config.py` via `python-dotenv` |

---

## Reality check (the part a backtest won't tell you)

These are not reasons to quit — they're the things that decide whether this
makes or loses money, so they're called out plainly.

1. **The edge is an assumption, not a given.** `SENSITIVITY` in `config.py`
   maps a 3-minute spot move to a 15-minute resolution probability. That
   mapping is the entire strategy and the placeholder value is a guess.
   Calibrate it: regress realized 15-min outcomes on 3-min momentum from
   historical data before trusting a single live dollar.

2. **These markets are the most efficiency-hunted on the venue.** Short-dated
   BTC up/down books are saturated with latency-optimised bots. A retail bot
   reading REST/WS from afar will often only get filled when it's *wrong*
   (adverse selection). Paper mode assumes optimistic fills at your limit — so
   paper PnL is an **upper bound**, not a forecast.

3. **No sandbox exists.** Polymarket has no testnet/paper environment of its
   own. Paper mode here is simulated locally; the only true test is small live
   size.

4. **Latency from Nepal.** Binance global + Polygon round-trips mean a
   "1-second" signal is already stale by the time an order rests. Treat
   sub-second precision as aspirational.

5. **Funder vs signer is the #1 setup mistake.** If you log in to Polymarket
   with email/Magic or a browser wallet, `POLYMARKET_PRIVATE_KEY` is the
   *signer* and `POLYMARKET_FUNDER` is your *proxy/deposit* address — they are
   different. Set `POLYMARKET_SIGNATURE_TYPE` accordingly (0 EOA, 1 email, 2
   browser). EOA users must also set USDC/CTF allowances once.

6. **Geo-blocks.** `api.binance.com` / `stream.binance.com` are global
   endpoints, blocked in a few regions. If you see HTTP 451, point the URLs in
   `.env` at a permitted mirror.

7. **The stop-loss is approximate.** True 24h PnL needs resolution data.
   `record_resolution()` must be fed by a resolution poller (a stub is the
   natural next addition); until a market resolves, open fills are marked to the
   live midpoint. Don't treat the −$15 line as exact to the cent.

8. **Order-confirmation is simplified.** Both paper and live paths record a fill
   optimistically. A production build confirms real fills via the CLOB **user**
   websocket channel before booking cost basis.

*This is engineering scaffolding and information, not financial advice. You are
responsible for your own capital and for complying with Polymarket's terms and
your local regulations.*
