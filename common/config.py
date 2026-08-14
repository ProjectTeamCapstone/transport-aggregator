"""Central configuration. Reads .env, never hardcodes secrets."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


# --- infrastructure ---------------------------------------------------------
KAFKA_BOOTSTRAP_SERVERS = _env("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
KSQLDB_URL = _env("KSQLDB_URL", "http://localhost:8088")
REDIS_HOST = _env("REDIS_HOST", "localhost")
REDIS_PORT = int(_env("REDIS_PORT", "6379"))

POSTGRES_USER = _env("POSTGRES_USER", "naijafares")
POSTGRES_PASSWORD = _env("POSTGRES_PASSWORD", "naijafares_dev")
POSTGRES_DB = _env("POSTGRES_DB", "naijafares")
POSTGRES_HOST = _env("POSTGRES_HOST", "localhost")
POSTGRES_PORT = int(_env("POSTGRES_PORT", "5432"))


def postgres_dsn() -> str:
    return (
        f"postgresql://{POSTGRES_USER}:{POSTGRES_PASSWORD}"
        f"@{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}"
    )


# Connection pool bounds, PER PROCESS. `uvicorn --workers 4` means four pools,
# so the ceiling against PostgreSQL is 4 x POSTGRES_POOL_MAX - keep the product
# well under the server's `max_connections` (100 by default). See common/db.py.
POSTGRES_POOL_MIN = int(_env("POSTGRES_POOL_MIN", "1"))
POSTGRES_POOL_MAX = int(_env("POSTGRES_POOL_MAX", "8"))
POSTGRES_CONNECT_TIMEOUT_SECONDS = float(_env("POSTGRES_CONNECT_TIMEOUT_SECONDS", "10"))


def redis_url() -> str:
    return f"redis://{REDIS_HOST}:{REDIS_PORT}"


# --- external APIs ----------------------------------------------------------
RAPIDAPI_KEY = _env("RAPIDAPI_KEY")
RAPIDAPI_HOST = _env("RAPIDAPI_HOST", "sky-scrapper.p.rapidapi.com")
TRAVELPAYOUTS_TOKEN = _env("TRAVELPAYOUTS_TOKEN")
ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = _env("ANTHROPIC_MODEL", "claude-sonnet-5")

# --- booking ----------------------------------------------------------------
DUFFEL_API_TOKEN = _env("DUFFEL_API_TOKEN")
DUFFEL_API_VERSION = _env("DUFFEL_API_VERSION", "v2")


# Up to three attempts at the carrier, with the wait doubling each time
# (0.5s, then 1.0s). Only failures that provably created nothing are retried -
# see booking/providers.py for why a timeout is not one of them.
BOOKING_MAX_ATTEMPTS = int(_env("BOOKING_MAX_ATTEMPTS", "3"))
BOOKING_BACKOFF_BASE_SECONDS = float(_env("BOOKING_BACKOFF_BASE_SECONDS", "0.5"))

# How far the live price may have drifted above the quoted one before we refuse
# to book. A traveller agreed to a number; booking them onto a materially
# higher one without asking is not ours to do. A price DROP never blocks.
BOOKING_PRICE_TOLERANCE_PCT = float(_env("BOOKING_PRICE_TOLERANCE_PCT", "1.0"))


def assert_sandbox_only() -> None:
    """Hard stop if a LIVE Duffel token is ever configured.

    Payment processing is explicitly out of scope. A live token in this project
    could create a real, chargeable booking. Failing loudly at startup is the
    only acceptable behaviour.
    """
    if DUFFEL_API_TOKEN and not DUFFEL_API_TOKEN.startswith("duffel_test_"):
        raise RuntimeError(
            "DUFFEL_API_TOKEN is not a sandbox token (must start with "
            "'duffel_test_'). This project must never touch a live booking API."
        )


# --- FX ---------------------------------------------------------------------
FX_API_URL = _env("FX_API_URL", "https://api.exchangerate.host/latest")
FX_FALLBACK_GBP_NGN = float(_env("FX_FALLBACK_GBP_NGN", "2050.0"))
FX_CACHE_TTL_SECONDS = int(_env("FX_CACHE_TTL_SECONDS", "3600"))

# --- simulator --------------------------------------------------------------
SIMULATOR_SEED = int(_env("SIMULATOR_SEED", "608"))
SIMULATOR_TICK_SECONDS = int(_env("SIMULATOR_TICK_SECONDS", "300"))

# --- search cache -----------------------------------------------------------
# How long the API holds a route's offers in memory before re-reading Redis.
# Prices only change when the pipeline delivers new ones (every SIMULATOR_TICK_
# SECONDS), so a few seconds costs nothing in freshness and is the difference
# between meeting the 100ms search target and missing it sevenfold - see
# api/store.for_route(). Set to 0 to disable caching entirely.
ROUTE_CACHE_TTL_SECONDS = float(_env("ROUTE_CACHE_TTL_SECONDS", "5"))

# --- rate limits ------------------------------------------------------------
# Per client IP. Counters live in Redis, not in the process, because
# `--workers 4` would otherwise mean four independent allowances and an
# effective limit four times whatever is configured here.
#
# The two limited endpoints are limited for different reasons. /book writes a
# PostgreSQL row per request before it does anything else, so an unlimited /book
# is an unauthenticated write to the ledger. /search/nl can call the Anthropic
# API, so an unlimited one spends money on our key. /search is not limited: it
# is a cached Redis read and the endpoint the site exists to serve.
RATE_LIMIT_ENABLED = _env("RATE_LIMIT_ENABLED", "true").lower() != "false"
RATE_LIMIT_BOOK = _env("RATE_LIMIT_BOOK", "10/minute")
RATE_LIMIT_NL_SEARCH = _env("RATE_LIMIT_NL_SEARCH", "20/minute")
RATE_LIMIT_DEFAULT = _env("RATE_LIMIT_DEFAULT", "120/minute")

# --- staleness --------------------------------------------------------------
# An offer older than this is served with stale=true rather than hidden. Users
# would rather see a price from 20 minutes ago, clearly labelled, than nothing.
STALE_AFTER_SECONDS = int(_env("STALE_AFTER_SECONDS", "900"))


@lru_cache(maxsize=1)
def has_live_credentials() -> dict[str, bool]:
    """Which live sources are actually usable in this environment."""
    return {
        "rapidapi": bool(RAPIDAPI_KEY),
        "travelpayouts": bool(TRAVELPAYOUTS_TOKEN),
        "duffel": bool(DUFFEL_API_TOKEN),
        "anthropic": bool(ANTHROPIC_API_KEY),
    }
