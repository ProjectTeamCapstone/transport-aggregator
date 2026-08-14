"""Per-IP rate limiting, counted in Redis, applied as middleware.

Two endpoints need this and the rest do not, for reasons worth keeping straight:

    POST /book        writes a PostgreSQL row BEFORE it does anything else.
                      That ordering is the double-booking guarantee and it is
                      not optional, so an unlimited /book is an unauthenticated
                      write to the ledger that nothing throttles.
    GET /search/nl    calls the Anthropic API when a key is configured. An
                      unlimited one spends our money at somebody else's pace.

    GET /search       gets only the generous default. It is a cached Redis read
                      and the thing the site exists to do; throttling it as
                      hard as /book would be protecting the wrong resource.

**Counters live in Redis, not in the process.** `uvicorn --workers 4` runs four
processes, so an in-memory limiter gives each its own allowance and the real
limit becomes four times whatever is configured - drifting with whichever worker
happens to answer. Redis is already a hard dependency of both limited endpoints
(each reads the offer from it), so this adds no new failure mode.

--------------------------------------------------------------------------
Why middleware rather than a decorator
--------------------------------------------------------------------------
The obvious implementation is slowapi's `@limiter.limit(...)` on each handler.
It does not survive this codebase: `api/main.py` uses `from __future__ import
annotations`, so every annotation is a string that FastAPI resolves against the
function's `__globals__`. A decorated handler's globals belong to the DECORATOR's
module, where `BookingRequest` does not exist - so FastAPI cannot resolve it,
silently demotes the request body to a query parameter, and POST /book answers
422 to every valid booking.

That failure is invisible in review and total in production, so the decorator is
avoided entirely. This module uses `limits` directly - the same engine slowapi
wraps - and leaves every handler signature untouched.
"""

from __future__ import annotations

from limits import RateLimitItem, parse
from limits.storage import storage_from_string
from limits.strategies import FixedWindowRateLimiter
from starlette.requests import Request

from common import logs
from common.config import (
    RATE_LIMIT_BOOK,
    RATE_LIMIT_DEFAULT,
    RATE_LIMIT_ENABLED,
    RATE_LIMIT_NL_SEARCH,
    redis_url,
)

log = logs.get(__name__)

# Endpoints with their own allowance. Everything else gets RATE_LIMIT_DEFAULT.
_ROUTE_LIMITS: dict[tuple[str, str], RateLimitItem] = {
    ("POST", "/book"): parse(RATE_LIMIT_BOOK),
    ("GET", "/search/nl"): parse(RATE_LIMIT_NL_SEARCH),
}
_DEFAULT_LIMIT = parse(RATE_LIMIT_DEFAULT)

# Paths that are never limited. /health is polled by Caddy and by whoever is
# watching a deploy; rate limiting the thing that reports an outage is how an
# outage becomes invisible.
_EXEMPT = frozenset({"/health", "/docs", "/openapi.json", "/redoc"})


class RateLimiter:
    """Counts requests per client per endpoint against a shared store."""

    def __init__(self, storage_uri: str | None = None, enabled: bool | None = None):
        self.enabled = RATE_LIMIT_ENABLED if enabled is None else enabled
        self._storage_uri = storage_uri or redis_url()
        self._limiter: FixedWindowRateLimiter | None = None

    @property
    def limiter(self) -> FixedWindowRateLimiter:
        # Built on first use, not at import: constructing the Redis storage
        # connects, and importing this module must not require a running Redis.
        if self._limiter is None:
            self._limiter = FixedWindowRateLimiter(
                storage_from_string(self._storage_uri)
            )
        return self._limiter

    def limit_for(self, method: str, path: str) -> RateLimitItem:
        return _ROUTE_LIMITS.get((method, path), _DEFAULT_LIMIT)

    def check(self, method: str, path: str, client: str) -> RateLimitItem | None:
        """The exceeded limit, or None when the request may proceed.

        A store that cannot be reached lets the request THROUGH. Both limited
        endpoints need Redis for their own work, so they will fail on their own
        terms a moment later with a message about the actual problem - which is
        more useful than a 429 blaming the caller for our outage.
        """
        if not self.enabled or path in _EXEMPT:
            return None

        item = self.limit_for(method, path)
        try:
            if self.limiter.hit(item, client, path):
                return None
        except Exception as exc:  # noqa: BLE001 - any store failure, same answer
            log.error("ratelimit.store_unavailable", extra={"error": str(exc)})
            return None
        return item


def client_key(request: Request) -> str:
    """Who to count against.

    Caddy is the only thing facing the internet and it proxies to 127.0.0.1, so
    the peer address alone would be the proxy on every request - rate limiting
    the entire internet as a single client. X-Forwarded-For's FIRST entry is the
    original caller.

    Trusting that header is only safe because nothing but Caddy can reach
    uvicorn: docker-compose binds the API to 127.0.0.1, so a client cannot set
    the header itself and reach us directly. Exposing the API without a proxy in
    front would make this spoofable and it would have to go back to the peer
    address.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


limiter = RateLimiter()
