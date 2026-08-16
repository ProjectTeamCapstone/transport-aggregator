"""The route cache, and the rate limits.

Both are performance mechanisms with a correctness edge, and the edge is what
is tested here: a cache that softened staleness reporting would hide dead
connectors, and a rate limiter counting in process memory would enforce four
different limits under `--workers 4`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from tests.conftest import make_offer, put


def iso(**delta) -> str:
    return (
        (datetime.now(timezone.utc) + timedelta(**delta))
        .replace(microsecond=0)
        .isoformat()
    )


# ------------------------------------------------------------ route cache ---

def test_the_cache_serves_a_second_read_without_touching_redis(fake_redis, monkeypatch):
    from api.store import OfferStore

    store = OfferStore(cache_ttl_seconds=60)
    monkeypatch.setattr(store, "_redis", fake_redis)
    put(fake_redis, make_offer(offer_id="gigm-road-LOS-ABV-1"))

    reads = 0
    original = store._read_route

    def counting(origin, destination):
        nonlocal reads
        reads += 1
        return original(origin, destination)

    monkeypatch.setattr(store, "_read_route", counting)

    for _ in range(4):
        assert len(store.for_route("LOS", "ABV")) == 1

    assert reads == 1


def test_the_cache_does_not_soften_staleness(fake_redis, monkeypatch):
    """The property that makes the cache safe to have at all.

    `stale` and `age_seconds` come from the `fetched_at` INSIDE each record, not
    from when the list was read, so a cached offer still reports its true age. A
    cache that reset the clock would make a dead connector invisible for as long
    as its last prices stayed cached - the exact failure the staleness flag
    exists to reveal.
    """
    from api.store import OfferStore
    from common.ranking import annotate

    store = OfferStore(cache_ttl_seconds=60)
    monkeypatch.setattr(store, "_redis", fake_redis)
    put(fake_redis, make_offer(
        offer_id="gigm-road-LOS-ABV-old",
        depart_time=iso(days=1),
        fetched_at=iso(hours=-1),
    ))

    store.for_route("LOS", "ABV")                      # populate
    cached = store.for_route("LOS", "ABV")[0]          # served from cache

    enriched = annotate(cached, 900)
    assert enriched["stale"] is True
    assert enriched["age_seconds"] > 3500


def test_a_caller_cannot_mutate_the_cache(fake_redis, monkeypatch):
    """`for_route` hands out a copy of the list. /search filters the result in
    place, and doing that to the cached list would corrupt later searches."""
    from api.store import OfferStore

    store = OfferStore(cache_ttl_seconds=60)
    monkeypatch.setattr(store, "_redis", fake_redis)
    put(fake_redis, make_offer(offer_id="gigm-road-LOS-ABV-1"))

    store.for_route("LOS", "ABV").clear()

    assert len(store.for_route("LOS", "ABV")) == 1


def test_the_scan_pattern_does_not_leak_other_routes(fake_redis, monkeypatch):
    from api.store import OfferStore

    store = OfferStore(cache_ttl_seconds=0)
    monkeypatch.setattr(store, "_redis", fake_redis)
    put(fake_redis, make_offer(offer_id="gigm-road-LOS-ABV-1",
                               origin="LOS", destination="ABV"))
    put(fake_redis, make_offer(offer_id="gigm-road-LOS-ONI-1",
                               origin="LOS", destination="ONI"))

    assert len(store.for_route("LOS", "ABV")) == 1
    assert len(store.for_route("LOS", "ONI")) == 1


def test_redis_strings_come_back_as_numbers(fake_redis, monkeypatch):
    """A Redis hash stores only strings. The contract says numbers, and the
    ranking sorts on them - `"9000" < "25000"` is True as text and false as
    money."""
    from api.store import OfferStore

    store = OfferStore(cache_ttl_seconds=0)
    monkeypatch.setattr(store, "_redis", fake_redis)
    put(fake_redis, make_offer(
        offer_id="gigm-road-LOS-ABV-1", price_ngn=25_000.0,
        duration_min=600, seats_left=12,
    ))

    offer = store.get("gigm-road-LOS-ABV-1")

    assert isinstance(offer["price_ngn"], float)
    assert isinstance(offer["duration_min"], int)
    assert isinstance(offer["seats_left"], int)


# ------------------------------------------------------------ rate limits ---

@pytest.fixture
def limited_client(store, monkeypatch):
    """The API with rate limiting ON, counting in memory.

    An in-memory store, because what is under test is that the LIMIT is applied
    to the right endpoints - not that Redis can count. Redis is how the counter
    is SHARED between workers, which is a deployment property no single-process
    test can observe.
    """
    from limits.storage import MemoryStorage
    from limits.strategies import FixedWindowRateLimiter

    from api import main

    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main.limiter, "enabled", True)
    monkeypatch.setattr(
        main.limiter, "_limiter", FixedWindowRateLimiter(MemoryStorage())
    )
    return TestClient(main.app)


def test_nl_search_is_rate_limited(limited_client):
    """It can reach the Anthropic API, so an unlimited one spends our money."""
    statuses = [
        limited_client.get("/search/nl", params={"q": "cheapest to Abuja"}).status_code
        for _ in range(30)
    ]

    assert 429 in statuses, "no request in 30 was rate limited"
    assert statuses[0] != 429, "the first request was limited"


BOOKING = {
    "offer_id": "gigm-road-LOS-ABV-nope",
    "quoted_price_ngn": 25_000.0,
    "passenger": {
        "given_name": "Ada", "family_name": "Okoro",
        "email": "ada@example.test",
    },
}


def test_book_is_rate_limited(limited_client):
    """Every call writes a ledger row before anything else, so an unlimited
    /book is an unauthenticated write to the ledger."""
    statuses = [
        limited_client.post("/book", json=BOOKING).status_code for _ in range(20)
    ]

    assert 429 in statuses


def test_the_booking_body_still_parses_as_a_body(limited_client):
    """A regression guard with a specific history.

    Rate limiting was first written as a decorator on the handler. Because
    `api/main.py` uses `from __future__ import annotations`, the decorator moved
    annotation resolution into ITS module - where `BookingRequest` does not
    exist - so FastAPI quietly reclassified the request body as a query
    parameter and POST /book answered 422 to every valid booking.

    A 404 here means the body parsed and the offer was looked up. A 422 means
    the body was not understood, which is the bug.
    """
    response = limited_client.post("/book", json=BOOKING)

    assert response.status_code != 422, response.json()
    assert response.status_code == 404, "expected 'no such offer', not a parse failure"


def test_plain_search_is_not_limited_at_the_same_rate(limited_client):
    """/search is a cached Redis read and the thing the site exists to do.
    Throttling it as hard as /book would be protecting the wrong resource."""
    statuses = [
        limited_client.get(
            "/search", params={"origin": "LOS", "dest": "ABV"}
        ).status_code
        for _ in range(30)
    ]

    assert 429 not in statuses


def test_the_limit_message_says_what_to_do(limited_client):
    for _ in range(30):
        response = limited_client.get("/search/nl", params={"q": "cheapest to Abuja"})
        if response.status_code == 429:
            assert "Too many requests" in response.json()["detail"]
            return
    pytest.fail("never hit the limit")


def test_the_client_key_prefers_the_forwarded_address(monkeypatch):
    """Caddy proxies from 127.0.0.1, so counting the peer address would rate
    limit the entire internet as one client."""
    from starlette.datastructures import Headers

    from api.ratelimit import client_key

    class Req:
        headers = Headers({"x-forwarded-for": "41.58.1.9, 10.0.0.1"})
        client = type("C", (), {"host": "127.0.0.1"})()

    assert client_key(Req()) == "41.58.1.9"
