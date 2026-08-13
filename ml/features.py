"""Feature engineering and labelling for the price-rise model.

The seven features the project specifies:

    1. days_to_departure   how far ahead the observation was made
    2. hour_of_day         departure hour, local to the origin
    3. day_of_week         departure weekday
    4. seats_left          remaining inventory
    5. recent price deltas price change over the last 6, 12 and 24 hours
    6. carrier             categorical
    7. route               categorical

The label: did this departure's price RISE in the following 24 hours?

Two rules everything here obeys, because breaking either produces a model that
scores brilliantly and is worthless:

**No future information.** Every feature for an observation at time t is
computed only from data available at or before t. A feature that peeks ahead
(say, the minimum price ever seen for this departure) leaks the answer, and the
model would score near-perfectly in testing and fail completely in use.

**Deltas are per departure, not per route.** A price change only means anything
compared with the SAME departure's earlier price. Comparing across departures
would mix "this flight got more expensive" with "a different flight is dearer".
"""

from __future__ import annotations

import pandas as pd

# How far ahead we predict, and the lags used as features.
LABEL_HORIZON_HOURS = 24
DELTA_LAGS_HOURS = (6, 12, 24)

FEATURE_COLUMNS = [
    "days_to_departure",
    "hour_of_day",
    "day_of_week",
    "seats_left",
    "price_delta_6h",
    "price_delta_12h",
    "price_delta_24h",
    "carrier",
    "route",
]
CATEGORICAL_COLUMNS = ["carrier", "route"]
LABEL_COLUMN = "will_rise"


HISTORY_COLUMNS = [
    "offer_id", "carrier", "mode", "origin", "destination",
    "depart_time", "duration_min", "price_ngn", "seats_left", "fetched_at",
]


def frame_from(cursor) -> pd.DataFrame:
    """Build a DataFrame from a psycopg cursor.

    pandas.read_sql warns on a raw DBAPI connection and asks for SQLAlchemy.
    Doing it directly avoids both the warning and an extra dependency.
    """
    return pd.DataFrame(cursor.fetchall(), columns=HISTORY_COLUMNS)


def load_history(conn) -> pd.DataFrame:
    """Read the raw price history. One row per observation of a departure."""
    return frame_from(conn.execute(
        f"SELECT {', '.join(HISTORY_COLUMNS)} FROM offers_history "
        "ORDER BY offer_id, fetched_at"
    ))


def build_features(history: pd.DataFrame) -> pd.DataFrame:
    """Turn raw observations into model-ready rows, labels included.

    Rows without a complete label are dropped, not guessed. An observation in
    the final 24 hours of the data has no "24 hours later" to compare against,
    so its outcome is genuinely unknown - inventing one would teach the model
    something false.
    """
    if history.empty:
        return pd.DataFrame(columns=FEATURE_COLUMNS + [LABEL_COLUMN])

    df = history.copy()
    df["fetched_at"] = pd.to_datetime(df["fetched_at"], utc=True)
    df["depart_time"] = pd.to_datetime(df["depart_time"], utc=True)
    df["price_ngn"] = df["price_ngn"].astype(float)
    df = df.sort_values(["offer_id", "fetched_at"]).reset_index(drop=True)

    # --- features available at observation time --------------------------
    df["days_to_departure"] = (
        (df["depart_time"] - df["fetched_at"]).dt.total_seconds() / 86_400
    )
    # Departure hour/weekday in the ORIGIN's local time. Using UTC would shift
    # every Nigerian departure back an hour and blur the rush-hour signal the
    # simulator (and real pricing) actually contains.
    local_departure = df["depart_time"] + pd.Timedelta(seconds=3600)
    df["hour_of_day"] = local_departure.dt.hour
    df["day_of_week"] = local_departure.dt.dayofweek
    df["route"] = df["origin"] + "-" + df["destination"]

    # --- recent price deltas, per departure ------------------------------
    for lag_hours in DELTA_LAGS_HOURS:
        df[f"price_delta_{lag_hours}h"] = _delta_over(df, lag_hours)

    # --- the label -------------------------------------------------------
    future = _price_at_offset(df, LABEL_HORIZON_HOURS)
    df["future_price"] = future
    df = df[df["future_price"].notna()].copy()
    df[LABEL_COLUMN] = (df["future_price"] > df["price_ngn"]).astype(int)

    return df


