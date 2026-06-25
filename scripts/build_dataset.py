"""
scripts/build_dataset.py
========================
OFFLINE, research-only. Builds a labelled training dataset for the BTC 15-min
up/down market from Binance public 1-minute klines. It touches NO secrets, NO
Polymarket order path, and does not import the live trading modules. Run it by
hand:

    python scripts/build_dataset.py [DAYS] [OUT_CSV]

    DAYS     how many trailing days of 1-min candles to pull   (default 90)
    OUT_CSV  where to write the dataset (default data/dataset.csv)

WHAT IT MODELS
--------------
The Polymarket "BTC 15-Min Up/Down" market resolves UP iff the BTC price at the
END of a fixed clock-aligned 15-minute window (:00/:15/:30/:45) is strictly
above the price at the START of that window. So:

    label = 1 if close(window_end) > open(window_start) else 0

Inside each window the bot may decide to trade at any time. We therefore emit
ONE training row per (window, decision-minute k), for k = 2..13 minutes elapsed
(which is exactly the live tradeable band: >= ~2 min of history for momentum,
and >= 2 min before resolution so we are not inside MIN_TIME_TO_RESOLVE_SEC=90).

TRAIN/SERVE PARITY (this is the whole point)
--------------------------------------------
Every feature below is computable by the LIVE bot at decision time from data it
already has (or can cheaply track). No feature peeks past the decision moment;
only the label looks at the window's end.

  ret_since_open   close_now/open(window_start) - 1   * needs live window-open px
  mom_1m           close_now/close_(now-1m) - 1
  mom_3m           close_now/close_(now-3m) - 1        (matches MOMENTUM_WINDOW_SEC)
  mom_5m           close_now/close_(now-5m) - 1
  vol_5m           stdev of last 5 one-min log returns
  minutes_remaining  15 - k                            (live: (end_ts-now)/60)

The momentum/vol lookbacks deliberately use the CONTINUOUS price series and may
cross a window boundary — that mirrors the live deque, which does not reset at
:00/:15/:30/:45. `window_id` is written so the trainer can split by window and
never leak correlated rows of one window across train/test.

NOTE ON COLD START: ret_since_open is the strongest feature but requires the
live bot to know the window's open price. The trainer reports an ablation with
and without it so we can decide whether the live plumbing is worth it.
"""

from __future__ import annotations

import csv
import json
import os
import statistics
import sys
import time
import urllib.parse
import urllib.request

KLINES_URL = "https://api.binance.com/api/v3/klines"
SYMBOL = "BTCUSDT"
MINUTE_MS = 60_000
WINDOW_MIN = 15
WINDOW_MS = WINDOW_MIN * MINUTE_MS

# Decision minutes within a window we emit rows for (inclusive). k minutes have
# elapsed; minutes_remaining = 15 - k. k=2..13 == the live tradeable band.
K_MIN, K_MAX = 2, 13

FEATURE_ORDER = [
    "ret_since_open",
    "mom_1m",
    "mom_3m",
    "mom_5m",
    "vol_5m",
    "minutes_remaining",
]


def _fetch_klines(start_ms: int, end_ms: int) -> list[list]:
    """Page forward through Binance 1-min klines in [start_ms, end_ms)."""
    out: list[list] = []
    cursor = start_ms
    while cursor < end_ms:
        params = urllib.parse.urlencode(
            {
                "symbol": SYMBOL,
                "interval": "1m",
                "startTime": cursor,
                "endTime": end_ms,
                "limit": 1000,
            }
        )
        req = urllib.request.Request(f"{KLINES_URL}?{params}")
        with urllib.request.urlopen(req, timeout=20) as resp:
            batch = json.loads(resp.read().decode())
        if not batch:
            break
        out.extend(batch)
        last_open = batch[-1][0]
        nxt = last_open + MINUTE_MS
        if nxt <= cursor:  # safety: no progress
            break
        cursor = nxt
        if len(batch) < 1000:
            break
        time.sleep(0.15)  # be polite to the public endpoint
    return out


