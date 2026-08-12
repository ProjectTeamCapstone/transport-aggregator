"""
Shared feature engineering — used by both training and live serving so the
model always sees identically-built features.

Label (training only): will this offer's price be HIGHER at the next daily
snapshot (i.e. ~24h later) for the same trip? -> price_will_rise in {0,1}

Features per offer snapshot:
  price_ngn            current fare
  days_to_departure    days until departure at capture time
  duration_min         journey time
  departure_hour       scheduled departure hour
  dow                  day-of-week of departure (0=Mon)
  is_air               1 if air, else 0
  price_vs_route_med   price / median price for that route+date (relative level)
  price_trend_3d       recent slope: (price - price_3_snapshots_ago)/price
  carrier_rank         cheapness rank of carrier on the route (1 = cheapest avg)
"""
from __future__ import annotations
import sqlite3
import pandas as pd
import numpy as np

FEATURE_COLS = [
    "price_ngn", "days_to_departure", "duration_min", "departure_hour",
    "dow", "is_air", "price_vs_route_med", "price_trend_3d", "carrier_rank",
]


def _load_offers(con: sqlite3.Connection) -> pd.DataFrame:
    df = pd.read_sql_query("SELECT * FROM offers", con)
    df["captured_at"] = pd.to_datetime(df["captured_at"])
    df["departure_date"] = pd.to_datetime(df["departure_date"])
    df["dow"] = df["departure_date"].dt.dayofweek
    df["is_air"] = (df["mode"] == "air").astype(int)
    return df


def _add_relative_and_trend(df: pd.DataFrame) -> pd.DataFrame:
    # price relative to the route+date median at the same capture snapshot
    med = (df.groupby(["route_id", "departure_date", "days_to_departure"])["price_ngn"]
             .transform("median"))
    df["price_vs_route_med"] = df["price_ngn"] / med

    # recent price trend within the same trip (ordered by capture time)
    df = df.sort_values(["trip_id", "captured_at"])
    grp = df.groupby("trip_id")["price_ngn"]
    prev3 = grp.shift(3)
    df["price_trend_3d"] = ((df["price_ngn"] - prev3) / prev3).fillna(0.0)

    # carrier cheapness rank on each route (1 = cheapest average fare)
    carr_avg = df.groupby(["route_id", "carrier"])["price_ngn"].transform("mean")
    df["_carr_avg"] = carr_avg
    ranks = (df[["route_id", "carrier", "_carr_avg"]].drop_duplicates()
               .sort_values(["route_id", "_carr_avg"]))
    ranks["carrier_rank"] = ranks.groupby("route_id").cumcount() + 1
    df = df.merge(ranks[["route_id", "carrier", "carrier_rank"]],
                  on=["route_id", "carrier"], how="left")
    return df.drop(columns=["_carr_avg"])


def build_training_frame(con: sqlite3.Connection) -> pd.DataFrame:
    """Return a frame with FEATURE_COLS + 'price_will_rise' label."""
    df = _load_offers(con)
    df = _add_relative_and_trend(df)
    # label: next daily snapshot price for the same trip (days_to_dep decreases by 1)
    df = df.sort_values(["trip_id", "days_to_departure"], ascending=[True, False])
    nxt = df.groupby("trip_id")["price_ngn"].shift(-1)
    df["next_price"] = nxt
    df = df.dropna(subset=["next_price"])
    df["price_will_rise"] = (df["next_price"] > df["price_ngn"]).astype(int)
    return df


def build_live_features(con: sqlite3.Connection) -> pd.DataFrame:
    """
    Latest snapshot per trip (what a traveller sees now) with model features,
    for scoring + search. Returns offers-with-features, one row per trip.
    """
    df = _load_offers(con)
    df = _add_relative_and_trend(df)
    latest = (df.sort_values("captured_at")
                .groupby("trip_id", as_index=False)
                .tail(1)
                .reset_index(drop=True))
    return latest