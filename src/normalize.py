"""
Stage 2 — Normalisation layer (stands in for ksqlDB unifying carrier topics).

Reads every per-carrier raw feed from data/raw/*.csv, cleans/deduplicates,
and unifies them into a single canonical `offers` table plus a `routes`
reference table. In production this is a set of ksqlDB stream queries writing
to PostgreSQL; here we use SQLite so it runs with zero setup. The schema and
SQL are written to be Postgres-compatible.

Output: data/aggregator.db  (tables: offers, routes)
"""
from __future__ import annotations
import os
import csv
import glob
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RAW_DIR = os.path.join(ROOT, "data", "raw")
DB_PATH = os.environ.get("AGG_DB", os.path.join(ROOT, "data", "aggregator.db"))

CITY = {
    "LOS": "Lagos", "ABV": "Abuja", "ONI": "Onitsha",
    "PHC": "Port Harcourt", "LON": "London",
}

SCHEMA = """
DROP TABLE IF EXISTS offers;
DROP TABLE IF EXISTS routes;

CREATE TABLE routes (
    route_id     TEXT PRIMARY KEY,
    origin       TEXT NOT NULL,
    destination  TEXT NOT NULL,
    origin_city  TEXT NOT NULL,
    dest_city    TEXT NOT NULL
);

CREATE TABLE offers (
    offer_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id           TEXT NOT NULL,
    route_id          TEXT NOT NULL,
    carrier           TEXT NOT NULL,
    mode              TEXT NOT NULL,           -- 'road' | 'air'
    departure_date    TEXT NOT NULL,
    departure_hour    INTEGER NOT NULL,
    duration_min      INTEGER NOT NULL,
    captured_at       TEXT NOT NULL,
    days_to_departure INTEGER NOT NULL,
    price_ngn         REAL NOT NULL
);
CREATE INDEX idx_offers_route_date ON offers(route_id, departure_date);
CREATE INDEX idx_offers_trip ON offers(trip_id, captured_at);
"""


def normalize():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)

    files = sorted(glob.glob(os.path.join(RAW_DIR, "*.csv")))
    if not files:
        raise SystemExit("No raw feeds found — run generate_fares.py first.")

    seen = set()          # (trip_id, captured_at) dedupe key
    routes = {}
    offers = []
    for path in files:
        with open(path, newline="") as f:
            for r in csv.DictReader(f):
                key = (r["trip_id"], r["captured_at"])
                if key in seen:
                    continue
                seen.add(key)
                o, d = r["origin"], r["destination"]
                route_id = f"{o}-{d}"
                routes.setdefault(route_id, (o, d, CITY.get(o, o), CITY.get(d, d)))
                offers.append((
                    r["trip_id"], route_id, r["carrier"], r["mode"],
                    r["departure_date"], int(r["departure_hour"]),
                    int(r["duration_min"]), r["captured_at"],
                    int(r["days_to_departure"]), float(r["price_ngn"]),
                ))

    con.executemany(
        "INSERT INTO routes VALUES (?,?,?,?,?)",
        [(rid, *vals) for rid, vals in routes.items()],
    )
    con.executemany(
        """INSERT INTO offers
           (trip_id, route_id, carrier, mode, departure_date, departure_hour,
            duration_min, captured_at, days_to_departure, price_ngn)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        offers,
    )
    con.commit()
    n_offers = con.execute("SELECT COUNT(*) FROM offers").fetchone()[0]
    n_routes = con.execute("SELECT COUNT(*) FROM routes").fetchone()[0]
    n_carriers = con.execute("SELECT COUNT(DISTINCT carrier) FROM offers").fetchone()[0]
    con.close()
    print(f"  unified {n_offers} offers | {n_carriers} carriers | {n_routes} routes")
    print(f"  wrote {DB_PATH}")


if __name__ == "__main__":
    print("Stage 2: normalising carrier feeds into canonical `offers` table...")
    normalize()