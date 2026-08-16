"""The booking idempotency ledger.

One row per booking attempt, in PostgreSQL, written BEFORE any carrier API is
called. That ordering is the entire safety mechanism, so it is worth stating
plainly why:

    Call the carrier first and the response is lost -> nothing on our side
    records that the call happened -> the retry books a second ticket.

    Write first and the response is lost -> we have a row saying "we asked, we
    never heard back" -> the retry sees that row and refuses to ask again.

The worst case therefore becomes a booking we cannot describe (`unknown`),
never a booking we made twice. A confused record is recoverable; a duplicate
ticket someone paid for is not.

The four states:

    pending    we handed the traveller to the carrier's checkout (deep link),
               or the attempt is in flight. No ticket exists yet.
    confirmed  a carrier gave us a booking reference.
    failed     a carrier gave us a definite no, or we refused to proceed
               (the price moved, the departure vanished).
    unknown    we asked and never learned the answer. A ticket may or may not
               exist. This state exists so that ignorance is recorded rather
               than guessed at.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from common import logs
from common.db import connection

log = logs.get(__name__)

STATES = ("pending", "confirmed", "failed", "unknown")

# States from which no further carrier call may ever be made. `pending` is not
# here: a deep-link handoff sits in `pending` and may legitimately be replayed
# (showing the traveller the same link again books nothing).
TERMINAL_STATES = ("confirmed", "failed", "unknown")

_COLUMNS = (
    "idempotency_key", "offer_id", "carrier", "state", "price_ngn_quoted",
    "price_ngn_at_book", "provider", "provider_reference", "attempts",
    "last_error", "created_at", "updated_at",
)
_RETURNING = ", ".join(_COLUMNS)

# Only these may be written by update(). A whitelist, because the column name
# is interpolated into SQL and a parameter placeholder cannot stand in for one.
_UPDATABLE = frozenset({
    "state", "price_ngn_at_book", "provider", "provider_reference",
    "attempts", "last_error",
})


@dataclass(frozen=True)
class Booking:
    idempotency_key: str
    offer_id: str
    carrier: str
    state: str
    price_ngn_quoted: float | None
    price_ngn_at_book: float | None
    provider: str | None
    provider_reference: str | None
    attempts: int
    last_error: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Booking":
        return cls(
            idempotency_key=row["idempotency_key"],
            offer_id=row["offer_id"],
            carrier=row["carrier"],
            state=row["state"],
            price_ngn_quoted=_as_float(row["price_ngn_quoted"]),
            price_ngn_at_book=_as_float(row["price_ngn_at_book"]),
            provider=row["provider"],
            provider_reference=row["provider_reference"],
            attempts=row["attempts"],
            last_error=row["last_error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def as_dict(self) -> dict[str, Any]:
        """API-shaped. Timestamps become strings; nothing else changes."""
        return {
            "idempotency_key": self.idempotency_key,
            "offer_id": self.offer_id,
            "carrier": self.carrier,
            "state": self.state,
            "price_ngn_quoted": self.price_ngn_quoted,
            "price_ngn_at_book": self.price_ngn_at_book,
            "provider": self.provider,
            "provider_reference": self.provider_reference,
            "attempts": self.attempts,
            "last_error": self.last_error,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


def _as_float(value: Any) -> float | None:
    # NUMERIC comes back as Decimal, which is not JSON-serialisable.
    return None if value is None else float(value)


def derive_key(offer_id: str, passenger_email: str, quoted_price_ngn: float) -> str:
    """An idempotency key derived from what the request actually asks for.

    A caller may always supply its own key. When it does not, we build one from
    the request contents so that an accidental double-submit - a double-clicked
    Book button, a retried HTTP request - collapses onto the same key and can
    only ever produce one booking.

    The quoted price is part of the key on purpose. Re-submitting the same trip
    at a NEW price is a genuinely different request (the traveller has seen and
    accepted a different number), so it gets its own key and is allowed through.
    """
    material = f"{offer_id}|{passenger_email.strip().lower()}|{quoted_price_ngn:.2f}"
    return "auto-v1-" + hashlib.sha256(material.encode()).hexdigest()[:16]


def claim(
    idempotency_key: str,
    offer_id: str,
    carrier: str,
    price_ngn_quoted: float,
) -> tuple[Booking, bool]:
    """Reserve the key. Returns (booking, is_new).

    A single atomic statement, which is what makes concurrent double-submits
    safe: two requests racing on the same key cannot both insert, so exactly
    one gets is_new=True and is allowed to call the carrier. The loser reads
    back the winner's row.
    """
    with connection() as conn:
        row = conn.execute(
            f"""
            INSERT INTO bookings
                (idempotency_key, offer_id, carrier, state, price_ngn_quoted)
            VALUES (%s, %s, %s, 'pending', %s)
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING {_RETURNING}
            """,
            (idempotency_key, offer_id, carrier, price_ngn_quoted),
        ).fetchone()

        if row is not None:
            log.info(
                "booking.claimed",
                extra={
                    "idempotency_key": idempotency_key,
                    "offer_id": offer_id,
                    "carrier": carrier,
                },
            )
            return Booking.from_row(row), True

        # DO NOTHING returned nothing, so the key was already taken. That is a
        # replay, not an error.
        existing = conn.execute(
            f"SELECT {_RETURNING} FROM bookings WHERE idempotency_key = %s",
            (idempotency_key,),
        ).fetchone()

    # Worth logging at INFO, not DEBUG: this line IS the double-booking
    # protection doing its job, and its absence during an incident is as
    # informative as its presence.
    log.info(
        "booking.replayed",
        extra={
            "idempotency_key": idempotency_key,
            "offer_id": offer_id,
            "state": existing["state"],
        },
    )
    return Booking.from_row(existing), False


def update(idempotency_key: str, **fields: Any) -> Booking:
    """Write outcome fields onto an existing booking."""
    unknown = set(fields) - _UPDATABLE
    if unknown:
        raise ValueError(f"not updatable: {sorted(unknown)}")
    if "state" in fields and fields["state"] not in STATES:
        raise ValueError(f"invalid state {fields['state']!r}; expected one of {STATES}")

    assignments = ", ".join(f"{name} = %s" for name in fields)
    with connection() as conn:
        row = conn.execute(
            f"""
            UPDATE bookings SET {assignments}, updated_at = now()
            WHERE idempotency_key = %s
            RETURNING {_RETURNING}
            """,
            (*fields.values(), idempotency_key),
        ).fetchone()
    if row is None:
        raise KeyError(f"no booking {idempotency_key!r}")

    if "state" in fields:
        # A state change is the whole story of a booking. Logged with the error
        # so an `unknown` can be explained without opening the database.
        log.info(
            "booking.state_changed",
            extra={
                "idempotency_key": idempotency_key,
                "state": fields["state"],
                "provider": row["provider"],
                "attempts": row["attempts"],
                "last_error": row["last_error"],
            },
        )
    return Booking.from_row(row)


def get(idempotency_key: str) -> Booking | None:
    with connection() as conn:
        row = conn.execute(
            f"SELECT {_RETURNING} FROM bookings WHERE idempotency_key = %s",
            (idempotency_key,),
        ).fetchone()
    return Booking.from_row(row) if row else None


def for_offer(offer_id: str) -> list[Booking]:
    """Every booking attempt against one departure, oldest first.

    The double-submit test uses this: after two identical requests there must
    be exactly one row here.
    """
    with connection() as conn:
        rows = conn.execute(
            f"SELECT {_RETURNING} FROM bookings WHERE offer_id = %s "
            "ORDER BY created_at",
            (offer_id,),
        ).fetchall()
    return [Booking.from_row(row) for row in rows]


def counts_by_state() -> dict[str, int]:
    with connection() as conn:
        rows = conn.execute(
            "SELECT state, count(*) AS n FROM bookings GROUP BY state"
        ).fetchall()
    return {row["state"]: row["n"] for row in rows}
