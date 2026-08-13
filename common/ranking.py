"""Ranking and staleness rules for the search API.

Two ideas live here, both of which decide what a user actually sees:

  * **Cheapest** is the lowest total price. Simple, and unambiguous.
  * **Fastest** is the shortest DOOR-TO-DOOR journey, not the shortest
    scheduled one. A 75-minute Lagos-Abuja flight is not a 75-minute journey:
    you must reach Murtala Muhammed through Lagos traffic, check in, and get
    out the other end. Ranking on scheduled time alone would put air above
    road every single time and make the road/air comparison this project
    exists for meaningless.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from common.normalise import to_utc_instant
from common.places import get_place


def door_to_door_minutes(offer: dict[str, Any]) -> int:
    """Total time the traveller actually spends, terminal to terminal.

    Road journeys already are door to door - you board in the city you are in
    and alight in the city you want. Air journeys carry the ground time at both
    ends, which is where the honesty comes from.
    """
    scheduled = int(offer["duration_min"])
    if offer["mode"] == "road":
        return scheduled
    return (
        scheduled
        + get_place(offer["origin"]).airport_ground_minutes
        + get_place(offer["destination"]).airport_ground_minutes
    )


def age_seconds(offer: dict[str, Any], now: datetime | None = None) -> float:
    now = now or datetime.now(timezone.utc)
    return (now - to_utc_instant(offer["fetched_at"])).total_seconds()


def is_stale(offer: dict[str, Any], threshold_seconds: int,
             now: datetime | None = None) -> bool:
    """Has this price gone quiet for longer than we are comfortable with?

    A stale price is SHOWN, clearly flagged, never hidden. A traveller would
    far rather see a price from twenty minutes ago labelled as such than an
    empty page - and an empty page also hides the fact that a connector died.
    """
    return age_seconds(offer, now) > threshold_seconds


def annotate(offer: dict[str, Any], threshold_seconds: int,
             now: datetime | None = None) -> dict[str, Any]:
    """Add the derived fields the UI needs, without touching canonical ones."""
    now = now or datetime.now(timezone.utc)
    enriched = dict(offer)
    enriched["door_to_door_min"] = door_to_door_minutes(offer)
    enriched["age_seconds"] = round(age_seconds(offer, now), 1)
    enriched["stale"] = is_stale(offer, threshold_seconds, now)
    return enriched


def rank_cheapest(offers: Iterable[dict[str, Any]], limit: int | None = None
                  ) -> list[dict[str, Any]]:
    """Lowest price first. Ties break on the faster journey, then on carrier,
    so the order is stable rather than dependent on Redis scan order."""
    ranked = sorted(
        offers,
        key=lambda o: (float(o["price_ngn"]), o["door_to_door_min"], o["carrier"]),
    )
    return ranked[:limit] if limit else ranked


def rank_fastest(offers: Iterable[dict[str, Any]], limit: int | None = None
                 ) -> list[dict[str, Any]]:
    """Shortest door-to-door journey first, ties broken on price."""
    ranked = sorted(
        offers,
        key=lambda o: (o["door_to_door_min"], float(o["price_ngn"]), o["carrier"]),
    )
    return ranked[:limit] if limit else ranked


def local_departure_date(offer: dict[str, Any]) -> str:
    """The departure date as the TRAVELLER would say it - local, not UTC.

    This matters at the edges of the day. A 22:00+01:00 coach departs on the
    21:00Z of the same date, but a 00:30+01:00 coach departs at 23:30Z the day
    BEFORE. Filtering a search on the UTC date would quietly drop or misfile
    every overnight departure, and overnight coaches are the cheapest ones on
    these routes.
    """
    return offer["depart_time"][:10]