def _price_at_offset(df: pd.DataFrame, hours: int) -> pd.Series:
    """Price of the SAME departure `hours` later, or NaN if not observed.

    merge_asof with direction='forward' finds the first observation at or after
    the target moment, within a tolerance, so an irregular observation schedule
    does not silently drop everything.
    """
    left = df[["offer_id", "fetched_at"]].copy()
    # Carry the original row identity through the merge. merge_asof REQUIRES
    # its input sorted by the join key and returns a fresh RangeIndex, so the
    # original index is destroyed by the sort. Sorting the result back by that
    # new index restores the sort order, NOT the original row order - which
    # silently pairs every observation with another row's future price.
    left["_row"] = df.index
    left["target"] = left["fetched_at"] + pd.Timedelta(seconds=int(hours * 3600))
    left = left.sort_values("target")

    right = df[["offer_id", "fetched_at", "price_ngn"]].rename(
        columns={"fetched_at": "observed_at", "price_ngn": "observed_price"}
    ).sort_values("observed_at")

    merged = pd.merge_asof(
        left, right,
        left_on="target", right_on="observed_at",
        by="offer_id", direction="forward",
        tolerance=pd.Timedelta(seconds=int(hours * 1800)),
    )
    return merged.set_index("_row")["observed_price"].reindex(df.index)


def _delta_over(df: pd.DataFrame, hours: int) -> pd.Series:
    """Price change since `hours` ago, as a fraction of the earlier price.

    A fraction rather than an absolute figure, so a NGN 2,000 move on a
    NGN 30,000 coach and on a NGN 1,500,000 London fare are not treated as the
    same event. Unknown history yields 0.0 - "no observed change" - rather than
    a missing value, because at prediction time a brand-new departure genuinely
    has no history and must still be scorable.
    """
    left = df[["offer_id", "fetched_at", "price_ngn"]].copy()
    left["_row"] = df.index          # see _price_at_offset - same trap
    left["target"] = left["fetched_at"] - pd.Timedelta(seconds=int(hours * 3600))
    left = left.sort_values("target")

    right = df[["offer_id", "fetched_at", "price_ngn"]].rename(
        columns={"fetched_at": "observed_at", "price_ngn": "past_price"}
    ).sort_values("observed_at")

    merged = pd.merge_asof(
        left, right,
        left_on="target", right_on="observed_at",
        by="offer_id", direction="nearest",
        tolerance=pd.Timedelta(seconds=int(hours * 1800)),
    )

    past = merged.set_index("_row")["past_price"].reindex(df.index)
    delta = (df["price_ngn"] - past) / past
    return delta.fillna(0.0)


def time_based_split(df: pd.DataFrame, train_fraction: float = 0.7
                     ) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split on observation time, never at random.

    A random split would put an observation from Tuesday afternoon in training
    and one of the SAME departure from Tuesday morning in testing. The model
    would effectively see the answer, score far too well, and tell us nothing
    about predicting a future it has not seen. Splitting on time reproduces the
    only situation that matters: learning from the past, predicting forward.

    The boundary falls on a timestamp, so no single moment straddles both sides.
    """
    if df.empty:
        return df, df

    ordered = df.sort_values("fetched_at")
    cutoff = ordered["fetched_at"].quantile(train_fraction)

    train = ordered[ordered["fetched_at"] <= cutoff]
    test = ordered[ordered["fetched_at"] > cutoff]
    return train, test


def to_model_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Feature columns only, with categoricals typed for LightGBM."""
    frame = df[FEATURE_COLUMNS].copy()
    for column in CATEGORICAL_COLUMNS:
        frame[column] = frame[column].astype("category")
    return frame
