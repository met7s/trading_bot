"""
research/calibrate.py
=====================
OFFLINE calibration of the momentum signal against historical BTC data.

This does NOT touch live trading and does NOT change PAPER_TRADE. It answers one
question with data instead of a guessed constant:

    "Does 3-minute BTC momentum predict the 15-minute up/down outcome better
     than a coin flip — and if so, by how much?"

It then fits the bot's fair-value mapping (config.SENSITIVITY) to the data.

WHAT IT CAN AND CANNOT TELL YOU
-------------------------------
CAN:  whether the directional SIGNAL has predictive power, and the calibrated
      probability curve P(window resolves in the momentum's direction | momentum).
CANNOT: whether the strategy is PROFITABLE on Polymarket. Profit depends on the
      *price you pay* (the ask) vs that probability — i.e. the lag/mispricing —
      which is not reconstructable from Polymarket's historical API for these
      resolved short-dated markets. A >50% hit rate here is NECESSARY but NOT
      SUFFICIENT: you still have to buy the side for less than its true value,
      net of adverse selection. That part is validated by Leg B (forward paper
      collection), not by this script.

DATA SOURCE
-----------
Binance public klines (no auth, free):
    GET https://api.binance.com/api/v3/klines
        ?symbol=BTCUSDT&interval=1m&startTime=<ms>&endTime=<ms>&limit=1000
1000 candles per call; we paginate. If you are in a Binance-restricted region
(HTTP 451), set --base to a permitted mirror.

USAGE
-----
    pip install numpy requests
    python research/calibrate.py --days 365
    python research/calibrate.py --days 90 --window 3 --threshold 0.002
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone

import numpy as np
import requests

KLINES_LIMIT = 1000          # Binance max candles per request
WINDOW_MINUTES = 15          # the market's resolution window


# ----------------------------------------------------------------- data fetch
def fetch_klines(base: str, symbol: str, start_ms: int, end_ms: int) -> np.ndarray:
    """
    Page through Binance 1-minute klines in [start_ms, end_ms].
    Returns an (N, 2) array of [open_time_ms, close_price], time-ascending.
    """
    out: list[tuple[int, float]] = []
    cursor = start_ms
    session = requests.Session()
    while cursor < end_ms:
        params = {
            "symbol": symbol,
            "interval": "1m",
            "startTime": cursor,
            "endTime": end_ms,
            "limit": KLINES_LIMIT,
        }
        r = session.get(f"{base}/api/v3/klines", params=params, timeout=20)
        if r.status_code == 451:
            raise SystemExit(
                "Binance returned 451 (geo-blocked). Re-run with --base set to "
                "a permitted mirror, e.g. https://data-api.binance.vision"
            )
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        for k in batch:
            out.append((int(k[0]), float(k[4])))  # openTime, close
        last_open = batch[-1][0]
        nxt = last_open + 60_000  # advance one minute past the last candle
        if nxt <= cursor:
            break
        cursor = nxt
        time.sleep(0.25)  # be gentle with the weight limit
        print(f"  fetched {len(out):>7} candles "
              f"(up to {datetime.fromtimestamp(last_open/1000, timezone.utc):%Y-%m-%d %H:%M})",
              end="\r")
    print()
    arr = np.array(out, dtype=np.float64)
    # De-duplicate / sort by time just in case of overlap at page edges.
    _, uniq = np.unique(arr[:, 0], return_index=True)
    return arr[np.sort(uniq)]


# ----------------------------------------------------- feature / label builder
def build_samples(
    times_ms: np.ndarray,
    closes: np.ndarray,
    window: int,
    momentum_window: int,
):
    """
    Replicate the 15-minute up/down market and pair each in-window minute with:
        signed_momentum  = close[t]/close[t-momentum_window] - 1
        resolved_up      = 1 if window_close > window_open else 0
        time_remaining   = minutes from t to window close

    Windows are aligned to clock quarter-hours (:00/:15/:30/:45), matching how
    Polymarket schedules these markets. Returns three 1-D arrays.

    Assumption stated plainly: the market resolves UP when the window's last
    1-minute close exceeds its first 1-minute close. If Polymarket uses a
    different reference (e.g. an index open print), the calibration shifts
    slightly but the shape of the result holds.
    """
    n = len(closes)
    # True clock alignment: minutes-since-epoch divisible by `window` start a window.
    starts = np.where(((times_ms // 60_000) % window) == 0)[0]

    sm: list[float] = []
    up: list[int] = []
    tr: list[int] = []

    for s in starts:
        e = s + window - 1  # last minute index of this window
        if e >= n:
            break
        # Require contiguous minutes (no gaps) across the window.
        if times_ms[e] - times_ms[s] != (window - 1) * 60_000:
            continue
        window_open = closes[s]
        window_close = closes[e]
        resolved_up = 1 if window_close > window_open else 0

        # Each candidate entry minute t inside the window.
        for t in range(s + momentum_window, e):  # leave >=1 min to resolve
            prev = t - momentum_window
            if closes[prev] <= 0:
                continue
            mom = closes[t] / closes[prev] - 1.0
            sm.append(mom)
            up.append(resolved_up)
            tr.append(e - t)

    return np.array(sm), np.array(up, dtype=np.float64), np.array(tr, dtype=np.float64)


# ---------------------------------------------------------- logistic (numpy)
def fit_logistic(X: np.ndarray, y: np.ndarray, l2: float = 1e-4, iters: int = 50):
    """Plain IRLS logistic regression. X includes an intercept column."""
    n, k = X.shape
    w = np.zeros(k)
    reg = l2 * np.eye(k)
    reg[0, 0] = 0.0  # don't penalise the intercept
    for _ in range(iters):
        z = X @ w
        p = 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))
        W = p * (1.0 - p) + 1e-9
        grad = X.T @ (p - y) + reg @ w
        H = X.T @ (X * W[:, None]) + reg
        try:
            w -= np.linalg.solve(H, grad)
        except np.linalg.LinAlgError:
            break
    return w


def sigmoid(z: float) -> float:
    return 1.0 / (1.0 + np.exp(-z))


# ------------------------------------------------------------------- analysis
def analyse(sm, up, tr, threshold: float):
    report: dict = {}
    report["samples_total"] = int(len(sm))
    report["base_rate_up"] = float(up.mean())

    # --- Fired trades: |momentum| > threshold, bet its direction -----------
    fired = np.abs(sm) > threshold
    n_fired = int(fired.sum())
    report["signals_fired"] = n_fired
    if n_fired > 0:
        bet_up = sm[fired] > 0
        success = (bet_up == (up[fired] > 0.5))
        hit = float(success.mean())
        # Bootstrap 95% CI for the hit rate.
        rng = np.random.default_rng(0)
        boot = [
            success[rng.integers(0, n_fired, n_fired)].mean()
            for _ in range(2000)
        ]
        lo, hi = np.percentile(boot, [2.5, 97.5])
        report["hit_rate"] = hit
        report["hit_rate_ci95"] = [float(lo), float(hi)]
        report["edge_vs_coin"] = float(hit - 0.5)
    else:
        report["hit_rate"] = None

    # --- Logistic fit: P(resolved_up) ~ signed_momentum --------------------
    # Standardise momentum for conditioning, then convert back.
    mu, sd = sm.mean(), sm.std() + 1e-12
    Xz = np.column_stack([np.ones_like(sm), (sm - mu) / sd])
    w = fit_logistic(Xz, up)
    b0_z, b1_z = float(w[0]), float(w[1])
    # Convert standardised slope to raw-momentum slope: logit = a0 + a1*mom_raw
    a1 = b1_z / sd
    a0 = b0_z - b1_z * mu / sd
    report["logit_intercept"] = a0
    report["logit_slope_per_momentum"] = a1
    # Linear approximation the bot uses: fair = 0.5 + momentum * SENSITIVITY.
    # Near p=0.5 the logistic slope is a1 * 0.25, so:
    report["implied_SENSITIVITY"] = a1 * 0.25
    # Example fair prices the fitted model assigns at a few momentum levels.
    report["fair_curve"] = {
        f"{m:+.3%}": round(float(sigmoid(a0 + a1 * m)), 4)
        for m in (-0.005, -0.002, 0.0, 0.002, 0.005)
    }

    # --- Calibration table: empirical P(up) by momentum bucket -------------
    edges = np.array([-1, -0.005, -0.002, -0.0005, 0.0005, 0.002, 0.005, 1])
    table = []
    for i in range(len(edges) - 1):
        m = (sm >= edges[i]) & (sm < edges[i + 1])
        if m.sum() >= 50:
            table.append({
                "momentum_range": [float(edges[i]), float(edges[i + 1])],
                "n": int(m.sum()),
                "empirical_p_up": round(float(up[m].mean()), 4),
            })
    report["calibration_table"] = table
    return report


def verdict(report: dict, threshold: float) -> list[str]:
    lines = []
    hr = report.get("hit_rate")
    ci = report.get("hit_rate_ci95")
    if hr is None:
        lines.append("No trades fired at this threshold — lower --threshold or extend --days.")
        return lines
    lines.append(
        f"Hit rate when |momentum| > {threshold:.2%}: {hr:.3%} "
        f"(95% CI {ci[0]:.3%}–{ci[1]:.3%}) over {report['signals_fired']:,} signals."
    )
    if ci[0] <= 0.5:
        lines.append(
            "VERDICT: the confidence interval includes 50%. No statistically "
            "detectable directional edge. Do NOT trade this signal as-is."
        )
    else:
        lines.append(
            "VERDICT: hit rate is above 50% with the CI clear of it — there is a "
            "detectable directional signal. This is NECESSARY but NOT SUFFICIENT: "
            "on Polymarket you pay the ask, which already embeds this probability. "
            "Whether you profit depends on the midpoint LAG (Leg B forward data) "
            "and on costs/adverse selection, neither captured here."
        )
    lines.append(
        f"Fitted SENSITIVITY ≈ {report['implied_SENSITIVITY']:.2f} "
        f"(your config.py placeholder was 25.0). Prefer wiring the full logistic "
        f"(intercept {report['logit_intercept']:+.3f}, slope "
        f"{report['logit_slope_per_momentum']:.1f}) into strategy.py over the linear map."
    )
    return lines


# ------------------------------------------------------------------- runner
def main() -> None:
    ap = argparse.ArgumentParser(description="Calibrate the momentum signal on historical BTC data.")
    ap.add_argument("--days", type=int, default=365, help="lookback in days (default 365)")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--base", default="https://api.binance.com", help="Binance REST base URL")
    ap.add_argument("--window", type=int, default=WINDOW_MINUTES, help="market window in minutes")
    ap.add_argument("--momentum-window", type=int, default=3, help="momentum lookback in minutes")
    ap.add_argument("--threshold", type=float, default=0.002, help="momentum threshold to 'fire'")
    ap.add_argument("--out", default="research/calibration_result.json")
    args = ap.parse_args()

    end_ms = int(time.time() * 1000)
    start_ms = end_ms - args.days * 24 * 60 * 60 * 1000

    print(f"Fetching {args.days}d of {args.symbol} 1m candles from {args.base} ...")
    data = fetch_klines(args.base, args.symbol, start_ms, end_ms)
    if len(data) < args.window * 10:
        raise SystemExit("Not enough data fetched. Check connectivity / region.")
    times_ms, closes = data[:, 0], data[:, 1]
    print(f"Got {len(closes):,} candles.")

    print("Building samples (replicating the 15-min up/down windows) ...")
    sm, up, tr = build_samples(times_ms, closes, args.window, args.momentum_window)
    print(f"Built {len(sm):,} (momentum, outcome) samples.")

    report = analyse(sm, up, tr, args.threshold)
    report["params"] = vars(args)
    report["generated_at"] = datetime.now(timezone.utc).isoformat()

    print("\n================= CALIBRATION REPORT =================")
    print(f"Samples: {report['samples_total']:,} | base rate up: {report['base_rate_up']:.3%}")
    print("Calibration (empirical P(up) by momentum bucket):")
    for row in report["calibration_table"]:
        a, b = row["momentum_range"]
        print(f"  [{a:+.2%}, {b:+.2%})  n={row['n']:>6}  P(up)={row['empirical_p_up']:.3f}")
    print("Model fair price by momentum:")
    for k, v in report["fair_curve"].items():
        print(f"  momentum {k}  ->  fair P = {v:.3f}")
    print("-----------------------------------------------------")
    for line in verdict(report, args.threshold):
        print(line)
    print("=====================================================\n")

    with open(args.out, "w") as f:
        json.dump(report, f, indent=2)
    print(f"Full report written to {args.out}")


if __name__ == "__main__":
    main()
