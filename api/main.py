"""The public API.

    GET  /search?origin=LOS&dest=ABV&date=2026-09-14
    GET  /search/nl?q=cheapest+way+to+Abuja+next+week
    GET  /offer/{offer_id}
    GET  /predict?offer_id=...
    POST /book
    GET  /booking/{idempotency_key}
    POST /booking/{idempotency_key}/reconcile
    GET  /health

Run it with:
    uvicorn api.main:app --reload --port 8000
"""

from __future__ import annotations

import re
import time
from datetime import date as date_type
from datetime import datetime, timezone
from typing import Any

import psycopg
import redis
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from common import logs
from common.config import STALE_AFTER_SECONDS, assert_sandbox_only
from common.normalise import to_utc_instant
from common.places import PLACES, ROUTES, is_supported_route
from common.ranking import (
    annotate,
    local_departure_date,
    rank_cheapest,
    rank_fastest,
)
from common.schema import OfferContractError, validate_offer
from api.ratelimit import client_key, limiter
from api.store import OfferStore

# Refuse to start against a live booking API. Payment processing is out of
# scope, so a live Duffel token is a configuration error, not an option.
assert_sandbox_only()

# Configured here rather than in each module, because this import runs once per
# uvicorn worker and covers everything those workers go on to import.
logs.configure()
log = logs.get(__name__)

app = FastAPI(
    title="Multi-Modal Ticketing & Dynamic Pricing Aggregator",
    description=(
        "Cheapest and fastest travel options across Nigerian road and air "
        "carriers. Prices are served from Redis, which holds the latest "
        "observation of every departure. Bookings go through the Duffel "
        "sandbox where possible and a pre-filled carrier link otherwise. No "
        "payment is ever taken."
    ),
    version="0.5.0",
)

store = OfferStore()

# Results per list. Enough to be useful, small enough to stay readable.
DEFAULT_LIMIT = 10

# --------------------------------------------------------- cross-cutting ----

@app.middleware("http")
async def _limit_and_log(request: Request, call_next):
    """Rate limit, then serve, then log one line with how long it took.

    Both concerns live in one middleware because the order matters and is
    easier to see written down once: a rejected request must not reach the
    handler, and a rejected request must still be logged.

    The duration is the useful field. `/search` has a 100 ms target that the
    route cache exists to meet (see api/store.for_route), and a target nobody
    measures in production is a target nobody knows they have missed.
    """
    started = time.perf_counter()
    path = request.url.path

    exceeded = limiter.check(request.method, path, client_key(request))
    if exceeded is not None:
        log.warning(
            "http.rate_limited", extra={"path": path, "limit": str(exceeded)}
        )
        response: Response = JSONResponse(
            status_code=429,
            content={
                "detail": (
                    f"Too many requests to {path}. This endpoint allows "
                    f"{exceeded}. Wait a moment and try again."
                )
            },
        )
    else:
        response = await call_next(request)

    log.info(
        "http.request",
        extra={
            "method": request.method,
            "path": path,
            "status": response.status_code,
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        },
    )
    return response


def _redis_down(exc: redis.RedisError) -> HTTPException:
    """Redis is the price store, so without it there are no prices to serve.

    A 503 saying so, rather than the bare 500 an uncaught RedisError would
    produce. This mirrors how POST /book reports an unreachable ledger: name
    the dependency, say what was and was not done, and do not dress an outage
    up as an empty result - "no departures found" would send a traveller away
    believing nothing runs that day.
    """
    log.error("redis.unavailable", exc_info=exc)
    return HTTPException(
        status_code=503,
        detail=(
            "The price store is unreachable, so no prices can be served right "
            f"now. This is an outage on our side, not an empty result. ({exc})"
        ),
    )


@app.get("/health")
def health() -> dict[str, Any]:
    """Liveness plus the one number that says whether the pipeline is feeding us.

    `offers_cached` is read from a counter maintained by the store, NOT by
    scanning the keyspace. Health is polled on an interval - by Caddy, by a
    monitor, by whoever is watching a deploy - and an O(keyspace) SCAN on every
    poll is a self-inflicted load spike on the datastore the endpoint exists to
    reassure you about.
    """
    redis_ok = store.ping()
    counted = store.count_cached() if redis_ok else None
    return {
        "status": "ok" if redis_ok else "degraded",
        "redis": "up" if redis_ok else "down",
        "offers_cached": counted["count"] if counted else 0,
        # When the number was last actually measured. Without this the count is
        # unfalsifiable - you cannot tell a real 2,280 from a stale one.
        "offers_counted_at": counted["measured_at"] if counted else None,
    }


