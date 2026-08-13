"""Place registry for the four in-scope routes.

Why this file exists: the canonical schema uses 3-letter place codes, but one of
our four destinations - Onitsha - has no airport and therefore no IATA code. We
cannot invent an IATA code (that would be wrong and would collide with a real
airport somewhere), and we cannot leave it out (it is an in-scope route). So the
registry distinguishes real IATA codes from internal codes and records which is
which, honestly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

CodeKind = Literal["iata", "internal"]


@dataclass(frozen=True)
class Place:
    code: str
    name: str
    kind: CodeKind
    timezone: str
    country: str
    has_airport: bool
    # Ground time added to air journeys: getting to the airport, security,
    # baggage, and the transfer at the far end. Without this, "fastest" would
    # always be air by a landslide and the comparison would be dishonest.
    airport_ground_minutes: int = 0


PLACES: dict[str, Place] = {
    "LOS": Place(
        code="LOS",
        name="Lagos",
        kind="iata",
        timezone="Africa/Lagos",
        country="NG",
        has_airport=True,
        airport_ground_minutes=150,  # Murtala Muhammed: notorious traffic + check-in
    ),
    "ABV": Place(
        code="ABV",
        name="Abuja",
        kind="iata",
        timezone="Africa/Lagos",
        country="NG",
        has_airport=True,
        airport_ground_minutes=90,
    ),
    "PHC": Place(
        code="PHC",
        name="Port Harcourt",
        kind="iata",
        timezone="Africa/Lagos",
        country="NG",
        has_airport=True,
        airport_ground_minutes=90,
    ),
    "LHR": Place(
        code="LHR",
        name="London Heathrow",
        kind="iata",
        timezone="Europe/London",
        country="GB",
        has_airport=True,
        airport_ground_minutes=120,
    ),
    "ONI": Place(
        code="ONI",
        name="Onitsha",
        kind="internal",  # NOT an IATA code - Onitsha has no airport
        timezone="Africa/Lagos",
        country="NG",
        has_airport=False,
        airport_ground_minutes=0,
    ),
}

# The only routes this project serves. Anything else is out of scope and the
# API returns a 400 rather than an empty result, so the user knows the
# difference between "no flights" and "we don't cover that".
ROUTES: list[tuple[str, str]] = [
    ("LOS", "LHR"),
    ("LOS", "ABV"),
    ("LOS", "ONI"),
    ("LOS", "PHC"),
]


def all_route_pairs() -> set[tuple[str, str]]:
    """Both directions of every in-scope route (Lagos <-> X)."""
    pairs: set[tuple[str, str]] = set()
    for origin, dest in ROUTES:
        pairs.add((origin, dest))
        pairs.add((dest, origin))
    return pairs


def is_supported_route(origin: str, destination: str) -> bool:
    return (origin, destination) in all_route_pairs()


def get_place(code: str) -> Place:
    try:
        return PLACES[code]
    except KeyError:
        raise KeyError(
            f"Unknown place code {code!r}. Known codes: {sorted(PLACES)}"
        ) from None


def supports_mode(code: str, mode: str) -> bool:
    """Onitsha cannot be served by air. Guards against a connector emitting an
    impossible offer, e.g. a flight to a city with no airport."""
    place = get_place(code)
    if mode == "air":
        return place.has_airport
    return True
