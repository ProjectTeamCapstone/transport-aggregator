"""Shared fixtures.

Two kinds of test live here and the split is deliberate:

**Unit tests** run everywhere, on a laptop with nothing installed and no
containers up. Redis is replaced by `fakeredis`, which speaks the real protocol
in-process, so `api/store.py` is exercised as written rather than mocked away.

**Integration tests** need a real PostgreSQL, and are marked `integration`. They
SKIP when one is unreachable and RUN in CI, where the workflow starts a
postgres:16-alpine service. They are not optional extras: the idempotency
guarantee is a claim about what the database does under a unique constraint and
a concurrent race, and a test that fakes the database would be testing the fake.
That is why they are skipped rather than mocked - a skipped test is honestly
absent, a mocked one is dishonestly present.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_SQL = REPO_ROOT / "infra" / "postgres" / "init" / "01_schema.sql"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "integration: needs a real PostgreSQL (skipped when absent)"
    )


# ----------------------------------------------------------------- offers ----

def make_offer(
    offer_id: str = "gigm-road-LOS-ABV-20260914T0600Z",
    carrier: str = "GIGM",
    mode: str = "road",
    origin: str = "LOS",
    destination: str = "ABV",
    price_ngn: float = 25_000.0,
    duration_min: int = 600,
    seats_left: int = 12,
    depart_time: str | None = None,
    fetched_at: str | None = None,
) -> dict[str, Any]:
    """A canonical offer, as the store hands one back.

    Departure defaults to tomorrow and `fetched_at` to now, so an offer built
    with no arguments is one /search will actually return - the endpoint drops
    departures in the past, and a fixture with a hardcoded date silently starts
    failing the day it goes by.
    """
    now = datetime.now(timezone.utc)
    depart = depart_time or (now + timedelta(days=1)).replace(
        microsecond=0
    ).isoformat()
    return {
        "offer_id": offer_id,
        "carrier": carrier,
        "mode": mode,
        "origin": origin,
        "destination": destination,
        "depart_time": depart,
        "duration_min": duration_min,
        "price_ngn": price_ngn,
        "seats_left": seats_left,
        "booking_url": f"https://example.test/{carrier.lower()}/book",
        "fetched_at": fetched_at or now.replace(microsecond=0).isoformat(),
    }


@pytest.fixture
def offer() -> dict[str, Any]:
    return make_offer()


# ------------------------------------------------------------------ redis ----

@pytest.fixture
def fake_redis():
    """An in-process Redis that speaks the real protocol."""
    fakeredis = pytest.importorskip("fakeredis")
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
def store(fake_redis, monkeypatch):
    """An OfferStore backed by fakeredis, with caching off.

    `cache_ttl_seconds=0` because these tests write to Redis and immediately
    read back. With the production 5-second window they would race it and fail
    intermittently, which is worse than failing outright. The cache has its own
    test that turns it back on.
    """
    from api.store import OfferStore

    instance = OfferStore(cache_ttl_seconds=0)
    monkeypatch.setattr(instance, "_redis", fake_redis)
    return instance


def put(fake_redis, offer: dict[str, Any]) -> None:
    """Write an offer into Redis the way the Kafka Connect sink does.

    Everything becomes a string, because that is what a Redis hash stores and
    therefore what the store has to cope with on the way back out.
    """
    fake_redis.hset(
        f"offers:{offer['offer_id']}",
        mapping={k: str(v) for k, v in offer.items()},
    )


# --------------------------------------------------------------- postgres ----

def _dsn() -> str:
    from common.config import postgres_dsn

    return os.getenv("TEST_POSTGRES_DSN") or postgres_dsn()


def _postgres_reachable() -> bool:
    try:
        import psycopg

        with psycopg.connect(_dsn(), connect_timeout=3) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def postgres() -> Iterator[str]:
    """A live PostgreSQL with the project's schema applied, or a skip.

    The real `01_schema.sql` is applied rather than a hand-written test table.
    The unique constraint on `idempotency_key` IS the double-booking guarantee,
    so a test schema that merely resembled the real one could pass while
    production duplicated tickets.
    """
    if not _postgres_reachable():
        message = (
            "no PostgreSQL at the configured DSN. Start one with "
            "`docker compose up -d postgres`, or set TEST_POSTGRES_DSN."
        )
        # On a laptop a missing database is a skip. In CI it is a failure.
        #
        # A skipped test is honestly absent, which is the right behaviour for
        # someone running the suite without containers up - but silently absent
        # is exactly how the double-booking guarantee would stop being tested
        # without anyone noticing. CI sets REQUIRE_POSTGRES=1 so a broken
        # service container turns the build red instead of green-with-skips.
        if os.getenv("REQUIRE_POSTGRES") == "1":
            pytest.fail(f"REQUIRE_POSTGRES=1 but {message}")
        pytest.skip(message)

    import psycopg

    with psycopg.connect(_dsn()) as conn:
        conn.execute(SCHEMA_SQL.read_text())
        conn.commit()

    yield _dsn()


@pytest.fixture
def clean_ledger(postgres, monkeypatch):
    """An empty `bookings` table, and a pool pointed at it.

    Truncated before each test rather than after, so a failure leaves its rows
    behind to be inspected.
    """
    import psycopg

    from common import db

    monkeypatch.setattr("common.config.postgres_dsn", lambda: postgres)
    monkeypatch.setattr("common.db.postgres_dsn", lambda: postgres)
    db.reset()

    with psycopg.connect(postgres) as conn:
        conn.execute("TRUNCATE bookings")
        conn.commit()

    yield postgres

    db.reset()
