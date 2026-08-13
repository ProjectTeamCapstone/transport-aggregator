"""Create every Kafka topic the project needs.

Topics are created explicitly rather than auto-created, so a typo in a topic
name fails loudly instead of quietly creating 'raw.airpaece' and losing data
into it. Safe to re-run: existing topics are left alone.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from confluent_kafka.admin import AdminClient, NewTopic  # noqa: E402

from common.carriers import OFFERS_TOPIC, all_topics  # noqa: E402
from common.config import KAFKA_BOOTSTRAP_SERVERS  # noqa: E402

# Single-node stack, so replication is 1. Partitions: the raw feeds are small
# and ordering per carrier is useful, so 1 each. `offers` gets 3 so the sinks
# and any future consumer group can scale out.
PARTITIONS = {OFFERS_TOPIC: 3}
DEFAULT_PARTITIONS = 1
REPLICATION = 1


def main() -> int:
    admin = AdminClient({"bootstrap.servers": KAFKA_BOOTSTRAP_SERVERS})

    existing = set(admin.list_topics(timeout=15).topics)
    wanted = all_topics()
    missing = [t for t in wanted if t not in existing]

    if not missing:
        print(f"All {len(wanted)} topics already exist. Nothing to do.")
        return 0

    new_topics = [
        NewTopic(
            topic,
            num_partitions=PARTITIONS.get(topic, DEFAULT_PARTITIONS),
            replication_factor=REPLICATION,
        )
        for topic in missing
    ]

    failed = 0
    for topic, future in admin.create_topics(new_topics).items():
        try:
            future.result(timeout=30)
            print(f"  created {topic}")
        except Exception as exc:  # noqa: BLE001
            print(f"  FAILED  {topic}: {exc}")
            failed += 1

    print(f"\n{len(missing) - failed}/{len(missing)} topics created.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
