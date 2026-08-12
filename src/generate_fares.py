"""
Stage 1 — Synthetic fare generator (stands in for per-carrier Kafka source connectors).

In production, each carrier below would have its own Kafka source connector
(REST poll + Scrapy/Playwright scraper) pushing raw fares to a per-carrier topic.
Here we simulate those raw feeds with realistic price *trajectories* so the
downstream LightGBM "price will rise in 24h" model has a learnable signal.

Output: data/raw/<carrier>.csv  (one file per carrier = one Kafka topic)
"""
from __future__ import annotations
import os
import csv
import random
from datetime import date, datetime, timedelta

random.seed(42)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RAW_DIR = os.path.join(ROOT, "data", "raw")

# --- Carrier catalogue -------------------------------------------------------
# mode, list of carriers, and the routes each mode serves.
ROAD_CARRIERS = ["GIGM", "ABC Transport", "Cross Country", "CHISCO"]
AIR_CARRIERS = ["Air Peace", "Ibom Air", "Arik Air", "British Airways"]

# Canonical routes as (origin, destination). All are Lagos (LOS) return legs.
ROAD_ROUTES = [("LOS", "ABV"), ("LOS", "ONI"), ("LOS", "PHC")]          # domestic road
AIR_DOMESTIC = [("LOS", "ABV"), ("LOS", "PHC")]                         # domestic air
AIR_INTL = [("LOS", "LON")]                                            # international air

# Baseline one-way fare (NGN) and duration (minutes) by route + mode.
# Road is cheaper/slower; air is pricier/faster; London is international.
BASE = {
    ("road", ("LOS", "ABV")): (18000, 600),
    ("road", ("LOS", "ONI")): (15000, 480),
    ("road", ("LOS", "PHC")): (20000, 720),
    ("air", ("LOS", "ABV")): (85000, 75),
    ("air", ("LOS", "PHC")): (95000, 65),
    ("air", ("LOS", "LON")): (1250000, 405),  # BA Lagos–London
}

# How strongly demand (and thus the chance of a price rise) grows as the
# departure date approaches, per mode. Higher = fares climb harder near departure.
URGENCY = {"road": 0.35, "air": 0.55}

CAPTURE_HORIZON_DAYS = 30      # we start watching each trip 30 days out
DEPARTURES_PER_ROUTE = 14      # distinct departure dates per route/carrier


def base_for(mode: str, route: tuple[str, str]) -> tuple[float, int]:
    """Base price/duration for a route, falling back to the reverse leg's
    figures when only the outbound direction is in BASE (return fares mirror
    the outbound ones)."""
    return BASE.get((mode, route)) or BASE[(mode, (route[1], route[0]))]


def carrier_bias(carrier: str) -> float:
    """Each carrier sits a little above/below the market — deterministic per name."""
    random.seed(hash(carrier) % (2**31))
    b = random.uniform(-0.12, 0.15)
    random.seed()  # restore
    return b


def price_trajectory(base_price: float, mode: str, days_out_start: int) -> list[tuple[int, float]]:
    """
    Simulate a daily fare series for one trip, captured each day from
    `days_out_start` down to 1 day before departure. Returns [(days_to_dep, price), ...].

    Model: a random walk whose *probability of an upward move each day* rises as
    departure approaches (closeness) and carries momentum from the last move.
    This makes the "will price rise in 24h" label balanced overall AND genuinely
    predictable from days_to_departure + recent trend — a real signal to learn.
    """
    series = []
    price = base_price * random.uniform(0.9, 1.10)
    last_up = 1  # momentum: was the previous move up?
    for d in range(days_out_start, 0, -1):
        closeness = (days_out_start - d) / max(1, days_out_start)  # 0 -> 1 near departure
        # base 40% chance to rise far out, climbing toward ~85% near departure
        p_up = 0.40 + URGENCY[mode] * closeness + 0.10 * (last_up - 0.5) * 2
        p_up = min(0.92, max(0.15, p_up))
        up = random.random() < p_up
        step = random.uniform(0.005, 0.045)
        price *= (1 + step) if up else (1 - step * random.uniform(0.6, 1.0))
        # occasional promo shock (independent of trend)
        if random.random() < 0.05:
            price *= random.uniform(0.90, 0.97)
        price = max(base_price * 0.72, min(base_price * 1.9, price))
        last_up = 1 if up else 0
        series.append((d, round(price, -2)))  # round to nearest 100 NGN
    return series


def build():
    os.makedirs(RAW_DIR, exist_ok=True)
    today = date.today()
    rows_by_carrier: dict[str, list[dict]] = {}

    def add_carrier_route(carrier, mode, route):
        base_price, duration = base_for(mode, route)
        base_price *= (1 + carrier_bias(carrier))
        rows = rows_by_carrier.setdefault(carrier, [])
        for _ in range(DEPARTURES_PER_ROUTE):
            dep_offset = random.randint(3, 45)
            dep_date = today + timedelta(days=dep_offset)
            dep_hour = random.choice([6, 7, 9, 11, 14, 16, 18, 20])
            start = min(CAPTURE_HORIZON_DAYS, dep_offset)
            traj = price_trajectory(base_price, mode, start)
            trip_id = f"{carrier[:3].upper()}-{route[0]}{route[1]}-{dep_date:%Y%m%d}-{dep_hour:02d}"
            dur = int(duration * random.uniform(0.95, 1.15))
            for days_to_dep, price in traj:
                capture_dt = datetime.combine(dep_date, datetime.min.time()) - timedelta(days=days_to_dep)
                rows.append({
                    "trip_id": trip_id,
                    "carrier": carrier,
                    "mode": mode,
                    "origin": route[0],
                    "destination": route[1],
                    "departure_date": dep_date.isoformat(),
                    "departure_hour": dep_hour,
                    "duration_min": dur,
                    "captured_at": capture_dt.isoformat(),
                    "days_to_departure": days_to_dep,
                    "price_ngn": price,
                })

    # Every route is bookable in both directions (outbound + return leg).
    for c in ROAD_CARRIERS:
        for o, d in ROAD_ROUTES:
            add_carrier_route(c, "road", (o, d))
            add_carrier_route(c, "road", (d, o))
    for c in AIR_CARRIERS:
        routes = AIR_INTL if c == "British Airways" else AIR_DOMESTIC
        for o, d in routes:
            add_carrier_route(c, "air", (o, d))
            add_carrier_route(c, "air", (d, o))

    total = 0
    for carrier, rows in rows_by_carrier.items():
        fname = os.path.join(RAW_DIR, carrier.replace(" ", "_") + ".csv")
        with open(fname, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        total += len(rows)
        print(f"  {carrier:16s} -> {len(rows):5d} fare snapshots  ({os.path.basename(fname)})")
    print(f"Generated {total} raw fare snapshots across {len(rows_by_carrier)} carriers.")


if __name__ == "__main__":
    print("Stage 1: generating synthetic per-carrier fare feeds...")
    build()