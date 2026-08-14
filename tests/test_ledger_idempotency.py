"""The double-booking guarantee, executed.

This project's central claim is that a booking can never happen twice. Until
now that claim was argued in prose - in the docstrings of `booking/ledger.py`
and `booking/orchestrator.py`, both of which reason correctly. Prose does not
fail when someone reorders two statements.

Every test here needs a real PostgreSQL, and says so by being marked
`integration`. The guarantee is a claim about what the database does with a
unique constraint under a concurrent race; against a fake it would be a claim
about the fake.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from booking import ledger, orchestrator
from booking.providers import (
    BookingResult,
    Passenger,
    ProviderRejected,
    ProviderTimeout,
    ProviderUnreachable,
)
from tests.conftest import make_offer

pytestmark = pytest.mark.integration

PASSENGER = Passenger(
    given_name="Ada", family_name="Okoro", email="ada@example.test"
)


# ------------------------------------------------------------- test doubles --

class StubProvider:
    """A carrier that does exactly what the test tells it to.

    `calls` is the assertion that matters in most of these tests: not "what did
    the API return" but "how many times was the carrier actually asked".
    """

    name = "stub"

    def __init__(self, *behaviours: Any):
        self._behaviours = list(behaviours)
        self.calls = 0

    def create(self, offer, passenger, idempotency_key) -> BookingResult:
        self.calls += 1
        behaviour = self._behaviours[min(self.calls - 1, len(self._behaviours) - 1)]
        if isinstance(behaviour, Exception):
            raise behaviour
        return behaviour


def confirmed(reference: str = "ORD-123") -> BookingResult:
    return BookingResult(
        provider="stub", state="confirmed", reference=reference,
        detail="Booked.", handoff_url=None,
    )


def book(offer, provider, **kwargs):
    """orchestrator.book with the slow parts removed.

    `sleep` is a no-op so the backoff does not make the suite wait 1.5 seconds
    per retry test. The delay is a courtesy to a struggling carrier, not part
    of the correctness argument, so removing it changes nothing being asserted.
    """
    return orchestrator.book(
        offer=offer,
        passenger=kwargs.pop("passenger", PASSENGER),
        quoted_price_ngn=kwargs.pop("quoted_price_ngn", offer["price_ngn"]),
        provider=provider,
        sleep=lambda _: None,
        **kwargs,
    )


# ------------------------------------------------------- the core guarantee --

def test_same_request_twice_books_once(clean_ledger):
    """Two identical requests: one carrier call, one ledger row.

    The double-clicked Book button. No idempotency key is supplied, so this is
    also the test that `derive_key` collapses identical requests onto one key.
    """
    offer = make_offer()
    provider = StubProvider(confirmed())

    first = book(offer, provider)
    second = book(offer, provider)

    assert provider.calls == 1, "the carrier was asked twice"
    assert len(ledger.for_offer(offer["offer_id"])) == 1

    assert first.replayed is False
    assert second.replayed is True
    assert second.booking.idempotency_key == first.booking.idempotency_key
    assert second.booking.provider_reference == first.booking.provider_reference
    # 201 created, then 200 - the second request changed nothing.
    assert (first.status_code, second.status_code) == (201, 200)


def test_concurrent_identical_requests_book_once(clean_ledger):
    """Two requests racing on one key. The unique constraint decides.

    Sequential double-submits would pass even if `claim` were a SELECT followed
    by an INSERT. This is the test that distinguishes the two: both threads are
    held at a barrier and released together, so they overlap inside `claim`.
    """
    offer = make_offer(offer_id="race-road-LOS-ABV-20260914T0600Z")
    provider = StubProvider(confirmed())
    barrier = threading.Barrier(2)

    def attempt():
        barrier.wait(timeout=5)
        return book(offer, provider)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = [f.result() for f in [pool.submit(attempt), pool.submit(attempt)]]

    assert provider.calls == 1, "both threads reached the carrier"
    assert len(ledger.for_offer(offer["offer_id"])) == 1
    # Exactly one winner: one request created the booking, one replayed it.
    assert sorted(o.replayed for o in outcomes) == [False, True]


def test_a_new_price_is_a_new_request(clean_ledger):
    """Re-booking the same trip at a different price is allowed through.

    The quoted price is part of the derived key on purpose: a traveller who has
    seen and accepted a new number is making a genuinely different request, and
    collapsing it onto the old key would silently refuse a legitimate booking.
    """
    offer = make_offer()
    provider = StubProvider(confirmed("ORD-1"), confirmed("ORD-2"))

    first = book(offer, provider, quoted_price_ngn=25_000.0)
    second = book(offer, provider, quoted_price_ngn=27_000.0)

    assert provider.calls == 2
    assert first.booking.idempotency_key != second.booking.idempotency_key
    assert len(ledger.for_offer(offer["offer_id"])) == 2


# ------------------------------------------------- what must never be retried --

def test_a_timeout_is_never_retried(clean_ledger):
    """The case the whole design exists for.

    A timeout means the request WAS sent and the answer was lost, so the carrier
    may hold a seat. Asking again could produce a second ticket. One call, state
    `unknown`, and the traveller is told plainly that we do not know.
    """
    offer = make_offer()
    provider = StubProvider(ProviderTimeout("no response in 30s"))

    outcome = book(offer, provider)

    assert provider.calls == 1, "a timeout was retried - this can duplicate a ticket"
    assert outcome.booking.state == "unknown"
    assert "not tried again" in outcome.detail.lower()


def test_replaying_an_unknown_never_calls_the_carrier(clean_ledger):
    """`unknown` is terminal. A repeat request must not resolve it by guessing."""
    offer = make_offer()
    provider = StubProvider(ProviderTimeout("no response"))

    book(offer, provider)
    assert provider.calls == 1

    replay = book(offer, provider)

    assert provider.calls == 1, "a replayed `unknown` reached the carrier"
    assert replay.replayed is True
    assert replay.booking.state == "unknown"
    assert len(ledger.for_offer(offer["offer_id"])) == 1


def test_unreachable_is_retried_because_nothing_was_sent(clean_ledger):
    """The mirror image: a refused connection created nothing, so retry is free.

    Retrying here is not a risk being taken, it is the absence of one - and
    getting this wrong in the safe direction (never retrying anything) would
    fail bookings that a second attempt would have completed.
    """
    offer = make_offer()
    provider = StubProvider(ProviderUnreachable("connection refused"), confirmed())

    outcome = book(offer, provider)

    assert provider.calls == 2
    assert outcome.booking.state == "confirmed"
    assert outcome.booking.attempts == 2


def test_a_definite_no_stops_immediately(clean_ledger):
    offer = make_offer()
    provider = StubProvider(ProviderRejected("offer expired"))

    outcome = book(offer, provider)

    assert provider.calls == 1
    assert outcome.booking.state == "failed"
    # A failed booking is still a created record, so 201 - the state is the
    # answer, not the status code.
    assert outcome.status_code == 201


def test_running_out_of_retries_lands_on_unknown_if_any_could_have_booked(
    clean_ledger,
):
    """Exhaustion is resolved by what the failures MEANT, not by a counter.

    `ProviderUnavailable` is ambiguous - the carrier answered, so it may have
    acted. Three of those in a row must not be reported as a clean failure.
    """
    from booking.providers import ProviderUnavailable

    offer = make_offer()
    provider = StubProvider(ProviderUnavailable("503"))

    outcome = book(offer, provider, max_attempts=3)

    assert provider.calls == 3
    assert outcome.booking.state == "unknown"


# --------------------------------------------------------- the price check --

def test_a_risen_price_is_refused_and_recorded(clean_ledger):
    """The traveller agreed to a number. We do not book them onto a bigger one.

    And the refusal is WRITTEN DOWN - the claim happens before the price check
    precisely so that "we declined to book this" is a record rather than a
    forgotten local decision.
    """
    offer = make_offer(price_ngn=30_000.0)
    provider = StubProvider(confirmed())

    outcome = book(offer, provider, quoted_price_ngn=25_000.0)

    assert provider.calls == 0, "the carrier was called despite the price moving"
    assert outcome.price_moved is True
    assert outcome.status_code == 409
    assert outcome.booking.state == "failed"
    assert "price_changed" in outcome.booking.last_error

    rows = ledger.for_offer(offer["offer_id"])
    assert len(rows) == 1, "the refusal was not recorded"


def test_a_fallen_price_books_at_the_lower_one(clean_ledger):
    """A price DROP never blocks - nobody objects to paying less."""
    offer = make_offer(price_ngn=20_000.0)
    provider = StubProvider(confirmed())

    outcome = book(offer, provider, quoted_price_ngn=25_000.0)

    assert provider.calls == 1
    assert outcome.booking.state == "confirmed"
    assert outcome.booking.price_ngn_at_book == 20_000.0


# --------------------------------------------------------------- the ledger --

def test_claim_is_written_before_the_carrier_is_called(clean_ledger):
    """The ordering the entire safety argument rests on.

    Asserted from inside the provider: at the moment the carrier is called, the
    row must already exist. A refactor that moved the claim after the call would
    leave every other test in this file passing.
    """
    offer = make_offer()
    seen: list[str] = []

    class Observer:
        name = "observer"

        def create(self, offer, passenger, idempotency_key):
            row = ledger.get(idempotency_key)
            seen.append("row exists" if row else "NO ROW")
            return confirmed()

    book(offer, Observer())

    assert seen == ["row exists"], "the carrier was called before the ledger claim"


def test_update_rejects_columns_outside_the_whitelist(clean_ledger):
    """The whitelist is a SQL-injection guard: `update` interpolates column
    names, because a placeholder cannot stand in for one."""
    ledger.claim("wl-1", "offer-1", "GIGM", 1000.0)

    # A column name carrying its own SQL. Rejected by the whitelist before it
    # can reach the f-string that builds the SET clause.
    with pytest.raises(ValueError, match="not updatable"):
        ledger.update("wl-1", **{"state = 'confirmed' WHERE true; --": "x"})

    # A real column that is nonetheless not the caller's to write. `created_at`
    # is the record of when we first claimed the key, and a booking that can
    # backdate its own claim is a booking whose ordering cannot be trusted.
    with pytest.raises(ValueError, match="not updatable"):
        ledger.update("wl-1", created_at="2020-01-01")


def test_update_rejects_an_invalid_state(clean_ledger):
    ledger.claim("st-1", "offer-1", "GIGM", 1000.0)

    with pytest.raises(ValueError, match="invalid state"):
        ledger.update("st-1", state="probably-fine")


# ------------------------------------------------------- over real HTTP -----

def test_double_clicking_book_over_http_creates_one_row(
    clean_ledger, store, fake_redis, monkeypatch
):
    """The guarantee as a user can actually reach it: two HTTP POSTs.

    Everything above calls `orchestrator.book` directly. This goes through the
    real request path - body parsing, the offer lookup in Redis, the middleware
    - because that is where a regression would actually land, and because a
    booking that never parses is a booking that never happens.
    """
    from fastapi.testclient import TestClient

    from api import main
    from tests.conftest import put

    offer = make_offer(offer_id="gigm-road-LOS-ABV-http")
    put(fake_redis, offer)
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main.limiter, "enabled", False)
    client = TestClient(main.app)

    payload = {
        "offer_id": offer["offer_id"],
        "quoted_price_ngn": offer["price_ngn"],
        "passenger": {
            "given_name": "Ada", "family_name": "Okoro",
            "email": "ada@example.test",
        },
    }

    first = client.post("/book", json=payload)
    second = client.post("/book", json=payload)

    assert first.status_code == 201, first.json()
    assert second.status_code == 200, "the replay was not recognised over HTTP"
    assert second.json()["replayed"] is True
    assert first.json()["idempotency_key"] == second.json()["idempotency_key"]
    assert len(ledger.for_offer(offer["offer_id"])) == 1

    # No token is configured in tests, so a road offer hands over a deep link -
    # and says so rather than claiming a seat is held.
    assert first.json()["state"] == "pending"
    assert first.json()["handoff_url"]
