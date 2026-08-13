-- Append-only price history + booking ledger.
-- Runs automatically on first container start (docker-entrypoint-initdb.d).

-- ---------------------------------------------------------------- offers ---
-- One row per OBSERVATION, never updated. If Air Peace's 07:00 Lagos-Abuja
-- flight is polled every 5 minutes, that is one row every 5 minutes. The
-- repetition is the point: the price movement between rows is the signal the
-- prediction model learns from.
CREATE TABLE IF NOT EXISTS offers_history (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    offer_id        TEXT        NOT NULL,
    carrier         TEXT        NOT NULL,
    mode            TEXT        NOT NULL CHECK (mode IN ('road', 'air')),
    origin          CHAR(3)     NOT NULL,
    destination     CHAR(3)     NOT NULL,
    depart_time     TIMESTAMPTZ NOT NULL,
    duration_min    INTEGER     NOT NULL CHECK (duration_min > 0),
    price_ngn       NUMERIC(12, 2) NOT NULL CHECK (price_ngn > 0),
    seats_left      INTEGER     NOT NULL CHECK (seats_left >= 0),
    booking_url     TEXT        NOT NULL,
    fetched_at      TIMESTAMPTZ NOT NULL,
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Same departure, same observation moment = the same fact. Re-delivering it
    -- (Kafka guarantees at-least-once, so this WILL happen) must not create a
    -- second row, or the model would learn from phantom duplicates.
    CONSTRAINT offers_history_dedup UNIQUE (offer_id, fetched_at)
);

-- Feature engineering reads "all observations of this departure, in order".
CREATE INDEX IF NOT EXISTS idx_offers_offer_time
    ON offers_history (offer_id, fetched_at DESC);

-- The Shiny dashboard and /search read "this route, around this date".
CREATE INDEX IF NOT EXISTS idx_offers_route_depart
    ON offers_history (origin, destination, depart_time);

CREATE INDEX IF NOT EXISTS idx_offers_carrier_fetched
    ON offers_history (carrier, fetched_at DESC);

-- -------------------------------------------------------------- bookings ---
-- The idempotency ledger. A row is written here BEFORE any carrier API call,
-- so a lost response leaves a booking in 'unknown' instead of risking a
-- duplicate ticket on retry.
CREATE TABLE IF NOT EXISTS bookings (
    idempotency_key TEXT PRIMARY KEY,
    offer_id        TEXT        NOT NULL,
    carrier         TEXT        NOT NULL,
    state           TEXT        NOT NULL DEFAULT 'pending'
                    CHECK (state IN ('pending', 'confirmed', 'failed', 'unknown')),
    price_ngn_quoted  NUMERIC(12, 2),
    price_ngn_at_book NUMERIC(12, 2),
    provider          TEXT,          -- 'duffel_sandbox' | 'deeplink'
    provider_reference TEXT,         -- sandbox booking reference, when we get one
    attempts        INTEGER     NOT NULL DEFAULT 0,
    last_error      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_bookings_state ON bookings (state, created_at DESC);

-- ------------------------------------------------------ model performance ---
-- Lets the dashboard show prediction accuracy over time, which requires
-- remembering what we predicted before the outcome was known.
CREATE TABLE IF NOT EXISTS predictions (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    offer_id        TEXT        NOT NULL,
    predicted_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    will_rise       BOOLEAN     NOT NULL,
    probability     DOUBLE PRECISION NOT NULL
                    CHECK (probability >= 0 AND probability <= 1),
    model_version   TEXT        NOT NULL,
    price_at_prediction NUMERIC(12, 2),
    actual_rose     BOOLEAN,       -- filled in 24h later by the scoring job
    scored_at       TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_predictions_offer ON predictions (offer_id, predicted_at DESC);
CREATE INDEX IF NOT EXISTS idx_predictions_unscored ON predictions (predicted_at) WHERE actual_rose IS NULL;
