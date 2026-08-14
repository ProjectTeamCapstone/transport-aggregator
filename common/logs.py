"""Structured logging, configured once, emitted as JSON.

This project's booking design rests on being able to say what happened - the
ledger records the OUTCOME of every attempt, and `unknown` exists precisely so
that ignorance is written down rather than guessed at. But the ledger records
one row per booking, not the sequence that produced it. When a booking on the
deployed box lands in `unknown`, the row says "we asked and never heard back";
it cannot say which attempt timed out, how long the carrier took, or whether the
two attempts before it failed the same way.

That is what these logs are for, and it is why they are JSON rather than
sentences: the questions worth asking of them are aggregate ones ("how many
bookings reached `unknown` this week, by carrier"), and those are a `jq` filter
over structured records and an unparseable mess over prose.

Two rules the whole codebase follows:

**Never log passenger details.** A traveller's name and email are needed to make
a booking and are needed nowhere else. Logs get the idempotency key, which
identifies the booking without identifying the person. `booking/providers.py`
keeps the same rule for deep-link URLs, for the same reason.

**Never log secrets.** No DSN (it carries the database password), no API token.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any

# Fields already on every LogRecord. Anything a caller passes via `extra=`
# that is NOT in here is ours, and gets serialised into the JSON payload.
_BUILTIN = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__
) | {"asctime", "message", "taskName"}


class JsonFormatter(logging.Formatter):
    """One JSON object per line.

    Line-delimited rather than a JSON array so the file stays appendable and
    `tail -f | jq` works while the process is still running.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime(
                "%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)
            ) + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }

        # Whatever the call site attached via extra={...}.
        for key, value in record.__dict__.items():
            if key not in _BUILTIN:
                payload[key] = value

        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info)

        # default=str so a Decimal price or a datetime never turns a log call
        # into an exception. Logging must not be able to break the request it
        # is describing.
        return json.dumps(payload, default=str)


def configure(level: str | None = None) -> None:
    """Install the JSON handler on the root logger. Safe to call twice.

    Called from `api/main.py` at import, which covers every uvicorn worker. A
    script that wants logs (`python -m simulator.run`) calls it itself; one that
    does not simply never configures logging and stays silent.
    """
    root = logging.getLogger()
    if any(getattr(h, "_naijafares", False) for h in root.handlers):
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler._naijafares = True  # type: ignore[attr-defined]

    root.addHandler(handler)
    root.setLevel(level or os.getenv("LOG_LEVEL", "INFO").upper())

    # uvicorn installs its own colourised handlers. Left alone they double every
    # access line: once as JSON, once as prose. Ours is the one that can be
    # parsed, so theirs propagate into it instead of formatting themselves.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True


def get(name: str) -> logging.Logger:
    """A named logger. Use the module's `__name__`."""
    return logging.getLogger(name)
