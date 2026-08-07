"""
Stage 3 — Train the LightGBM "price will rise in 24h" classifier.

Reads the canonical offers table, builds features (features.py), does a
time-aware train/test split, trains LightGBM, reports AUC / accuracy /
precision-recall, and saves the model + metrics. Also scores the latest
snapshot per trip and writes a `predictions` table for the API to serve.

Outputs:
  models/price_rise_lgbm.txt   trained booster
  models/metrics.json          evaluation metrics + feature importance
  data/aggregator.db:predictions   per-trip rise probability
"""
from __future__ import annotations
import os
import json
import sqlite3
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DB_PATH = os.environ.get("AGG_DB", os.path.join(ROOT, "data", "aggregator.db"))
MODEL_DIR = os.path.join(ROOT, "models")

import features as F  # noqa: E402


def train():
    os.makedirs(MODEL_DIR, exist_ok=True)
    import lightgbm as lgb
    from sklearn.metrics import roc_auc_score, accuracy_score, classification_report

    con = sqlite3.connect(DB_PATH)
    df = F.build_training_frame(con)

    # Time-aware split: earlier captures train, most recent captures test.
    df = df.sort_values("captured_at")
    cut = int(len(df) * 0.8)
    train_df, test_df = df.iloc[:cut], df.iloc[cut:]

    Xtr, ytr = train_df[F.FEATURE_COLS], train_df["price_will_rise"]
    Xte, yte = test_df[F.FEATURE_COLS], test_df["price_will_rise"]

    dtrain = lgb.Dataset(Xtr, label=ytr)
    dtest = lgb.Dataset(Xte, label=yte, reference=dtrain)
    params = {
        "objective": "binary",
        "metric": ["auc", "binary_logloss"],
        "learning_rate": 0.05,
        "num_leaves": 31,
        "min_data_in_leaf": 40,
        "feature_fraction": 0.9,
        "bagging_fraction": 0.9,
        "bagging_freq": 1,
        "is_unbalance": True,
        "verbose": -1,
    }
    booster = lgb.train(
        params, dtrain, num_boost_round=400, valid_sets=[dtrain, dtest],
        valid_names=["train", "test"],
        callbacks=[lgb.early_stopping(40, verbose=False), lgb.log_evaluation(0)],
    )

    proba = booster.predict(Xte)
    pred = (proba >= 0.5).astype(int)
    auc = roc_auc_score(yte, proba)
    acc = accuracy_score(yte, pred)
    base_rate = float(ytr.mean())

    print(f"  train rows: {len(train_df):,} | test rows: {len(test_df):,}")
    print(f"  base rate (price rises): {base_rate:.1%}")
    print(f"  test AUC:      {auc:.3f}")
    print(f"  test accuracy: {acc:.1%}")
    print("  " + classification_report(yte, pred, digits=3, zero_division=0).replace("\n", "\n  "))

    importance = dict(sorted(
        zip(F.FEATURE_COLS, booster.feature_importance(importance_type="gain")),
        key=lambda kv: -kv[1],
    ))

    booster.save_model(os.path.join(MODEL_DIR, "price_rise_lgbm.txt"))
    metrics = {
        "auc": round(float(auc), 4),
        "accuracy": round(float(acc), 4),
        "base_rate": round(base_rate, 4),
        "n_train": int(len(train_df)),
        "n_test": int(len(test_df)),
        "best_iteration": booster.best_iteration,
        "feature_importance_gain": {k: float(round(v, 1)) for k, v in importance.items()},
    }
    with open(os.path.join(MODEL_DIR, "metrics.json"), "w") as f:
        json.dump(metrics, f, indent=2)

    _write_predictions(con, booster)
    con.close()
    print(f"  saved model + metrics to {MODEL_DIR}")
    return metrics


def _write_predictions(con: sqlite3.Connection, booster):
    """Score the latest snapshot per trip and persist for the API."""
    live = F.build_live_features(con)
    live["rise_prob"] = booster.predict(live[F.FEATURE_COLS])
    cols = ["trip_id", "route_id", "carrier", "mode", "departure_date",
            "departure_hour", "duration_min", "price_ngn", "days_to_departure",
            "rise_prob"]
    out = live[cols].copy()
    out["departure_date"] = out["departure_date"].dt.date.astype(str)
    con.execute("DROP TABLE IF EXISTS predictions")
    out.to_sql("predictions", con, index=False)
    con.commit()
    print(f"  wrote {len(out)} live predictions to aggregator.db:predictions")


if __name__ == "__main__":
    print("Stage 3: training LightGBM price-rise model...")
    train()
