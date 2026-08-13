"""The order of operations that makes double-booking impossible.

The whole phase comes down to doing five things in this order:

    1. Work out the idempotency key.
    2. Find the offer, and refuse early if it has gone.
    3. CLAIM THE KEY IN POSTGRESQL.            <- before any carrier is called
    4. Re-check the price against what was quoted.
    5. Call the carrier, retrying only what is provably safe to retry.

Step 3 is the one that matters. Everything before it is a local read that books
nothing, so it can fail freely. Everything after it is recorded, so however
badly it goes, we can always say what we attempted.

Steps 4 and 5 are in that order for a reason too. The price check is a local
read, so doing it after the claim costs nothing and means a refusal ("the fare
moved, we did not book") is written down rather than forgotten.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from booking import ledger
from booking.ledger import Booking
from booking.providers import (
    BookingResult,
    Passenger,
    Provider,
    ProviderError,
    choose_provider,
    provider_reason,
)
from common.config import (
    BOOKING_BACKOFF_BASE_SECONDS,
    BOOKING_MAX_ATTEMPTS,
    BOOKING_PRICE_TOLERANCE_PCT,
    STALE_AFTER_SECONDS,
)
from common.ranking import annotate


class OfferGone(Exception):
    """The departure is no longer being offered, so there is nothing to book.

    Raised before the ledger is touched: no attempt was made, so no attempt is
    recorded.
    """


@dataclass(frozen=True)
class Outcome:
    booking: Booking
    replayed: bool          # true when this request changed nothing
    detail: str             # one plain sentence for the traveller
    handoff_url: str | None
    price_moved: bool = False
    live_price_ngn: float | None = None
    priced_from_stale_data: bool = False

    @property
    def status_code(self) -> int:
        """HTTP status.

        The booking STATE is the outcome; the status code only says whether the
        caller must do something differently. A `failed` booking still returns
        201, because a record was created and its state is the answer. The one
        exception is a price move, which the caller genuinely must act on by
        re-quoting, so that gets a 409.
        """
        if self.price_moved:
            return 409
        return 200 if self.replayed else 201

    def as_dict(self) -> dict[str, Any]:
        body = self.booking.as_dict()
        body.update({
            "replayed": self.replayed,
            "detail": self.detail,
            "handoff_url": self.handoff_url,
            "live_price_ngn": self.live_price_ngn,
            "priced_from_stale_data": self.priced_from_stale_data,
        })
        return body


def book(
    offer: dict[str, Any],
    passenger: Passenger,
    quoted_price_ngn: float,
    idempotency_key: str | None = None,
    provider: Provider | None = None,
    sleep: Callable[[float], None] = time.sleep,
    max_attempts: int = BOOKING_MAX_ATTEMPTS,
) -> Outcome:
    """Book one offer, at most once, whatever goes wrong.

    `offer` is the live record from Redis, not the client's copy of it - the
    client's copy is only trusted for `quoted_price_ngn`, which is what they
    agreed to pay and therefore the thing we check against.
    """
    key = idempotency_key or ledger.derive_key(
        offer["offer_id"], passenger.email, quoted_price_ngn
    )

    # --- step 3: claim the key BEFORE anything can be booked ----------------
    booking, is_new = ledger.claim(
        idempotency_key=key,
        offer_id=offer["offer_id"],
        carrier=offer["carrier"],
        price_ngn_quoted=quoted_price_ngn,
    )

    if not is_new:
        return _replay(booking)

    # --- step 4: has the fare moved since they saw it? ----------------------
    enriched = annotate(offer, STALE_AFTER_SECONDS)
    live_price = float(offer["price_ngn"])
    allowed = quoted_price_ngn * (1 + BOOKING_PRICE_TOLERANCE_PCT / 100)

    if live_price > allowed:
        rise_pct = (live_price - quoted_price_ngn) / quoted_price_ngn * 100
        booking = ledger.update(
            key,
            state="failed",
            price_ngn_at_book=live_price,
            last_error=f"price_changed: quoted {quoted_price_ngn:.0f}, now {live_price:.0f}",
        )
        return Outcome(
            booking=booking,
            replayed=False,
            price_moved=True,
            live_price_ngn=live_price,
            priced_from_stale_data=enriched["stale"],
            handoff_url=None,
            detail=(
                f"The fare has risen {rise_pct:.1f}% since you saw it - from "
                f"NGN {quoted_price_ngn:,.0f} to NGN {live_price:,.0f}. Nothing "
                "was booked. Search again to accept the new price."
            ),
        )

    # --- step 5: call the carrier ------------------------------------------
    provider = provider or choose_provider(offer)
    outcome = _attempt(
        key, offer, passenger, provider, live_price, sleep, max_attempts
    )
    return Outcome(
        booking=outcome.booking,
        replayed=False,
        detail=outcome.detail,
        handoff_url=outcome.handoff_url,
        live_price_ngn=live_price,
        priced_from_stale_data=enriched["stale"],
    )


def _replay(booking: Booking) -> Outcome:
    """A key we have seen before. Return what happened; call nobody."""
    explanation = {
        "confirmed": (
            f"Already booked. Reference {booking.provider_reference}. This "
            "request was a duplicate, so nothing new was created."
        ),
        "pending": (
            "This booking was already started and is waiting to be completed "
            "on the carrier's own checkout. Nothing new was created."
        ),
        "failed": (
            f"This booking was already attempted and did not succeed: "
            f"{booking.last_error}. It was not retried."
        ),
        "unknown": (
            "This booking was already attempted and we never learned the "
            "outcome, so it was deliberately NOT retried - a second attempt "
            "could produce a second ticket. It needs reconciling with the "
            "carrier."
        ),
    }[booking.state]
    return Outcome(
        booking=booking,
        replayed=True,
        detail=explanation,
        handoff_url=None,
    )


@dataclass(frozen=True)
class _Attempted:
    booking: Booking
    detail: str
    handoff_url: str | None


def _attempt(
    key: str,
    offer: dict[str, Any],
    passenger: Passenger,
    provider: Provider,
    live_price: float,
    sleep: Callable[[float], None],
    max_attempts: int,
) -> _Attempted:
    """Up to `max_attempts` calls, with the wait doubling between them.

    Which failures get another go is decided by the exception, not by a counter:

        retryable and nothing was created   -> wait and try again
        not retryable, might have booked    -> stop now, state `unknown`
        not retryable, definitely no ticket -> stop now, state `failed`

    Running out of attempts lands on `unknown` if any of the failures could have
    booked something, and `failed` if none of them could.
    """
    last_error: ProviderError | None = None

    for attempt in range(1, max_attempts + 1):
        ledger.update(key, attempts=attempt)
        try:
            result: BookingResult = provider.create(offer, passenger, key)
        except ProviderError as exc:
            last_error = exc
            if not exc.retryable:
                break
            if attempt < max_attempts:
                # 0.5s, then 1.0s. Doubling gives a struggling carrier room to
                # recover instead of hammering it at a fixed interval.
                sleep(BOOKING_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
            continue
        else:
            return _succeed(key, result, live_price, attempt)

    assert last_error is not None  # the loop only exits here via an exception
    state = "unknown" if last_error.ambiguous else "failed"
    booking = ledger.update(
        key,
        state=state,
        last_error=f"{type(last_error).__name__}: {last_error}",
        price_ngn_at_book=live_price,
    )
    return _Attempted(
        booking=booking,
        handoff_url=None,
        detail=(
            _UNKNOWN_DETAIL if state == "unknown"
            else f"{offer['carrier']} could not complete this booking: {last_error}"
        ),
    )


_UNKNOWN_DETAIL = (
    "We asked the carrier to book this and never got an answer, so we do not "
    "know whether a seat was taken. We have deliberately NOT tried again, "
    "because a second attempt could produce a second ticket. This booking is "
    "marked 'unknown' and needs checking against the carrier."
)


def _succeed(
    key: str, result: BookingResult, live_price: float, attempt: int
) -> _Attempted:
    booking = ledger.update(
        key,
        state=result.state,
        provider=result.provider,
        provider_reference=result.reference,
        price_ngn_at_book=live_price,
        attempts=attempt,
        last_error=None,
    )
    return _Attempted(
        booking=booking, detail=result.detail, handoff_url=result.handoff_url
    )


# ------------------------------------------------------------- reconcile ----

def reconcile(idempotency_key: str) -> Outcome:
    """Resolve an `unknown` booking by asking the carrier what it actually did.

    Only Duffel can answer this, and only because every order we create carries
    our idempotency key in its metadata. A deep-link booking has no carrier
    record to consult, so it stays `pending` for ever - we hand the traveller
    over and never find out what they did, and pretending otherwise would be
    inventing information.
    """
    booking = ledger.get(idempotency_key)
    if booking is None:
        raise KeyError(idempotency_key)

    if booking.state != "unknown":
        return _replay(booking)

    from booking.duffel import DuffelSandbox

    reference = DuffelSandbox().find_by_idempotency_key(idempotency_key)
    if reference:
        resolved = ledger.update(
            idempotency_key,
            state="confirmed",
            provider="duffel_sandbox",
            provider_reference=reference,
            last_error=None,
        )
        detail = (
            f"Resolved: the carrier did book it. Reference {reference}. No "
            "second booking was ever made."
        )
    else:
        resolved = ledger.update(
            idempotency_key,
            state="failed",
            last_error="reconciled: no order carrying this key exists at the carrier",
        )
        detail = (
            "Resolved: the carrier has no booking carrying this request's key, "
            "so nothing was booked and no seat is held."
        )

    return Outcome(
        booking=resolved, replayed=False, detail=detail, handoff_url=None
    )


def describe_routing(offer: dict[str, Any]) -> dict[str, Any]:
    """How this offer would book, without booking it. Used by GET /book/options."""
    provider = choose_provider(offer)
    return {
        "offer_id": offer["offer_id"],
        "carrier": offer["carrier"],
        "provider": provider.name,
        "books_via_api": provider.name != "deeplink",
        "reason": provider_reason(offer),
    }
