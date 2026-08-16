"""The search path, end to end, against an in-process Redis.

No containers and no network: `fakeredis` speaks the real protocol, so
`api/store.py` runs exactly as written - the SCAN pattern, the pipeline, the
string coercion. Mocking the store instead would leave the code that actually
breaks untested.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import redis
from fastapi.testclient import TestClient

from tests.conftest import make_offer, put


@pytest.fixture
def client(store, monkeypatch):
    """The API with its module-level store swapped for the fakeredis one.

    Rate limiting is disabled here. It has its own test; leaving it on would
    make every other test in this file depend on how many requests the ones
    before it happened to make.
    """
    from api import main

    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main.limiter, "enabled", False)
    return TestClient(main.app)


def iso(**delta) -> str:
    return (
        (datetime.now(timezone.utc) + timedelta(**delta))
        .replace(microsecond=0)
        .isoformat()
    )


# ----------------------------------------------------------------- routing --

def test_unknown_place_is_a_400_naming_what_is_known(client):
    body = client.get("/search", params={"origin": "XXX", "dest": "ABV"}).json()
    assert "Unknown place code" in body["detail"]


def test_a_real_but_unsupported_route_is_a_400_not_an_empty_200(client):
    """"We don't do that journey" must be distinguishable from "nothing runs
    that day". An empty 200 would conflate them and send a traveller away."""
    response = client.get("/search", params={"origin": "ABV", "dest": "PHC"})
    assert response.status_code == 400
    assert "out of scope" in response.json()["detail"]


def test_a_covered_route_with_no_departures_is_an_empty_200(client, store):
    response = client.get("/search", params={"origin": "LOS", "dest": "ABV"})
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 0
    assert body["cheapest"] == [] and body["fastest"] == []


# ----------------------------------------------------------------- ranking --

def test_cheapest_and_fastest_disagree_and_both_are_returned(client, fake_redis):
    """The comparison the project exists for.

    The flight is quicker in the air (75 min vs 600) and dearer. It must top
    `fastest` and not `cheapest` - and it must win `fastest` on DOOR-TO-DOOR
    time, which for LOS->ABV adds 150 + 90 minutes of ground time to its 75.
    """
    put(fake_redis, make_offer(
        offer_id="gigm-road-LOS-ABV-x", carrier="GIGM", mode="road",
        price_ngn=25_000.0, duration_min=600, depart_time=iso(days=1),
    ))
    put(fake_redis, make_offer(
        offer_id="airpeace-air-LOS-ABV-x", carrier="AirPeace", mode="air",
        price_ngn=95_000.0, duration_min=75, depart_time=iso(days=1),
    ))

    body = client.get("/search", params={"origin": "LOS", "dest": "ABV"}).json()

    assert body["count"] == 2
    assert body["cheapest"][0]["carrier"] == "GIGM"
    assert body["fastest"][0]["carrier"] == "AirPeace"
    # 75 in the air, 315 door to door. The number the ranking actually used.
    assert body["fastest"][0]["door_to_door_min"] == 75 + 150 + 90


def test_a_road_trip_can_beat_a_flight_on_door_to_door_time(client, fake_redis):
    """Ground time is not decoration. A 4-hour flight-plus-airports loses to a
    5-hour coach, and ranking on scheduled time alone would hide that."""
    put(fake_redis, make_offer(
        offer_id="gigm-road-LOS-ABV-y", carrier="GIGM", mode="road",
        duration_min=300, depart_time=iso(days=1),
    ))
    put(fake_redis, make_offer(
        offer_id="airpeace-air-LOS-ABV-y", carrier="AirPeace", mode="air",
        duration_min=80, depart_time=iso(days=1),
    ))

    body = client.get("/search", params={"origin": "LOS", "dest": "ABV"}).json()
    assert body["fastest"][0]["carrier"] == "GIGM"


def test_limit_is_applied_to_both_lists(client, fake_redis):
    for n in range(5):
        put(fake_redis, make_offer(
            offer_id=f"gigm-road-LOS-ABV-{n}", price_ngn=20_000.0 + n * 1000,
            depart_time=iso(days=1),
        ))

    body = client.get(
        "/search", params={"origin": "LOS", "dest": "ABV", "limit": 2}
    ).json()

    assert body["count"] == 5
    assert len(body["cheapest"]) == 2 and len(body["fastest"]) == 2


# ------------------------------------------------------------------- time ---

def test_departures_already_gone_are_not_offered(client, fake_redis):
    put(fake_redis, make_offer(
        offer_id="gigm-road-LOS-ABV-past", depart_time=iso(hours=-2)
    ))
    put(fake_redis, make_offer(
        offer_id="gigm-road-LOS-ABV-future", depart_time=iso(hours=+2)
    ))

    body = client.get("/search", params={"origin": "LOS", "dest": "ABV"}).json()

    assert body["count"] == 1
    assert body["cheapest"][0]["offer_id"].endswith("future")


