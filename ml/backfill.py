"""Generate the price history the model needs to learn from.

WHY THIS EXISTS, STATED PLAINLY
-------------------------------
The label this project predicts is "did the price rise in the following 24
hours?". Learning that needs observations of the same departure spaced 24 hours
apart, across many departures. The live pipeline has been running for minutes,
not days, so training on it would produce a model that has never seen a single
complete label.

The simulator was built as a pure function of observation time with no stored
state, precisely so this is possible: asking it for a price "as at" a past
moment returns exactly what it would have returned had it been running then.
This script replays it over a window of past timestamps and writes the result
into offers_history.

WHAT THIS IS NOT
----------------
This is not a bypass of the pipeline, and it is not a substitute for real data.
The rows it writes are simulated, and a model trained on them learns the
SIMULATOR's price dynamics, not Nigeria's. Any accuracy reported from this data
answers "can the model recover the rules the simulator was given?" - a real
question, and a necessary sanity check, but not the same as predicting real
fares. Every report must say so.

It writes straight to PostgreSQL rather than through Kafka. Pushing ~350k
historical messages through the live stream would take a long time, would mix
backfilled history into the live topics, and would prove nothing that the
Phase 2 tests do not already prove.

    python -m ml.backfill --days 40 --every-hours 6
"""

from __future__ import annotations

import argparse
import io
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg  # noqa: E402

from common.carriers import CARRIERS  # noqa: E402
from common.config import SIMULATOR_SEED, postgres_dsn  # noqa: E402
from common.normalise import canonical_offer_id  # noqa: E402
from common.places import get_place  # noqa: E402
from simulator.pricing import (  # noqa: E402
    duration_minutes,
    seats_remaining,
    simulate_price_ngn,
    upcoming_departures,
)
from simulator.run import _directions  # noqa: E402

BACKFILL_MARKER = "backfill"


def observations_at(now: datetime, seed: int):
    """Every offer that would have been on sale at `now`."""
    for carrier in CARRIERS:
        for origin, destination in _directions(carrier):
            tz_name = get_place(origin).timezone
            scheduled = duration_minutes(carrier.mode, origin, destination)

            for depart in upcoming_departures(carrier.key, carrier.mode, now, tz_name):
                seats = seats_remaining(
                    carrier.name, depart, now, carrier.mode, seed)
                price = simulate_price_ngn(
                    carrier.name, carrier.mode, origin, destination,
                    depart, now, seed, seats_left=seats)
                yield (
                    canonical_offer_id(
                        carrier.name, carrier.mode, origin, destination,
                        depart.isoformat()),
                    carrier.name,
                    carrier.mode,
                    origin,
                    destination,
                    depart.isoformat(timespec="seconds"),
                    scheduled,
                    price,
                    seats,
                    f"https://example.test/{carrier.key}",
                    now.isoformat(timespec="seconds").replace("+00:00", "Z"),
                )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=40,
                        help="how far back to replay")
    parser.add_argument("--every-hours", type=int, default=6,
                        help="spacing between observations")
    parser.add_argument("--seed", type=int, default=SIMULATOR_SEED)
    parser.add_argument("--truncate", action="store_true",
                        help="clear offers_history first")
    args = parser.parse_args()

    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    snapshots = [
        now - timedelta(hours=h)
        for h in range(args.days * 24, 0, -args.every_hours)
    ]
    print(f"Replaying {len(snapshots)} snapshots, "
          f"{snapshots[0]:%Y-%m-%d %H:%M} -> {snapshots[-1]:%Y-%m-%d %H:%M} UTC")

    with psycopg.connect(postgres_dsn()) as conn:
        if args.truncate:
            conn.execute("TRUNCATE offers_history")
            conn.commit()
            print("  cleared offers_history")

        total = 0
        for i, moment in enumerate(snapshots, 1):
            buffer = io.StringIO()
            rows = 0
            for row in observations_at(moment, args.seed):
                buffer.write("\t".join(str(f) for f in row) + "\n")
                rows += 1
            buffer.seek(0)

            # COPY, not INSERT: ~350k rows one at a time would take far longer
            # than the rest of this phase put together.
            with conn.cursor() as cur:
                with cur.copy(
                    "COPY offers_history (offer_id, carrier, mode, origin, "
                    "destination, depart_time, duration_min, price_ngn, "
                    "seats_left, booking_url, fetched_at) FROM STDIN"
                ) as copy:
                    copy.write(buffer.read())
            conn.commit()
            total += rows
            if i % 20 == 0 or i == len(snapshots):
                print(f"  [{i:>3}/{len(snapshots)}] {moment:%Y-%m-%d %H:%M}  "
                      f"+{rows} rows  (total {total:,})")

        counts = conn.execute(
            "SELECT count(*), count(DISTINCT offer_id), count(DISTINCT fetched_at) "
            "FROM offers_history"
        ).fetchone()

    print(f"\noffers_history now holds {counts[0]:,} rows, "
          f"{counts[1]:,} distinct departures, {counts[2]} observation times.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
