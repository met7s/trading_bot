"""
scripts/train_model.py
======================
OFFLINE, research-only. Trains a logistic-regression probability model for the
BTC 15-min up/down market on the dataset produced by build_dataset.py, and
reports HONEST out-of-sample metrics. It then exports plain coefficients to JSON
so the live bot can score with nothing more than a dot-product + sigmoid (no
scikit-learn at runtime).

    python scripts/train_model.py [DATASET_CSV]

scikit-learn + numpy are used here only (offline). They are NOT added to the
live runtime path. Default dataset: data/dataset.csv ; models -> models/*.json

METHODOLOGY (why you can trust the numbers)
-------------------------------------------
* Split is by WINDOW and by TIME. All k-rows of one 15-min window stay on the
  same side of the split, so correlated rows never leak train->test. The
  holdout is the most-recent 30% of windows the model never saw.
* A 5-fold expanding WALK-FORWARD is also reported: train on past, test on the
  next time block. This is the realistic "could it have predicted the future"
  test, not a shuffled CV that leaks the future into the past.
* Baselines printed alongside: always-0.5, and sign(ret_since_open). A model
  that can't beat these has no business near real money.
* HONEST LIMITATION: tradable edge needs Polymarket to MISPRICE relative to the
  model. We cannot measure that from Binance data alone. These metrics show
  whether the model has genuine *predictive content*; they do NOT prove profit.
"""

from __future__ import annotations

import csv
import json
import math
import os
import sys

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score,
    brier_score_loss,
    log_loss,
    roc_auc_score,
)

FULL_FEATURES = [
    "ret_since_open",
    "mom_1m",
    "mom_3m",
    "mom_5m",
    "vol_5m",
    "minutes_remaining",
]
COLD_FEATURES = ["mom_1m", "mom_3m", "mom_5m", "vol_5m", "minutes_remaining"]

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MODELS_DIR = os.path.join(ROOT, "models")


def load(path: str):
    rows = []
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            rows.append(r)
    if not rows:
        raise SystemExit(f"empty dataset: {path}")
    window_id = np.array([int(r["window_id"]) for r in rows])
    y = np.array([int(r["label"]) for r in rows])
    cols = {f: np.array([float(r[f]) for r in rows]) for f in FULL_FEATURES}
    return window_id, y, cols


def chrono_window_split(window_id: np.ndarray, frac_train: float = 0.70):
    """Boolean train/test masks, split by time at the WINDOW level."""
    uniq = np.sort(np.unique(window_id))
    cut = uniq[int(len(uniq) * frac_train)]
    train = window_id < cut
    test = window_id >= cut
    return train, test


def standardize(train_X, *others):
    mean = train_X.mean(axis=0)
    scale = train_X.std(axis=0)
    scale[scale == 0] = 1.0
    out = [(train_X - mean) / scale]
    for o in others:
        out.append((o - mean) / scale)
    return mean, scale, out


def fit_eval(features, window_id, y, cols, label: str):
    X = np.column_stack([cols[f] for f in features])
    train, test = chrono_window_split(window_id)
    Xtr, Xte = X[train], X[test]
    ytr, yte = y[train], y[test]
    mean, scale, (Ztr, Zte) = standardize(Xtr, Xte)

    clf = LogisticRegression(C=1.0, max_iter=2000)
    clf.fit(Ztr, ytr)
    p = clf.predict_proba(Zte)[:, 1]

    base = np.full_like(p, yte.mean())  # always base-rate
    metrics = {
        "n_train": int(train.sum()),
        "n_test": int(test.sum()),
        "base_rate_test": float(yte.mean()),
        "log_loss": float(log_loss(yte, p)),
        "log_loss_baseline": float(log_loss(yte, base, labels=[0, 1])),
        "brier": float(brier_score_loss(yte, p)),
        "brier_baseline": float(brier_score_loss(yte, base)),
        "auc": float(roc_auc_score(yte, p)),
        "accuracy": float(accuracy_score(yte, (p >= 0.5).astype(int))),
    }
    metrics["brier_skill_score"] = 1.0 - metrics["brier"] / max(
        metrics["brier_baseline"], 1e-12
    )

    print(f"\n=== {label}  (features: {', '.join(features)}) ===")
    print(f"  train rows / test rows : {metrics['n_train']:,} / {metrics['n_test']:,}")
    print(f"  test base rate (UP)    : {metrics['base_rate_test']:.4f}")
    print(f"  AUC                    : {metrics['auc']:.4f}   (0.5 = no skill)")
    print(
        f"  log loss   model/base  : {metrics['log_loss']:.4f} / "
        f"{metrics['log_loss_baseline']:.4f}   (lower better)"
    )
    print(
        f"  Brier      model/base  : {metrics['brier']:.4f} / "
        f"{metrics['brier_baseline']:.4f}"
    )
    print(
        f"  Brier skill score      : {metrics['brier_skill_score']:+.4f}   "
        f"(>0 beats base rate, <0 worse)"
    )
    print(f"  accuracy @0.5          : {metrics['accuracy']:.4f}")
    _reliability(yte, p)
    return clf, mean, scale, metrics


