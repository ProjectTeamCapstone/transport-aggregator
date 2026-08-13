"""Per-carrier raw message shapes.

Each carrier here publishes in its own idiosyncratic format, exactly as a real
carrier would. That is the point: Phase 2's job is to reconcile eight different
ways of describing the same thing, and if the simulator emitted ready-made
canonical messages the stream layer would have nothing to do and the tests
would prove nothing.

The variations are drawn from what these formats really look like in the wild:

  GIGM            DD-MM-YYYY date + separate time, price as "NGN 42,500",
                  seats as "12 left", city names rather than codes
  ABC Transport   named terminals, single "YYYY-MM-DD HH:MM:SS" local datetime
  Cross Country   epoch seconds, and price in KOBO (1/100 of a naira)
  CHISCO          DD/MM/YYYY HH:MM, price as "30,000.00"
  Air Peace       IATA codes, ISO-8601 with offset, fare as a nested object
  Ibom Air        departure already in UTC - must be put back into local time
  Arik Air        minutes-since-midnight plus a separate date
  British Airways fares in GBP - the only carrier needing currency conversion
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

# Road operators publish city names, not airport codes. The stream layer maps
# these to canonical place codes; keeping the mapping OUT of the simulator is
# deliberate, so the ksqlDB statements are genuinely doing the work.
CITY_NAMES: dict[str, str] = {
    "LOS": "Lagos",
    "ABV": "Abuja",
    "ONI": "Onitsha",
    "PHC": "Port Harcourt",
    "LHR": "London",
}

TERMINALS: dict[str, str] = {
    "LOS": "LAGOS-JIBOWU",
    "ABV": "ABUJA-UTAKO",
    "ONI": "ONITSHA-UPPER-IWEKA",
    "PHC": "PORT-HARCOURT-WATERLINES",
}


def _z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _money(value: float) -> str:
    return f"{value:,.2f}"


# --------------------------------------------------------------- road ------


def gigm(depart: datetime, price: float, seats: int, origin: str,
         destination: str, duration_min: int, now: datetime) -> dict[str, Any]:
    return {
        "operator": "G.I.G.M",
        "from_city": CITY_NAMES[origin],
        "to_city": CITY_NAMES[destination],
        "dep_date": depart.strftime("%d-%m-%Y"),      # DD-MM-YYYY
        "dep_time": depart.strftime("%H:%M"),
        "fare": f"NGN {price:,.0f}",                   # "NGN 42,500"
        "seats": f"{seats} left",
        "travel_hours": round(duration_min / 60, 1),
        "booking_link": f"https://gigm.com/booking?trip={origin}{destination}",
        "scraped_at": _z(now),
    }


def abctransport(depart: datetime, price: float, seats: int, origin: str,
                 destination: str, duration_min: int, now: datetime) -> dict[str, Any]:
    return {
        "company": "ABC Transport",
        "origin_terminal": TERMINALS[origin],
        "destination_terminal": TERMINALS[destination],
        "departs": depart.strftime("%Y-%m-%d %H:%M:%S"),  # local, no offset
        "amount": float(price),
        "seats_available": seats,
        "journey_minutes": duration_min,
        "url": f"https://abctransport.com/booking/{origin}-{destination}",
        "ts": _z(now),
    }


def crosscountry(depart: datetime, price: float, seats: int, origin: str,
                 destination: str, duration_min: int, now: datetime) -> dict[str, Any]:
    # Prices in kobo, timestamps in epoch seconds - a genuine integration
    # hazard: read the kobo figure as naira and every fare is 100x too high.
    return {
        "carrier_code": "CC",
        "start": CITY_NAMES[origin],
        "end": CITY_NAMES[destination],
        "epoch_departure": int(depart.timestamp()),
        "price_kobo": int(round(price * 100)),
        "seats": seats,
        "duration_secs": duration_min * 60,
        "deeplink": f"https://crosscountry.example/book/{origin}{destination}",
        "captured_at_epoch": int(now.timestamp()),
    }


def chisco(depart: datetime, price: float, seats: int, origin: str,
           destination: str, duration_min: int, now: datetime) -> dict[str, Any]:
    return {
        "line": "CHISCO",
        "board": CITY_NAMES[origin],
        "alight": CITY_NAMES[destination],
        "when": depart.strftime("%d/%m/%Y %H:%M"),     # DD/MM/YYYY HH:MM
        "cost": _money(price),                          # "30,000.00"
        "open_seats": seats,
        "hours": round(duration_min / 60, 1),
        "link": f"https://chiscotransport.com/book?r={origin}{destination}",
        "pulled": _z(now),
    }


# ---------------------------------------------------------------- air ------


def airpeace(depart: datetime, price: float, seats: int, origin: str,
             destination: str, duration_min: int, now: datetime) -> dict[str, Any]:
    return {
        "airline": "Air Peace",
        "flight_number": f"P4{7000 + depart.hour * 3 + len(destination)}",
        "dep_airport": origin,
        "arr_airport": destination,
        "std": depart.isoformat(timespec="seconds"),   # ISO-8601 with offset
        "cabin": "ECONOMY",
        "total_fare": {"amount": float(price), "currency": "NGN"},  # nested
        "seats_remaining": seats,
        "duration_minutes": duration_min,
        "deep_link": f"https://flyairpeace.com/book?o={origin}&d={destination}",
        "fetched": _z(now),
    }


def ibomair(depart: datetime, price: float, seats: int, origin: str,
            destination: str, duration_min: int, now: datetime) -> dict[str, Any]:
    # Publishes departure already converted to UTC. The stream layer must put
    # it back into local time, or every Ibom Air departure shows an hour early.
    return {
        "carrier": "QI",
        "from": origin,
        "to": destination,
        "departure_utc": _z(depart),
        "fare_ngn": float(price),
        "seats": seats,
        "block_minutes": duration_min,
        "booking_url": f"https://ibomair.com/select?from={origin}&to={destination}",
        "as_of": _z(now),
    }


def arik(depart: datetime, price: float, seats: int, origin: str,
         destination: str, duration_min: int, now: datetime) -> dict[str, Any]:
    # Date and time-of-day arrive separately, the latter as minutes past
    # midnight - a legacy shape that still turns up in airline feeds.
    return {
        "op_carrier": "W3",
        "board_point": origin,
        "off_point": destination,
        "flight_date": depart.strftime("%Y-%m-%d"),
        "dep_minutes_after_midnight": depart.hour * 60 + depart.minute,
        "fare_amount": float(price),
        "fare_currency": "NGN",
        "avail": seats,
        "elapsed_minutes": duration_min,
        "url": f"https://arikair.com/booking/{origin}{destination}",
        "extracted_at": _z(now),
    }


def britishairways(depart: datetime, price_ngn: float, seats: int, origin: str,
                   destination: str, duration_min: int, now: datetime,
                   gbp_ngn_rate: float) -> dict[str, Any]:
    # The only carrier quoting a foreign currency. The simulator works in NGN
    # internally, so the fare is converted BACK to GBP here - which is exactly
    # what makes this a real test of the conversion path in Phase 2.
    return {
        "marketingCarrier": "BA",
        "origin": origin,
        "destination": destination,
        "departureDateTime": depart.isoformat(timespec="seconds"),
        "cabinClass": "M",
        "totalPrice": round(price_ngn / gbp_ngn_rate, 2),
        "currencyCode": "GBP",
        "availableSeats": seats,
        "journeyDurationMinutes": duration_min,
        "bookingUrl": f"https://britishairways.com/travel/book?o={origin}&d={destination}",
        "timestamp": _z(now),
    }


BUILDERS = {
    "gigm": gigm,
    "abctransport": abctransport,
    "crosscountry": crosscountry,
    "chisco": chisco,
    "airpeace": airpeace,
    "ibomair": ibomair,
    "arik": arik,
    "britishairways": britishairways,
}
