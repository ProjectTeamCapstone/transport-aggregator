"""Train the price-rise classifier and record the run in MLflow.

    python -m ml.train
    python -m ml.train --no-search      # skip the grid search

Reports AUC on a held-out, time-separated test set. The target is 0.70; if it
is not met the number is reported as measured, not massaged.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lightgbm as lgb  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import psycopg  # noqa: E402
from sklearn.metrics import (  # noqa: E402
    accuracy_score,
    brier_score_loss,
    roc_auc_score,
)

from common.config import postgres_dsn  # noqa: E402
from ml.features import (  # noqa: E402
    CATEGORICAL_COLUMNS,
    FEATURE_COLUMNS,
    LABEL_COLUMN,
    build_features,
    load_history,
    time_based_split,
    to_model_frame,
)

MODEL_DIR = Path(__file__).resolve().parent / "artifacts"
MODEL_PATH = MODEL_DIR / "price_rise_lgbm.txt"
METADATA_PATH = MODEL_DIR / "model_metadata.json"
TARGET_AUC = 0.70

BASE_PARAMS = {
    "objective": "binary",
    "metric": "auc",
    "verbosity": -1,
    "seed": 608,
    # Small, shallow trees. The dataset has seven features and a lot of rows;
    # a deep forest would memorise individual departures instead of learning
    # the pricing behaviour that generalises.
    "num_leaves": 31,
    "learning_rate": 0.05,
    "feature_fraction": 0.9,
    "bagging_fraction": 0.8,
    "bagging_freq": 5,
    "min_data_in_leaf": 100,
}

# Deliberately small. The approved fallback says to use plain LightGBM with a
# scikit-learn style search when the data does not justify Ray, and it does not:
# this is a few hundred thousand rows of seven features on one laptop.
SEARCH_GRID = [
    {"num_leaves": 15, "learning_rate": 0.05, "min_data_in_leaf": 200},
    {"num_leaves": 31, "learning_rate": 0.05, "min_data_in_leaf": 100},
    {"num_leaves": 63, "learning_rate": 0.03, "min_data_in_leaf": 50},
    {"num_leaves": 31, "learning_rate": 0.10, "min_data_in_leaf": 100},
]


def _fit(params: dict, train: pd.DataFrame, valid: pd.DataFrame,
         rounds: int = 600) -> tuple[lgb.Booster, float]:
    train_set = lgb.Dataset(
        to_model_frame(train), label=train[LABEL_COLUMN],
        categorical_feature=CATEGORICAL_COLUMNS, free_raw_data=False,
    )
    valid_set = lgb.Dataset(
        to_model_frame(valid), label=valid[LABEL_COLUMN],
        categorical_feature=CATEGORICAL_COLUMNS, reference=train_set,
        free_raw_data=False,
    )
    booster = lgb.train(
        {**BASE_PARAMS, **params}, train_set,
        num_boost_round=rounds, valid_sets=[valid_set],
        callbacks=[lgb.early_stopping(50, verbose=False)],
    )
    scores = booster.predict(to_model_frame(valid))
    return booster, roc_auc_score(valid[LABEL_COLUMN], scores)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-search", action="store_true")
    parser.add_argument("--train-fraction", type=float, default=0.7)
    args = parser.parse_args()

    print("==> Loading price history from PostgreSQL")
    with psycopg.connect(postgres_dsn()) as conn:
        history = load_history(conn)
    print(f"    {len(history):,} observations of "
          f"{history['offer_id'].nunique():,} departures")

    print("==> Building features and labels")
    data = build_features(history)
    if data.empty:
        print("ERROR: no labelled rows. The history is too short to contain a "
              "24-hour outcome. Run: python -m ml.backfill --days 45")
        return 1

    positive_rate = data[LABEL_COLUMN].mean()
    print(f"    {len(data):,} labelled rows, "
          f"{positive_rate:.1%} of them price rises")

    # A wildly lopsided label makes AUC easy to misread, so it is stated.
    if positive_rate < 0.05 or positive_rate > 0.95:
        print(f"    WARNING: labels are heavily imbalanced ({positive_rate:.1%})")

    print("==> Splitting by TIME, not at random")
    train, test = time_based_split(data, args.train_fraction)
    print(f"    train: {len(train):,} rows up to {train['fetched_at'].max()}")
    print(f"    test : {len(test):,} rows from {test['fetched_at'].min()}")
    if train.empty or test.empty:
        print("ERROR: split produced an empty side; not enough history.")
        return 1

    # An inner split of the TRAINING period only. Touching the test set to
    # choose hyper-parameters would leak it and inflate the final number.
    inner_train, inner_valid = time_based_split(train, 0.8)

    import mlflow

    mlflow.set_tracking_uri(f"file://{Path(__file__).resolve().parent.parent}/mlruns")
    mlflow.set_experiment("price-rise")

    candidates = [{}] if args.no_search else SEARCH_GRID
    best_params, best_auc, best_booster = {}, -1.0, None

    print(f"==> Training {len(candidates)} candidate(s)")
    for i, params in enumerate(candidates, 1):
        with mlflow.start_run(run_name=f"candidate-{i}"):
            booster, auc = _fit(params, inner_train, inner_valid)
            mlflow.log_params({**BASE_PARAMS, **params})
            mlflow.log_metric("validation_auc", auc)
            mlflow.log_metric("best_iteration", booster.best_iteration)
            print(f"    [{i}/{len(candidates)}] {params or 'defaults'} "
                  f"-> validation AUC {auc:.4f}")
            if auc > best_auc:
                best_params, best_auc, best_booster = params, auc, booster

    print(f"==> Refitting the best candidate on the full training period")
    with mlflow.start_run(run_name="final"):
        booster, _ = _fit(best_params, train, test)

        probabilities = booster.predict(to_model_frame(test))
        truth = test[LABEL_COLUMN]
        auc = roc_auc_score(truth, probabilities)
        accuracy = accuracy_score(truth, (probabilities >= 0.5).astype(int))
        brier = brier_score_loss(truth, probabilities)

        importance = dict(zip(
            booster.feature_name(),
            (int(v) for v in booster.feature_importance("gain")),
        ))

        # The honest yardstick. Labels are heavily skewed towards "rise", so
        # always answering "rise" already scores well on accuracy while being
        # useless. Reporting accuracy without this baseline overstates the
        # model; AUC is the metric that is not fooled by the skew.
        majority_rate = float(max(truth.mean(), 1 - truth.mean()))
        accuracy_lift = accuracy - majority_rate

        mlflow.log_params({**BASE_PARAMS, **best_params})
        mlflow.log_metrics({
            "test_auc": auc, "test_accuracy": accuracy,
            "test_brier": brier, "train_rows": len(train), "test_rows": len(test),
            "baseline_accuracy": majority_rate, "accuracy_lift": accuracy_lift,
        })

        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        booster.save_model(str(MODEL_PATH))
        mlflow.log_artifact(str(MODEL_PATH))

        metadata = {
            "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "model_version": f"lgbm-{datetime.now(timezone.utc):%Y%m%d%H%M}",
            "features": FEATURE_COLUMNS,
            "categorical_features": CATEGORICAL_COLUMNS,
            "params": {**BASE_PARAMS, **best_params},
            "best_iteration": booster.best_iteration,
            "rows": {"train": len(train), "test": len(test)},
            "split": {
                "kind": "time-based",
                "train_ends": str(train["fetched_at"].max()),
                "test_starts": str(test["fetched_at"].min()),
            },
            "label_positive_rate": float(positive_rate),
            "metrics": {"test_auc": auc, "test_accuracy": accuracy,
                        "test_brier": brier,
                        "baseline_accuracy_always_majority": majority_rate,
                        "accuracy_lift_over_baseline": accuracy_lift},
            "feature_importance_gain": importance,
            "data_provenance": (
                "SIMULATED. Trained on price history generated by the project's "
                "own simulator (see ml/backfill.py). This measures whether the "
                "model can recover the simulator's pricing rules - not whether "
                "it can predict real Nigerian fares."
            ),
        }
        METADATA_PATH.write_text(json.dumps(metadata, indent=2))
        mlflow.log_artifact(str(METADATA_PATH))

    print("\n" + "=" * 62)
    print(f"  Held-out test AUC : {auc:.4f}   (target {TARGET_AUC})")
    print(f"  Accuracy          : {accuracy:.4f}")
    print(f"  Baseline accuracy : {majority_rate:.4f}   (always answer the "
          f"majority class)")
    print(f"  Lift over baseline: {accuracy_lift:+.4f}")
    print(f"  Brier score       : {brier:.4f}   (lower is better)")
    print("=" * 62)
    print(f"  {'MEETS' if auc >= TARGET_AUC else 'BELOW'} the {TARGET_AUC} target")
    print("\n  Feature importance (gain):")
    for name, gain in sorted(importance.items(), key=lambda kv: -kv[1]):
        print(f"    {name:<22} {gain:>12,}")
    print(f"\n  Model    -> {MODEL_PATH}")
    print(f"  Metadata -> {METADATA_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
