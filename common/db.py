"""One PostgreSQL connection pool per process.

Every ledger operation used to open its own connection. That reads harmlessly
until you count them on the booking path: `claim` opens one, each retry
attempt's `attempts=` write opens another, and the final state write opens a
last one - six TCP connects, six authentication handshakes, for a single
booking that may itself have taken 50 ms of carrier time.

The arithmetic is the problem. `uvicorn --workers 4` runs four processes, each
with FastAPI's 40-thread pool for sync endpoints, so 160 requests can be in
flight against a PostgreSQL whose default `max_connections` is 100. Connection
exhaustion there does not degrade gracefully: it fails `claim`, which is the one
call the booking safety argument depends on happening BEFORE any carrier call.

So connections are pooled and bounded. `max_size` is deliberately small - the
work per query is microseconds and the pool is per-process, so four workers hold
at most 4 x POSTGRES_POOL_MAX connections between them, comfortably inside any
sane `max_connections`.

The pool opens lazily on first use rather than at import, because importing this
module must never require a running database - `python -m ml.train` and the test
collector both import modules that reach this one.
"""

from __future__ import annotations

import atexit
import threading
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout

from common.config import (
    POSTGRES_CONNECT_TIMEOUT_SECONDS,
    POSTGRES_POOL_MAX,
    POSTGRES_POOL_MIN,
    postgres_dsn,
)

_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()


def pool() -> ConnectionPool:
    """The process-wide pool, created on first use."""
    global _pool
    if _pool is None:
        # Double-checked under a lock: two threads hitting a cold pool must not
        # each build one, or the second silently leaks the first's connections.
        with _pool_lock:
            if _pool is None:
                _pool = ConnectionPool(
                    conninfo=postgres_dsn(),
                    min_size=POSTGRES_POOL_MIN,
                    max_size=POSTGRES_POOL_MAX,
                    timeout=POSTGRES_CONNECT_TIMEOUT_SECONDS,
                    kwargs={"row_factory": dict_row},
                    open=True,
                    # Hand back a connection only if it still works. A pooled
                    # connection can be killed by a database restart or an idle
                    # timeout while it sits in the pool, and discovering that
                    # during `claim` would fail a booking for no reason.
                    check=ConnectionPool.check_connection,
                    name="naijafares",
                )
                atexit.register(_close)
    return _pool


@contextmanager
def connection() -> Iterator[psycopg.Connection]:
    """A pooled connection, committed on clean exit and rolled back on error.

    Drop-in for `with psycopg.connect(...) as conn`, with one difference worth
    knowing: leaving this block returns the connection to the pool rather than
    closing it, so nothing may hold a cursor or a server-side result past it.

    A pool that cannot be filled raises `psycopg.OperationalError`, the same
    exception a direct connect would raise, so callers that already handle an
    unreachable database - `POST /book` returns 503 - keep working unchanged.
    """
    try:
        with pool().connection() as conn:
            yield conn
    except PoolTimeout as exc:
        # Every connection is busy. That is an unreachable database from the
        # caller's point of view, so it is reported as one rather than as a
        # pool-specific exception no caller knows to catch.
        raise psycopg.OperationalError(
            f"no PostgreSQL connection available within "
            f"{POSTGRES_CONNECT_TIMEOUT_SECONDS}s (pool of {POSTGRES_POOL_MAX} "
            f"exhausted)"
        ) from exc


def _close() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def reset() -> None:
    """Drop the pool. Tests use this after pointing the DSN somewhere else."""
    _close()
