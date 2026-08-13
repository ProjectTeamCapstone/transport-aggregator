"""Loads the trained model and scores a single offer on demand.

The awkward part of serving this model is that one of its features - the recent
price deltas - cannot be read off the offer itself. It has to be reconstructed
from that departure's own history in PostgreSQL, using EXACTLY the same
arithmetic the training set used. Two implementations of one rule is how a model
silently degrades in production, so this module calls the same functions in
ml.features rather than reimplementing them.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

import lightgbm as lgb
import pandas as pd
import psycopg

from common.config import postgres_dsn
from ml.features import (
    DELTA_LAGS_HOURS,
    FEATURE_COLUMNS,
    HISTORY_COLUMNS,
    frame_from,
)

ARTIFACT_DIR = Path(__file__).resolve().parent / "artifacts"
MODEL_PATH = ARTIFACT_DIR / "price_rise_lgbm.txt"
METADATA_PATH = ARTIFACT_DIR / "model_metadata.json"

# How much history to pull when scoring. Must comfortably exceed the largest
# delta lag or the deltas come back as 0.0 and the model is scoring blind.
HISTORY_WINDOW_HOURS = max(DELTA_LAGS_HOURS) * 2


class ModelNotTrained(RuntimeError):
    """Raised when no trained model is on disk."""


@lru_cache(maxsize=1)
def load_model() -> tuple[lgb.Booster, dict[str, Any]]:
    if not MODEL_PATH.exists():
        raise ModelNotTrained(
            f"No model at {MODEL_PATH}. Train one with: python -m ml.train"
        )
    booster = lgb.Booster(model_file=str(MODEL_PATH))
    metadata = json.loads(METADATA_PATH.read_text()) if METADATA_PATH.exists() else {}
    return booster, metadata


def is_available() -> bool:
    return MODEL_PATH.exists()


def _history_for(offer_id: str) -> pd.DataFrame:
    """This departure's recent observations, most recent last."""
    with psycopg.connect(postgres_dsn(), connect_timeout=10) as conn:
        return frame_from(conn.execute(
            f"SELECT {', '.join(HISTORY_COLUMNS)} FROM offers_history "
            "WHERE offer_id = %(offer_id)s "
            "  AND fetched_at >= now() - make_interval(hours => %(hours)s) "
            "ORDER BY fetched_at",
            {"offer_id": offer_id, "hours": HISTORY_WINDOW_HOURS},
        ))


def _features_for_latest(history: pd.DataFrame) -> pd.DataFrame:
    """Build the feature row for the most recent observation.

    build_features drops rows that have no 24-hours-later observation, which is
    every recent row - correct for training, useless for scoring. So the label
    machinery is bypassed and only the point-in-time features are rebuilt, using
    the same functions.
    """
    from ml.features import _delta_over  # same arithmetic as training

    df = history.copy()
    df["fetched_at"] = pd.to_datetime(df["fetched_at"], utc=True)
    df["depart_time"] = pd.to_datetime(df["depart_time"], utc=True)
    df["price_ngn"] = df["price_ngn"].astype(float)
    df = df.sort_values(["offer_id", "fetched_at"]).reset_index(drop=True)

    now = datetime.now(timezone.utc)
    df["days_to_departure"] = (
        (df["depart_time"] - pd.Timestamp(now)).dt.total_seconds() / 86_400
    )
    local_departure = df["depart_time"] + pd.Timedelta(seconds=3600)
    df["hour_of_day"] = local_departure.dt.hour
    df["day_of_week"] = local_departure.dt.dayofweek
    df["route"] = df["origin"] + "-" + df["destination"]
    for lag in DELTA_LAGS_HOURS:
        df[f"price_delta_{lag}h"] = _delta_over(df, lag)

    return df.tail(1)


def predict(offer_id: str) -> dict[str, Any]:
    """Score one offer. Returns the endpoint's payload.

    Raises KeyError when the offer has no history at all, and ModelNotTrained
    when no model exists - both are the caller's to turn into an HTTP status.
    """
    booster, metadata = load_model()

    history = _history_for(offer_id)
    if history.empty:
        raise KeyError(offer_id)

    row = _features_for_latest(history)
    frame = row[FEATURE_COLUMNS].copy()
    for column in ("carrier", "route"):
        frame[column] = frame[column].astype("category")

    probability = float(booster.predict(frame)[0])
    observations = len(history)

    return {
        "offer_id": offer_id,
        "will_rise": bool(probability >= 0.5),
        "probability": round(probability, 4),
        "model_version": metadata.get("model_version", "unknown"),
        "based_on_observations": observations,
        # Deltas are 0.0 when a departure has too little history, which means
        # the model is working from the point-in-time features alone. Saying so
        # is more useful than quietly returning a weaker number.
        "has_price_history": bool(
            observations > 1 and float(row["price_delta_6h"].iloc[0]) != 0.0
        ),
        "current_price_ngn": float(row["price_ngn"].iloc[0]),
        "data_provenance": metadata.get("data_provenance", ""),
    }