@app.get("/search")
def search(
    origin: str = Query(..., min_length=3, max_length=3, description="e.g. LOS"),
    dest: str = Query(..., min_length=3, max_length=3, description="e.g. ABV"),
    date: date_type | None = Query(None, description="Departure date, YYYY-MM-DD"),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=50),
) -> dict[str, Any]:
    """Cheapest and fastest options for a route on a date.

    Returns 400 for a route we do not cover, and 200 with empty lists when we
    cover the route but have nothing for that date. The distinction matters: a
    user should be able to tell "we don't do that journey" apart from "nothing
    is running that day".
    """
    origin, dest = origin.upper(), dest.upper()

    for code in (origin, dest):
        if code not in PLACES:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown place code {code!r}. Known: {sorted(PLACES)}",
            )
    if not is_supported_route(origin, dest):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Route {origin}->{dest} is out of scope. This project covers "
                f"{', '.join(f'{a} to {b}' for a, b in ROUTES)}, in both directions."
            ),
        )

    now = datetime.now(timezone.utc)
    try:
        offers = store.for_route(origin, dest)
    except redis.RedisError as exc:
        raise _redis_down(exc) from exc

    wanted = date.isoformat() if date else None
    if wanted:
        offers = [o for o in offers if local_departure_date(o) == wanted]

    # Never offer a seat on a service that has already left. Compared as
    # instants, not as text: "17:00+01:00" sorts after "16:30+00:00" as a
    # string while being an hour EARLIER in real time, so a string comparison
    # would keep offering departures that had already gone.
    offers = [o for o in offers if to_utc_instant(o["depart_time"]) > now]

    enriched = [annotate(o, STALE_AFTER_SECONDS, now) for o in offers]

    stale_count = sum(1 for o in enriched if o["stale"])
    return {
        "origin": origin,
        "destination": dest,
        "date": wanted,
        "count": len(enriched),
        # `stale` is true when EVERY result is stale, which is what a dead
        # connector looks like. `stale_count` exposes the partial case.
        "stale": bool(enriched) and stale_count == len(enriched),
        "stale_count": stale_count,
        "searched_at": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "cheapest": rank_cheapest(enriched, limit),
        "fastest": rank_fastest(enriched, limit),
    }


@app.get("/search/nl")
def search_natural_language(
    q: str = Query(..., min_length=2, max_length=300,
                   description='e.g. "cheapest way to Abuja next Friday"'),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=50),
) -> dict[str, Any]:
    """Plain-English search: free text in, the same results /search returns.

    The parsed interpretation is echoed back so the UI can show what it
    understood ("Lagos to Abuja on 18 September, cheapest first"). A silent
    misreading is worse than a visible one - the user can correct what they
    can see.

    Rate limited in middleware, because this path can reach the Anthropic API
    and therefore spends money - see api/ratelimit.py.
    """
    from api import nl_search

    parsed = nl_search.parse_query(q)
    problems = nl_search.validate(parsed)
    if problems:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "Could not turn that into a search.",
                "problems": problems,
                "understood": parsed.query.model_dump(),
                "hint": 'Try naming a place, e.g. "cheapest to Abuja on Friday".',
            },
        )

    results = search(
        origin=parsed.query.origin,
        dest=parsed.query.destination,
        date=date_type.fromisoformat(parsed.query.date) if parsed.query.date else None,
        limit=limit,
    )
    return {
        "asked": q,
        "understood": parsed.query.model_dump(),
        "interpreted_by": parsed.strategy,
        "note": parsed.note,
        # Which list to show first. The ranked lists are both returned either
        # way, so the UI can offer the other tab without a second request.
        "show": parsed.query.preference,
        **results,
    }


@app.get("/predict")
def predict(offer_id: str = Query(..., description="e.g. gigm-road-LOS-ABV-20260914T0600Z")
            ) -> dict[str, Any]:
    """Will this offer's price rise in the next 24 hours?

    Returns `will_rise` and a `probability` between 0 and 1. The probability is
    the useful part - "70% likely to rise" tells a traveller far more than a
    bare yes, particularly near the 0.5 boundary where the model is least sure.
    """
    try:
        from ml import predictor
    except ImportError as exc:
        # The ML packages are a separate, much larger install. Everything else
        # in this API works without them, so a missing one is a 503 explaining
        # what to install - not a 500 stack trace.
        raise HTTPException(
            status_code=503,
            detail=(
                "Price prediction needs the machine-learning packages, which "
                "are not installed. Run: pip install -r requirements-ml.txt "
                f"({exc})"
            ),
        ) from exc

    try:
        return predictor.predict(offer_id)
    except predictor.ModelNotTrained as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except KeyError:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No price history for {offer_id!r}, so there is nothing to "
                "predict from. Either the offer does not exist or it has not "
                "been observed yet."
            ),
        ) from None


# --------------------------------------------------------------- booking ----

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class PassengerIn(BaseModel):
    """Who is travelling.

    `extra="forbid"` is a scope guard, not a nicety: a request carrying a card
    number is rejected with a 422 rather than silently ignored, so this API can
    never be mistaken for one that takes payments.
    """

    model_config = ConfigDict(extra="forbid")

    given_name: str = Field(min_length=1, max_length=60)
    family_name: str = Field(min_length=1, max_length=60)
    email: str = Field(min_length=3, max_length=200)
    # Airlines need these; coach operators do not, so they stay optional.
    born_on: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    phone: str | None = Field(default=None, max_length=20)
    title: str | None = Field(default=None, max_length=10)
    gender: str | None = Field(default=None, pattern="^[mf]$")

    @field_validator("email")
    @classmethod
    def _looks_like_an_address(cls, value: str) -> str:
        if not _EMAIL.match(value.strip()):
            raise ValueError("not an email address")
        return value.strip()


class BookingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    offer_id: str = Field(min_length=5, max_length=120)
    # What the traveller agreed to pay. Checked against the live price before
    # anything is booked, so it is the consent record, not a display value.
    quoted_price_ngn: float = Field(gt=0)
    passenger: PassengerIn
    # Optional. Left out, one is derived from the request contents, which is
    # what makes a double-clicked Book button harmless.
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=120)


def _require_offer(offer_id: str) -> dict[str, Any]:
    try:
        offer = store.get(offer_id)
    except redis.RedisError as exc:
        raise _redis_down(exc) from exc
    if offer is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No current offer {offer_id!r}. Either it never existed or the "
                "departure is no longer being sold. Search again to get a live "
                "offer id."
            ),
        )
    return offer


@app.post("/book")
def create_booking(request: BookingRequest, response: Response) -> dict[str, Any]:
    """Book an offer, at most once, whatever goes wrong.

    Submitting the same request twice returns the FIRST result and books
    nothing further. The booking's `state` is the real answer:

        confirmed  a carrier gave us a reference
        pending    the traveller was handed a pre-filled carrier checkout,
                   which they must complete themselves
        failed     a definite no, or we refused (the fare moved)
        unknown    we asked and never heard back, so a ticket may exist. Never
                   retried automatically - that is what could duplicate it.

    Rate limited in middleware, because every call writes a ledger row before
    it does anything else - see api/ratelimit.py.
    """
    from booking import orchestrator
    from booking.providers import Passenger

    offer = _require_offer(request.offer_id)

    try:
        outcome = orchestrator.book(
            offer=offer,
            passenger=Passenger(**request.passenger.model_dump()),
            quoted_price_ngn=request.quoted_price_ngn,
            idempotency_key=request.idempotency_key,
        )
    except psycopg.OperationalError as exc:
        # The ledger is the safety mechanism. Without it we cannot promise not
        # to double-book, so we decline to try rather than risk it.
        raise HTTPException(
            status_code=503,
            detail=(
                "The booking ledger is unreachable, so no booking was attempted. "
                "Bookings are deliberately refused rather than risked when we "
                f"cannot record them first. ({exc})"
            ),
        ) from exc

    response.status_code = outcome.status_code
    return outcome.as_dict()


@app.get("/booking/{idempotency_key}")
def get_booking(idempotency_key: str) -> dict[str, Any]:
    from booking import ledger

    booking = ledger.get(idempotency_key)
    if booking is None:
        raise HTTPException(status_code=404, detail=f"No booking {idempotency_key!r}")
    return booking.as_dict()


@app.post("/booking/{idempotency_key}/reconcile")
def reconcile_booking(idempotency_key: str) -> dict[str, Any]:
    """Ask the carrier what really happened to an `unknown` booking.

    This is what stops `unknown` being a dead end. It only works for bookings
    made through Duffel, because every order we create there carries this key in
    its metadata; a deep-link handoff has no carrier record to consult.
    """
    from booking import orchestrator
    from booking.providers import ProviderError

    try:
        return orchestrator.reconcile(idempotency_key).as_dict()
    except KeyError:
        raise HTTPException(
            status_code=404, detail=f"No booking {idempotency_key!r}"
        ) from None
    except ProviderError as exc:
        raise HTTPException(
            status_code=502,
            detail=(
                f"Could not ask the carrier what happened, so the booking stays "
                f"'unknown'. ({exc})"
            ),
        ) from exc


@app.get("/book/options")
def booking_options(offer_id: str = Query(..., description="e.g. gigm-road-LOS-ABV-...")
                    ) -> dict[str, Any]:
    """How this offer would be booked, without booking it.

    Lets the UI label the button honestly - "Book" when a real reservation is
    possible, "Continue on carrier site" when it is a handoff.
    """
    from booking import orchestrator

    return orchestrator.describe_routing(_require_offer(offer_id))


@app.get("/offer/{offer_id}")
def get_offer(offer_id: str) -> dict[str, Any]:
    try:
        offer = store.get(offer_id)
    except redis.RedisError as exc:
        raise _redis_down(exc) from exc
    if offer is None:
        raise HTTPException(status_code=404, detail=f"No offer {offer_id!r}")

    now = datetime.now(timezone.utc)
    enriched = annotate(offer, STALE_AFTER_SECONDS, now)

    # The store should only ever hold canonical offers. If one is not, that is
    # a stream-layer fault worth surfacing rather than quietly serving.
    try:
        validate_offer(offer)
        enriched["contract_valid"] = True
    except OfferContractError as exc:
        enriched["contract_valid"] = False
        enriched["contract_error"] = str(exc)

    return enriched
