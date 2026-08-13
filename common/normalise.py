"""Transformation functions applied at ingestion.

Everything here is pure and unit-tested. These are the functions that turn each
carrier's idiosyncratic output into the canonical shape: timestamps become
offset-aware, prices become NGN, place codes become the registry codes.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

from common.places import get_place

# --------------------------------------------------------------- timestamps --


@lru_cache(maxsize=4096)
def _parse_iso(raw: str) -> datetime:
    """Parse an ISO timestamp, remembering recent answers.

    Worth caching because of how the data actually looks: every offer published
    in one pipeline sweep carries the SAME `fetched_at`, so a search over 420
    offers was parsing one identical string 420 times. Load testing put this on
    the critical path of the widest search.

    Safe to share results because datetimes are immutable, and the function is
    pure - the same text always means the same instant.
    """
    # Python's fromisoformat handles 'Z' from 3.11 onward, but be explicit.
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def to_utc_instant(value: str | datetime) -> datetime:
    """Parse any accepted timestamp into a tz-aware UTC datetime.

    A naive timestamp (no offset) is REJECTED rather than silently assumed to
    be Lagos time. Silently assuming a timezone is how a Lagos-London fare ends
    up an hour wrong and nobody notices until the demo.
    """
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            raise ValueError("empty timestamp")
        parsed = _parse_iso(raw)
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise TypeError(f"unsupported timestamp type: {type(value).__name__}")

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(
            f"naive timestamp {value!r} - every timestamp must carry an explicit "
            "UTC offset. Use localise_departure() if you have a wall-clock time "
            "and know which place it belongs to."
        )
    return parsed.astimezone(timezone.utc)


def localise_departure(wall_clock: str | datetime, place_code: str) -> datetime:
    """Attach the correct offset to a carrier's wall-clock departure time.

    Bus operators publish '07:00' with no timezone because to them it is
    obvious. This attaches Africa/Lagos (or Europe/London for LHR) properly,
    including any daylight-saving rules, which matters for the London route:
    the UK shifts by an hour twice a year and Nigeria never does.
    """
    if isinstance(wall_clock, str):
        parsed = datetime.fromisoformat(wall_clock.strip())
    else:
        parsed = wall_clock

    if parsed.tzinfo is not None:
        raise ValueError(
            f"{wall_clock!r} already carries an offset; use to_utc_instant() instead"
        )

    tz = ZoneInfo(get_place(place_code).timezone)
    return parsed.replace(tzinfo=tz)


def format_departure(dt: datetime, place_code: str) -> str:
    """Render depart_time for the canonical message.

    Kept in the ORIGIN's local time with an explicit offset, matching the
    canonical example in the spec. A traveller reading '07:00+01:00' sees the
    time they must arrive at the terminal; the offset keeps the instant exact.
    """
    tz = ZoneInfo(get_place(place_code).timezone)
    return dt.astimezone(tz).isoformat(timespec="seconds")


def format_fetched_at(dt: datetime) -> str:
    """Render fetched_at in UTC with a Z suffix, matching the canonical example."""
    return (
        to_utc_instant(dt)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


# ------------------------------------------------------------------ prices --

_PRICE_CLEAN_RE = re.compile(r"[^\d.]")


def parse_price(raw: str | int | float) -> float:
    """Turn a scraped price string into a number.

    Carrier sites write prices every possible way: 'NGN 42,500', '₦42,500.00',
    '42 500', '£310.50'. This strips everything that is not a digit or a decimal
    point. It deliberately does NOT infer the currency - that is the caller's
    job, because guessing currency from a symbol that failed to render is how
    a £310 fare becomes ₦310.
    """
    if isinstance(raw, (int, float)):
        value = float(raw)
    else:
        cleaned = _PRICE_CLEAN_RE.sub("", str(raw))
        if cleaned.count(".") > 1:  # e.g. '1.234.567' - dots used as separators
            cleaned = cleaned.replace(".", "")
        if not cleaned:
            raise ValueError(f"no numeric content in price {raw!r}")
        value = float(cleaned)

    if value <= 0:
        raise ValueError(f"non-positive price parsed from {raw!r}: {value}")
    return value


def to_ngn(amount: float, currency: str, gbp_ngn_rate: float) -> float:
    """Convert a fare to Naira. Rounded to whole Naira - kobo are not quoted."""
    if amount <= 0:
        raise ValueError(f"non-positive amount: {amount}")

    code = currency.strip().upper()
    if code == "NGN":
        converted = amount
    elif code == "GBP":
        if gbp_ngn_rate <= 0:
            raise ValueError(f"invalid GBP->NGN rate: {gbp_ngn_rate}")
        converted = amount * gbp_ngn_rate
    else:
        raise ValueError(
            f"unsupported currency {currency!r}. This project quotes NGN and "
            "converts GBP for the London route; anything else means a connector "
            "is reading the wrong field."
        )
    return round(converted, 2)


# --------------------------------------------------------------- offer_id ---


def carrier_key_for(carrier: str) -> str:
    """Resolve a display name ('Air Peace') or key ('airpeace') to the key."""
    from common.carriers import CARRIERS_BY_KEY, CARRIERS_BY_NAME

    text = carrier.strip()
    if text in CARRIERS_BY_NAME:
        return CARRIERS_BY_NAME[text].key
    lowered = text.lower()
    if lowered in CARRIERS_BY_KEY:
        return lowered
    raise KeyError(
        f"unknown carrier {carrier!r}; expected one of "
        f"{sorted(CARRIERS_BY_NAME)} or {sorted(CARRIERS_BY_KEY)}"
    )


def canonical_offer_id(
    carrier: str,
    mode: str,
    origin: str,
    destination: str,
    depart_time: str | datetime,
) -> str:
    """Deterministic ID for one physical departure.

    Format: ``<carrier_key>-<mode>-<origin>-<destination>-<YYYYMMDD>T<HHMM>Z``
    e.g. ``gigm-road-LOS-ABV-20260914T0600Z``.

    Built on the UTC INSTANT, not the formatted string, so the same departure
    expressed as '07:00+01:00' or '06:00Z' produces the same id. That is what
    lets us watch one departure's price move over hours and build the
    price-delta features the model needs.

    Price is deliberately NOT part of the id - a departure whose price changed
    is the same departure, and that change is exactly the signal we are learning.

    A readable composite rather than a hash, for two reasons. ksqlDB has no
    SHA-256 function, so a hash could not be computed in the stream layer
    without shipping a custom Java UDF; and when an offer looks wrong, an id
    that says what it refers to is far easier to debug than 16 hex characters.
    """
    instant = to_utc_instant(depart_time)
    return "-".join([
        carrier_key_for(carrier),
        mode.strip().lower(),
        origin.strip().upper(),
        destination.strip().upper(),
        instant.strftime("%Y%m%dT%H%MZ"),
    ])
