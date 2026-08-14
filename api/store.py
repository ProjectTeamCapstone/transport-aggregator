"""Redis access for the search API.

Redis holds exactly one hash per offer, keyed `offers:<offer_id>`, written by
the Kafka Connect sink. Each write overwrites the previous observation of that
departure, so what is in Redis is by definition the latest known price.

Values come back as strings because that is what a Redis hash stores, so
everything is coerced back to canonical types here - once, in one place, rather
than in every caller.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Iterator

import redis

from common.config import REDIS_HOST, REDIS_PORT, ROUTE_CACHE_TTL_SECONDS

KEY_PREFIX = "offers"

# How long the /health offer count may go without being re-measured. Longer
# than the route cache because the number is diagnostic, not a search result,
# and measuring it costs a full keyspace scan. See count_cached().
COUNT_TTL_SECONDS = 30.0

# Canonical types. Redis hands back strings; the contract expects numbers.
_INT_FIELDS = ("duration_min", "seats_left")
_FLOAT_FIELDS = ("price_ngn",)


class OfferStore:
    def __init__(self, host: str = REDIS_HOST, port: int = REDIS_PORT,
                 cache_ttl_seconds: float = ROUTE_CACHE_TTL_SECONDS):
        self._redis = redis.Redis(
            host=host, port=port, socket_timeout=5, decode_responses=True
        )
        # route -> (read_at_monotonic, offers). See for_route() for why.
        self._cache_ttl = cache_ttl_seconds
        self._cache: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}
        # (read_at_monotonic, {"count": int, "measured_at": iso}). See count_cached().
        self._count: tuple[float, dict[str, Any]] | None = None

    def ping(self) -> bool:
        try:
            return bool(self._redis.ping())
        except redis.RedisError:
            return False

    @staticmethod
    def key_for(offer_id: str) -> str:
        return f"{KEY_PREFIX}:{offer_id}"

    @staticmethod
    def _coerce(raw: dict[str, str]) -> dict[str, Any]:
        offer = dict(raw)
        for field in _INT_FIELDS:
            if field in offer:
                offer[field] = int(float(offer[field]))
        for field in _FLOAT_FIELDS:
            if field in offer:
                offer[field] = float(offer[field])
        return offer

    def get(self, offer_id: str) -> dict[str, Any] | None:
        raw = self._redis.hgetall(self.key_for(offer_id))
        return self._coerce(raw) if raw else None

    def _scan(self, pattern: str) -> Iterator[str]:
        return self._redis.scan_iter(match=pattern, count=2000)

    def for_route(self, origin: str, destination: str) -> list[dict[str, Any]]:
        """Every current offer on a route, briefly cached in this process.

        Reading a route costs Redis about 19 ms - one scan plus a few hundred
        hash reads - and Redis handles one command at a time. Load testing showed
        exactly what that implies: twenty concurrent searches queue behind each
        other and the 95th percentile lands near 700 ms, seven times over the
        100 ms target. Redis was not slow; it was being asked the same question
        forty-eight times a second.

        So the answer is held for a few seconds. Prices only change when the
        pipeline delivers new ones, which is every five minutes, so a
        five-second cache serves data at most five seconds older than Redis.

        Crucially this does NOT soften the staleness reporting, which is what
        makes it safe: `stale` and `age_seconds` are computed from the
        `fetched_at` carried inside each record, so a cached offer still reports
        its true age. A dead connector is just as visible through the cache as
        without it.
        """
        route = (origin, destination)
        now = time.monotonic()

        cached = self._cache.get(route)
        if cached is not None and now - cached[0] < self._cache_ttl:
            # A copy, so a caller filtering the list cannot edit the cache. The
            # offers themselves are only ever read.
            return list(cached[1])

        offers = self._read_route(origin, destination)
        # No lock needed: a race here costs one duplicated read, never a wrong
        # answer, and a dict assignment cannot be torn.
        self._cache[route] = (now, offers)
        return list(offers)

    def _read_route(self, origin: str, destination: str) -> list[dict[str, Any]]:
        """Read a route straight from Redis.

        The offer_id encodes the route (`gigm-road-LOS-ABV-...`), so the scan
        pattern narrows to that route server-side instead of pulling every key
        and filtering here.

        Date is deliberately NOT part of the pattern. The id carries the
        departure in UTC while a traveller searches by local date, so an
        overnight departure would fall on the wrong side of midnight. Dates are
        filtered in Python against depart_time, which carries the real offset.
        """
        keys = list(self._scan(f"{KEY_PREFIX}:*-{origin}-{destination}-*"))
        if not keys:
            return []

        pipe = self._redis.pipeline()
        for key in keys:
            pipe.hgetall(key)
        return [self._coerce(raw) for raw in pipe.execute() if raw]

    def clear_cache(self) -> None:
        """Forget every cached route.

        Needed by tests that write straight into Redis and then search: without
        this they would race the cache window and fail intermittently, which is
        worse than failing outright.
        """
        self._cache.clear()
        self._count = None

    def count(self) -> int:
        """Every offer key in Redis. O(keyspace) - see count_cached().

        Kept because it is the only exact answer available: the Kafka Connect
        sink writes the hashes, so nothing on this side is told when an offer
        appears or expires, and there is no counter to read instead.
        """
        return sum(1 for _ in self._scan(f"{KEY_PREFIX}:*"))

    def count_cached(self) -> dict[str, Any] | None:
        """The offer count, re-measured at most once per COUNT_TTL_SECONDS.

        `/health` is polled on an interval, and count() is a full keyspace SCAN.
        Answering every poll exactly would mean sweeping the whole keyspace
        every few seconds - a steady, self-inflicted load on the datastore that
        the health check exists to reassure you about, and one that grows with
        the data rather than staying flat.

        The measurement timestamp is returned with the number, because a cached
        count without one cannot be told apart from a live one. A caller can see
        the figure is 40 seconds old and judge it accordingly.

        Returns None only when Redis cannot be read at all, which /health
        already reports through `redis: down`.
        """
        now = time.monotonic()
        if self._count is not None and now - self._count[0] < COUNT_TTL_SECONDS:
            return self._count[1]

        try:
            measured = {
                "count": self.count(),
                "measured_at": datetime.now(timezone.utc)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z"),
            }
        except redis.RedisError:
            return None

        self._count = (now, measured)
        return measured
