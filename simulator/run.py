"""Generates simulated carrier feeds and publishes them to the raw.* topics.

Usage:
    python -m simulator.run --once          # one full sweep, then exit
    python -m simulator.run                 # sweep every SIMULATOR_TICK_SECONDS
    python -m simulator.run --once --dry-run --sample 3

Each sweep publishes every departure currently on sale for every carrier, in
that carrier's own raw format. Re-running is safe: prices are a pure function
of the observation time, so a restart continues the history rather than
breaking it.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.carriers import CARRIERS, Carrier, raw_topic  # noqa: E402
from common.config import (  # noqa: E402
    FX_FALLBACK_GBP_NGN,
    KAFKA_BOOTSTRAP_SERVERS,
    SIMULATOR_SEED,
    SIMULATOR_TICK_SECONDS,
)
from common.places import get_place  # noqa: E402
from simulator.pricing import (  # noqa: E402
    duration_minutes,
    seats_remaining,
    simulate_price_ngn,
    upcoming_departures,
)
from simulator.raw_formats import BUILDERS  # noqa: E402


def _directions(carrier: Carrier) -> list[tuple[str, str]]:
    """Carriers run both ways; the registry lists each route once."""
    pairs: list[tuple[str, str]] = []
    for origin, destination in carrier.routes:
        pairs.append((origin, destination))
        pairs.append((destination, origin))
    return pairs


def generate_for_carrier(
    carrier: Carrier, now: datetime, seed: int, gbp_ngn_rate: float
) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield (message_key, raw_message) for every departure on sale.

    The key is stable across sweeps for a given departure, so Kafka log
    compaction and downstream dedup both behave sensibly.
    """
    builder = BUILDERS[carrier.key]

    for origin, destination in _directions(carrier):
        tz_name = get_place(origin).timezone
        scheduled = duration_minutes(carrier.mode, origin, destination)

        for depart in upcoming_departures(carrier.key, carrier.mode, now, tz_name):
            seats = seats_remaining(carrier.name, depart, now, carrier.mode, seed)
            price = simulate_price_ngn(
                carrier.name, carrier.mode, origin, destination,
                depart, now, seed, seats_left=seats,
            )

            kwargs: dict[str, Any] = dict(
                depart=depart, seats=seats, origin=origin,
                destination=destination, duration_min=scheduled, now=now,
            )
            # British Airways quotes GBP, so it needs the rate to convert back.
            if carrier.key == "britishairways":
                kwargs["price_ngn"] = price
                kwargs["gbp_ngn_rate"] = gbp_ngn_rate
            else:
                kwargs["price"] = price

            key = f"{carrier.name}|{origin}|{destination}|{depart.isoformat()}"
            yield key, builder(**kwargs)


def generate_all(
    now: datetime | None = None, seed: int = SIMULATOR_SEED,
    gbp_ngn_rate: float = FX_FALLBACK_GBP_NGN,
) -> Iterator[tuple[str, str, dict[str, Any]]]:
    """Yield (topic, key, raw_message) across every carrier."""
    now = now or datetime.now(timezone.utc)
    for carrier in CARRIERS:
        for key, message in generate_for_carrier(carrier, now, seed, gbp_ngn_rate):
            yield raw_topic(carrier.key), key, message


def sweep(producer, now: datetime, seed: int, rate: float) -> dict[str, int]:
    counts: dict[str, int] = {}
    for topic, key, message in generate_all(now, seed, rate):
        producer.produce(
            topic, key=key.encode("utf-8"),
            value=json.dumps(message, separators=(",", ":")).encode("utf-8"),
        )
        counts[topic] = counts.get(topic, 0) + 1
    unsent = producer.flush(timeout=60)
    if unsent:
        raise RuntimeError(f"{unsent} message(s) failed to reach Kafka")
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description="Price simulator")
    parser.add_argument("--once", action="store_true", help="one sweep, then exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="print messages instead of publishing")
    parser.add_argument("--sample", type=int, default=0,
                        help="with --dry-run, print N messages per carrier")
    parser.add_argument("--seed", type=int, default=SIMULATOR_SEED)
    args = parser.parse_args()

    if args.dry_run:
        seen: dict[str, int] = {}
        for topic, key, message in generate_all(seed=args.seed):
            seen[topic] = seen.get(topic, 0) + 1
            if args.sample and seen[topic] <= args.sample:
                print(f"\n--- {topic}  key={key}")
                print(json.dumps(message, indent=2))
        print(f"\nWould publish {sum(seen.values())} messages across "
              f"{len(seen)} topics:")
        for topic in sorted(seen):
            print(f"  {topic:<24} {seen[topic]:>5}")
        return 0

    from confluent_kafka import Producer

    producer = Producer({
        "bootstrap.servers": KAFKA_BOOTSTRAP_SERVERS,
        "acks": "all",
        "linger.ms": 50,           # small batching window; this is bursty traffic
        "compression.type": "lz4",
        "queue.buffering.max.messages": 200_000,
    })

    while True:
        started = time.time()
        now = datetime.now(timezone.utc)
        counts = sweep(producer, now, args.seed, FX_FALLBACK_GBP_NGN)
        total = sum(counts.values())
        print(f"[{now.isoformat(timespec='seconds')}] published {total} messages "
              f"in {time.time() - started:.1f}s across {len(counts)} topics")

        if args.once:
            for topic in sorted(counts):
                print(f"  {topic:<24} {counts[topic]:>5}")
            return 0
        time.sleep(SIMULATOR_TICK_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