def build(days: int, out_csv: str) -> None:
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - days * 24 * 60 * MINUTE_MS
    print(f"[dataset] fetching ~{days}d of 1m klines for {SYMBOL} ...")
    klines = _fetch_klines(start_ms, now_ms)
    print(f"[dataset] got {len(klines):,} candles")
    if len(klines) < WINDOW_MIN * 4:
        raise SystemExit("not enough candles fetched; aborting")

    # Index candles by open_time so we can address neighbours / window members
    # robustly even if Binance has the rare gap.
    open_px: dict[int, float] = {}
    close_px: dict[int, float] = {}
    for k in klines:
        ot = int(k[0])
        open_px[ot] = float(k[1])
        close_px[ot] = float(k[4])

    def close_at(ot: int) -> float | None:
        return close_px.get(ot)

    # Pre-compute per-window open price, last-candle close, and completeness.
    windows: dict[int, dict] = {}
    for ot in open_px:
        w0 = (ot // WINDOW_MS) * WINDOW_MS
        windows.setdefault(w0, {})
    complete: dict[int, tuple[float, float]] = {}  # w0 -> (open_price, close_price)
    for w0 in windows:
        members = [w0 + i * MINUTE_MS for i in range(WINDOW_MIN)]
        if not all(m in close_px for m in members):
            continue  # incomplete window (gap or edge of range) -> skip
        complete[w0] = (open_px[members[0]], close_px[members[-1]])

    rows: list[dict] = []
    for w0, (w_open, w_close) in complete.items():
        label = 1 if w_close > w_open else 0
        for k in range(K_MIN, K_MAX + 1):
            # Decision at k minutes elapsed: price_now is the close of the
            # candle that ENDS at w0 + k minutes, i.e. candle index k-1.
            dec_ot = w0 + (k - 1) * MINUTE_MS
            p_now = close_at(dec_ot)
            if p_now is None or p_now <= 0:
                continue
            c1 = close_at(dec_ot - 1 * MINUTE_MS)
            c3 = close_at(dec_ot - 3 * MINUTE_MS)
            c5 = close_at(dec_ot - 5 * MINUTE_MS)
            if not all(c and c > 0 for c in (c1, c3, c5)):
                continue
            # 5 one-minute log returns ending at dec_ot for realized vol.
            seq = [close_at(dec_ot - i * MINUTE_MS) for i in range(6)]
            if any(c is None or c <= 0 for c in seq):
                continue
            rets = []
            for a, b in zip(seq[1:], seq[:-1]):  # older->newer pairs
                rets.append((b / a) - 1.0)
            vol_5m = statistics.pstdev(rets)

            rows.append(
                {
                    "window_id": w0,
                    "k": k,
                    "minutes_remaining": WINDOW_MIN - k,
                    "ret_since_open": p_now / w_open - 1.0,
                    "mom_1m": p_now / c1 - 1.0,
                    "mom_3m": p_now / c3 - 1.0,
                    "mom_5m": p_now / c5 - 1.0,
                    "vol_5m": vol_5m,
                    "label": label,
                }
            )

    if not rows:
        raise SystemExit("no rows produced; aborting")

    os.makedirs(os.path.dirname(out_csv) or ".", exist_ok=True)
    fieldnames = ["window_id", "k", *FEATURE_ORDER, "label"]
    # minutes_remaining is already in FEATURE_ORDER; drop the dup "k" stays first.
    seen = set()
    ordered = []
    for f in fieldnames:
        if f not in seen:
            ordered.append(f)
            seen.add(f)
    with open(out_csv, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=ordered)
        w.writeheader()
        for r in rows:
            w.writerow({k: r[k] for k in ordered})

    n_up = sum(r["label"] for r in rows)
    n_win = len(complete)
    print(f"[dataset] windows complete: {n_win:,}")
    print(f"[dataset] rows written:     {len(rows):,}  -> {out_csv}")
    print(f"[dataset] base rate UP:     {n_up / len(rows):.4f}")


if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 90
    out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "dataset.csv"
    )
    build(days, out)
