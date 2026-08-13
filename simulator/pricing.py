"""The price model behind the simulator.

This is a first-class component, not a stand-in. Two requirements shape it:

1. **Reproducible.** Every price is a pure function of
   (carrier, route, departure, observation time, seed). Nothing is stored, so
   restarting the simulator does not create a discontinuity in price history,
   and a test can assert an exact figure.

2. **Learnable.** Phase 4 trains on seven features: days-to-departure, hour of
   day, day of week, seats left, recent price deltas, carrier and route. If the
   simulator emitted noise, the model could not beat AUC 0.50 no matter how well
   it was tuned. So real, signed relationships are built in here deliberately -
   fares climb as departure nears, scarcity lifts them further, and weekends
   cost more. The model's job is to recover those relationships from prices
   alone.

Everything here is pure and unit-tested.
"""

from __future__ import annotations

import hashlib
import math
from datetime import datetime, timedelta

from common.places import get_place

# How far ahead the simulator sells. Beyond this a departure is not on sale.
BOOKING_HORIZON_DAYS = 30

# Base one-way fare in NGN, before any adjustment. Road fares reflect real
# Nigerian interstate coach pricing; air fares reflect domestic economy.
BASE_FARE_NGN: dict[tuple[str, str], float] = {
    ("road", "LOS-ABV"): 38_000,
    ("road", "LOS-ONI"): 22_000,
    ("road", "LOS-PHC"): 30_000,
    ("air", "LOS-ABV"): 145_000,
    ("air", "LOS-PHC"): 165_000,
    ("air", "LOS-LHR"): 1_450_000,
}

# Per-carrier positioning: a premium operator sits above the route's base fare.
CARRIER_POSITIONING: dict[str, float] = {
    "GIGM": 1.10,           # market leader, charges for it
    "ABC Transport": 1.00,
    "Cross Country": 0.92,  # competes on price
    "CHISCO": 0.95,
    "Air Peace": 1.00,
    "Ibom Air": 1.08,       # reputation for punctuality
    "Arik Air": 0.94,
    "British Airways": 1.00,
}

# Seats offered per departure.
CAPACITY: dict[str, int] = {"road": 59, "air": 150}


def _unit_noise(*parts: object) -> float:
    """Deterministic pseudo-random value in [0, 1) from the given parts.

    Used instead of `random` so that the same departure observed at the same
    moment always yields the same figure, on any machine, in any process.
    """
    key = "|".join(str(p) for p in parts).encode("utf-8")
    digest = hashlib.sha256(key).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def route_key(origin: str, destination: str) -> str:
    """Direction-independent route key: LOS-ABV and ABV-LOS price the same.

    Every in-scope route touches Lagos, so the key is always written Lagos
    first. Stated explicitly rather than derived from a sort, which silently
    produced 'ABV-LOS' for the outbound direction.
    """
    if origin == destination:
        raise ValueError(f"origin and destination are both {origin!r}")
    pair = {origin, destination}
    if "LOS" not in pair:
        raise ValueError(
            f"route {origin}-{destination} does not touch Lagos, so it is not "
            "one of the four in-scope routes"
        )
    (other,) = pair - {"LOS"}
    return f"LOS-{other}"


def base_fare(mode: str, origin: str, destination: str) -> float:
    key = (mode, route_key(origin, destination))
    if key not in BASE_FARE_NGN:
        raise KeyError(f"no base fare configured for {key}")
    return BASE_FARE_NGN[key]


def days_to_departure(depart: datetime, now: datetime) -> float:
    return (depart - now).total_seconds() / 86_400


def urgency_multiplier(dtd: float) -> float:
    """Fares climb as departure approaches.

    A smooth exponential rather than banded steps: real revenue management moves
    continuously, and a smooth curve gives the model a cleaner signal to learn
    than a step function would.

    ~0.88x a month out, ~1.00x at two weeks, ~1.45x the day before.
    """
    if dtd < 0:
        raise ValueError(f"departure is in the past: dtd={dtd}")
    return 0.86 + 0.72 * math.exp(-dtd / 7.5)


def seats_remaining(
    carrier: str, depart: datetime, now: datetime, mode: str, seed: int
) -> int:
    """Seats sell down as departure approaches, at a pace that varies by
    departure so every service does not deplete identically."""
    capacity = CAPACITY[mode]
    dtd = max(0.0, days_to_departure(depart, now))
    remaining_fraction = min(1.0, dtd / BOOKING_HORIZON_DAYS) ** 0.65

    # Some departures are simply more popular than others.
    popularity = 0.55 + 0.5 * _unit_noise(seed, carrier, depart.isoformat(), "pop")
    seats = capacity * remaining_fraction / popularity
    return max(0, min(capacity, int(round(seats))))


def scarcity_multiplier(seats_left: int, mode: str) -> float:
    """The last few seats cost more. Caps at +45% so prices stay plausible."""
    capacity = CAPACITY[mode]
    free_fraction = max(0.0, min(1.0, seats_left / capacity))
    return 1.0 + 0.45 * (1.0 - free_fraction) ** 2