def _reliability(y_true, p, bins=5):
    print("  reliability (predicted -> actual UP rate):")
    edges = np.linspace(0.0, 1.0, bins + 1)
    for i in range(bins):
        lo, hi = edges[i], edges[i + 1]
        m = (p >= lo) & (p < hi if i < bins - 1 else p <= hi)
        if m.sum() == 0:
            continue
        print(
            f"    [{lo:.2f},{hi:.2f})  n={int(m.sum()):5d}  "
            f"pred={p[m].mean():.3f}  actual={y_true[m].mean():.3f}"
        )


def walk_forward(features, window_id, y, cols, folds=5):
    """Expanding-window walk-forward at the window level. Mean OOS AUC/logloss."""
    X = np.column_stack([cols[f] for f in features])
    uniq = np.sort(np.unique(window_id))
    blocks = np.array_split(uniq, folds + 1)
    aucs, lls = [], []
    for i in range(1, folds + 1):
        train_w = np.concatenate(blocks[:i])
        test_w = blocks[i]
        tr = np.isin(window_id, train_w)
        te = np.isin(window_id, test_w)
        if te.sum() == 0 or len(np.unique(y[tr])) < 2:
            continue
        mean, scale, (Ztr, Zte) = standardize(X[tr], X[te])
        clf = LogisticRegression(C=1.0, max_iter=2000).fit(Ztr, y[tr])
        p = clf.predict_proba(Zte)[:, 1]
        aucs.append(roc_auc_score(y[te], p))
        lls.append(log_loss(y[te], p, labels=[0, 1]))
    if aucs:
        print(
            f"\n  walk-forward ({len(aucs)} folds): "
            f"AUC {np.mean(aucs):.4f} +/- {np.std(aucs):.4f} | "
            f"log loss {np.mean(lls):.4f}"
        )


def sign_baseline(window_id, y, cols):
    """Accuracy of the naive 'bet the direction we're already leaning' rule."""
    _, test = chrono_window_split(window_id)
    pred = (cols["ret_since_open"][test] > 0).astype(int)
    acc = accuracy_score(y[test], pred)
    print(f"\n[baseline] sign(ret_since_open) accuracy on holdout: {acc:.4f}")


def export(path, features, clf, mean, scale, metrics):
    os.makedirs(MODELS_DIR, exist_ok=True)
    payload = {
        "model": "logistic_regression",
        "feature_order": features,
        "mean": mean.tolist(),
        "scale": scale.tolist(),
        "coef": clf.coef_[0].tolist(),
        "intercept": float(clf.intercept_[0]),
        "inference": "p = sigmoid(coef . ((x-mean)/scale) + intercept)",
        "fair_clamp": [0.05, 0.95],
        "holdout_metrics": metrics,
        "note": "OFFLINE-trained. Predictive content only; not a profit guarantee.",
    }
    with open(path, "w") as fh:
        json.dump(payload, fh, indent=2)
    print(f"\n[export] wrote {path}")


def main(dataset: str):
    window_id, y, cols = load(dataset)
    print(f"[train] rows={len(y):,}  windows={len(np.unique(window_id)):,}  "
          f"overall UP rate={y.mean():.4f}")

    sign_baseline(window_id, y, cols)

    clf_full, mean_f, scale_f, m_full = fit_eval(
        FULL_FEATURES, window_id, y, cols, "FULL model"
    )
    walk_forward(FULL_FEATURES, window_id, y, cols)

    clf_cold, mean_c, scale_c, m_cold = fit_eval(
        COLD_FEATURES, window_id, y, cols, "COLD-START model (no ret_since_open)"
    )
    walk_forward(COLD_FEATURES, window_id, y, cols)

    export(os.path.join(MODELS_DIR, "momentum_lr.json"),
           FULL_FEATURES, clf_full, mean_f, scale_f, m_full)
    export(os.path.join(MODELS_DIR, "momentum_lr_cold.json"),
           COLD_FEATURES, clf_cold, mean_c, scale_c, m_cold)

    print("\n" + "=" * 68)
    print("HONEST READ: AUC near 0.50 and Brier skill near 0 == momentum has no")
    print("real edge at this horizon (expected for efficient markets). Only wire")
    print("this into the live bot if the holdout AUC is meaningfully > 0.55 AND")
    print("the walk-forward agrees. Even then, edge requires Polymarket to")
    print("misprice vs the model, which this study cannot measure.")
    print("=" * 68)


if __name__ == "__main__":
    ds = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "data", "dataset.csv")
    main(ds)