def test_an_offset_departure_is_compared_as_an_instant_not_as_text(
    client, fake_redis
):
    """The bug this guards is subtle and would look like working code.

    A departure written "+01:00" sorts AFTER a "+00:00" one as a string while
    being an hour earlier in real time. Comparing the text would keep offering
    departures that had already gone. Here the offer is 30 minutes in the past
    but written in a +01:00 offset, so a string comparison would keep it.
    """
    gone = datetime.now(timezone.utc) - timedelta(minutes=30)
    lagos_offset = timezone(timedelta(hours=1))
    put(fake_redis, make_offer(
        offer_id="gigm-road-LOS-ABV-tz",
        depart_time=gone.astimezone(lagos_offset).replace(microsecond=0).isoformat(),
    ))

    body = client.get("/search", params={"origin": "LOS", "dest": "ABV"}).json()
    assert body["count"] == 0, "a departure that has already gone was offered"


def test_date_filter_uses_the_travellers_local_date(client, fake_redis):
    """An overnight departure belongs to the date the traveller would name.

    00:30+01:00 on the 15th is 23:30Z on the 14th. Filtering on the UTC date
    would file it under the wrong day - and overnight coaches are the cheapest
    departures on these routes, so they are exactly what must not go missing.
    """
    lagos = timezone(timedelta(hours=1))
    overnight = (datetime.now(lagos) + timedelta(days=2)).replace(
        hour=0, minute=30, second=0, microsecond=0
    )
    put(fake_redis, make_offer(
        offer_id="gigm-road-LOS-ABV-night", depart_time=overnight.isoformat()
    ))

    local_date = overnight.date().isoformat()
    utc_date = overnight.astimezone(timezone.utc).date().isoformat()
    assert local_date != utc_date, "fixture is not actually an overnight case"

    on_local = client.get(
        "/search", params={"origin": "LOS", "dest": "ABV", "date": local_date}
    ).json()
    on_utc = client.get(
        "/search", params={"origin": "LOS", "dest": "ABV", "date": utc_date}
    ).json()

    assert on_local["count"] == 1
    assert on_utc["count"] == 0


# --------------------------------------------------------------- staleness --

def test_a_stale_price_is_flagged_and_still_shown(client, fake_redis):
    """Never hidden. An empty page also hides that a connector has died."""
    put(fake_redis, make_offer(
        offer_id="gigm-road-LOS-ABV-old",
        depart_time=iso(days=1),
        fetched_at=iso(hours=-1),          # STALE_AFTER_SECONDS defaults to 900
    ))

    body = client.get("/search", params={"origin": "LOS", "dest": "ABV"}).json()

    assert body["count"] == 1, "a stale offer was hidden instead of flagged"
    assert body["stale"] is True and body["stale_count"] == 1
    assert body["cheapest"][0]["stale"] is True


def test_stale_is_true_only_when_every_result_is_stale(client, fake_redis):
    """`stale` is the dead-connector signal; `stale_count` is the partial case."""
    put(fake_redis, make_offer(
        offer_id="gigm-road-LOS-ABV-fresh", depart_time=iso(days=1)
    ))
    put(fake_redis, make_offer(
        offer_id="gigm-road-LOS-ABV-stale",
        depart_time=iso(days=1), fetched_at=iso(hours=-1),
    ))

    body = client.get("/search", params={"origin": "LOS", "dest": "ABV"}).json()

    assert body["stale"] is False
    assert body["stale_count"] == 1


# ------------------------------------------------------- outage behaviour ---

def test_an_unreachable_redis_is_a_503_not_a_500(client, store, monkeypatch):
    """The gap this closes: `/book` reported an unreachable ledger carefully,
    while `/search` let a RedisError become a bare 500 stack trace."""
    def down(*_args, **_kwargs):
        raise redis.ConnectionError("connection refused")

    monkeypatch.setattr(store, "for_route", down)

    response = client.get("/search", params={"origin": "LOS", "dest": "ABV"})

    assert response.status_code == 503
    assert "unreachable" in response.json()["detail"]


def test_an_outage_is_never_reported_as_an_empty_result(client, store, monkeypatch):
    """The failure mode worth naming: telling a traveller nothing runs today
    when in fact we simply cannot see."""
    monkeypatch.setattr(
        store, "for_route",
        lambda *a, **k: (_ for _ in ()).throw(redis.ConnectionError("down")),
    )

    body = client.get("/search", params={"origin": "LOS", "dest": "ABV"}).json()
    assert "cheapest" not in body


def test_offer_lookup_reports_an_outage_too(client, store, monkeypatch):
    monkeypatch.setattr(
        store, "get",
        lambda *a, **k: (_ for _ in ()).throw(redis.ConnectionError("down")),
    )
    assert client.get("/offer/anything").status_code == 503


# ------------------------------------------------------------------ health --

def test_health_is_degraded_when_redis_is_down(client, store, monkeypatch):
    monkeypatch.setattr(store, "ping", lambda: False)

    body = client.get("/health").json()

    assert body["status"] == "degraded" and body["redis"] == "down"


def test_health_does_not_scan_the_keyspace_on_every_poll(client, store, fake_redis):
    """The count is cached, because /health is polled on an interval and
    counting means an O(keyspace) SCAN."""
    put(fake_redis, make_offer(offer_id="gigm-road-LOS-ABV-1"))
    store.clear_cache()

    scans = 0
    original = store._scan

    def counting_scan(pattern):
        nonlocal scans
        scans += 1
        return original(pattern)

    store._scan = counting_scan

    first = client.get("/health").json()
    for _ in range(5):
        client.get("/health")

    assert first["offers_cached"] == 1
    assert first["offers_counted_at"] is not None
    assert scans == 1, f"the keyspace was scanned {scans} times over six polls"