def weekday_multiplier(depart: datetime) -> float:
    """Friday and Sunday departures cost more; midweek is cheapest.

    Nigerian intercity travel peaks around the weekend, so this is a real
    effect rather than decoration - and it is one of the model's features.
    """
    return {0: 0.97, 1: 0.95, 2: 0.96, 3: 1.02, 4: 1.14, 5: 1.05, 6: 1.12}[
        depart.weekday()
    ]


def hour_multiplier(depart: datetime) -> float:
    """Early-morning and early-evening departures are the ones people want."""
    hour = depart.hour
    if 5 <= hour <= 8:
        return 1.09        # beat the traffic / full day at destination
    if 16 <= hour <= 19:
        return 1.06        # after-work departures
    if 22 <= hour or hour <= 4:
        return 0.88        # overnight coaches, cheapest
    return 1.0


def market_noise(carrier: str, depart: datetime, now: datetime, seed: int) -> float:
    """Short-term drift, so prices wobble instead of moving monotonically.

    Bucketed into 30-minute windows: within a window the price is stable (as a
    real fare would be), and it steps between windows. Two overlapping cycles
    avoid an obviously repeating pattern.
    """
    bucket = int(now.timestamp() // 1800)
    a = _unit_noise(seed, carrier, depart.isoformat(), bucket, "a")
    b = _unit_noise(seed, carrier, depart.isoformat(), bucket // 8, "b")
    return 1.0 + 0.055 * (a - 0.5) + 0.045 * (b - 0.5)


def simulate_price_ngn(
    carrier: str,
    mode: str,
    origin: str,
    destination: str,
    depart: datetime,
    now: datetime,
    seed: int,
    seats_left: int | None = None,
) -> float:
    """The full price model. Returns NGN rounded to the nearest 100.

    Fares are quoted in round numbers; a coach ticket is never NGN 42,517.
    """
    if seats_left is None:
        seats_left = seats_remaining(carrier, depart, now, mode, seed)

    price = (
        base_fare(mode, origin, destination)
        * CARRIER_POSITIONING.get(carrier, 1.0)
        * urgency_multiplier(max(0.0, days_to_departure(depart, now)))
        * scarcity_multiplier(seats_left, mode)
        * weekday_multiplier(depart)
        * hour_multiplier(depart)
        * market_noise(carrier, depart, now, seed)
    )
    return float(round(price / 100.0) * 100)


def duration_minutes(mode: str, origin: str, destination: str) -> int:
    """Scheduled journey time, gate to gate or park to park.

    The door-to-door figure used by the Fastest ranking adds airport ground time
    on top of this - see places.airport_ground_minutes.
    """
    key = route_key(origin, destination)
    if mode == "road":
        return {"LOS-ABV": 720, "LOS-ONI": 480, "LOS-PHC": 600}[key]
    return {"LOS-ABV": 75, "LOS-PHC": 65, "LOS-LHR": 390}[key]


def door_to_door_minutes(mode: str, origin: str, destination: str) -> int:
    """What the traveller actually spends, including getting to the airport.

    Without this, air always wins the Fastest ranking by a landslide and the
    road/air comparison the project exists to make would be dishonest: a 75
    minute flight is not a 75 minute journey.
    """
    scheduled = duration_minutes(mode, origin, destination)
    if mode == "road":
        return scheduled
    return (
        scheduled
        + get_place(origin).airport_ground_minutes
        + get_place(destination).airport_ground_minutes
    )


def departure_times(carrier_key: str, mode: str) -> list[int]:
    """Which hours of the day this carrier runs, local time."""
    if mode == "air":
        return {
            "airpeace": [7, 11, 16],
            "ibomair": [8, 14],
            "arik": [9, 17],
            "britishairways": [23],   # LOS-LHR overnight
        }[carrier_key]
    return {
        "gigm": [6, 8, 22],
        "abctransport": [7, 21],
        "crosscountry": [6, 20],
        "chisco": [7, 22],
    }[carrier_key]


def upcoming_departures(
    carrier_key: str, mode: str, now: datetime, timezone_name: str,
    horizon_days: int = BOOKING_HORIZON_DAYS,
) -> list[datetime]:
    """Every departure currently on sale, as tz-aware local datetimes."""
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(timezone_name)
    local_now = now.astimezone(tz)
    hours = departure_times(carrier_key, mode)

    departures: list[datetime] = []
    for day in range(horizon_days + 1):
        date = (local_now + timedelta(days=day)).date()
        for hour in hours:
            candidate = datetime(
                date.year, date.month, date.day, hour, 0, tzinfo=tz
            )
            # Only sell departures still in the future and inside the horizon.
            if candidate > local_now and days_to_departure(candidate, now) <= horizon_days:
                departures.append(candidate)
    return sorted(departures)
