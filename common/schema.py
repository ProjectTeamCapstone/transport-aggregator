"""Canonical offer validation - the single gate every connector must pass.

Rule 3 requires ONE shared contract test that validates any offer message
against the canonical schema, and requires every connector to pass it. This
module is that gate. Connectors import `validate_offer`; the contract test
imports the same function. There is deliberately no second implementation.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from common.normalise import canonical_offer_id, to_utc_instant
from common.places import is_supported_route, supports_mode

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schemas" / "offer.schema.json"


class OfferContractError(ValueError):
    """Raised when a message does not satisfy the canonical offer contract."""


@lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return Draft202012Validator(schema, format_checker=FormatChecker())


def schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def validate_offer(offer: dict[str, Any]) -> None:
    """Validate one offer. Raises OfferContractError listing EVERY problem.

    Checks fall into three layers:
      1. JSON Schema  - types, required fields, ranges, patterns.
      2. Semantics    - things JSON Schema cannot express (route in scope,
                        mode possible at that place, timestamps ordered).
      3. Identity     - offer_id actually matches the offer it labels, so a
                        connector cannot ship a wrong or copy-pasted id.
    """
    if not isinstance(offer, dict):
        raise OfferContractError(f"offer must be a dict, got {type(offer).__name__}")

    problems = [
        f"{'/'.join(str(p) for p in e.path) or '<root>'}: {e.message}"
        for e in sorted(_validator().iter_errors(offer), key=lambda e: list(e.path))
    ]

    # Only run semantic checks once the shape is known-good, otherwise we
    # produce confusing cascading errors on an obviously malformed message.
    if not problems:
        problems.extend(_semantic_problems(offer))

    if problems:
        raise OfferContractError(
            f"offer failed the canonical contract ({len(problems)} problem(s)):\n  - "
            + "\n  - ".join(problems)
        )


def _semantic_problems(offer: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    origin, destination = offer["origin"], offer["destination"]
    mode = offer["mode"]

    if origin == destination:
        problems.append(f"origin and destination are both {origin!r}")
    elif not is_supported_route(origin, destination):
        problems.append(
            f"route {origin}->{destination} is not one of the four in-scope routes"
        )

    for code in (origin, destination):
        if not supports_mode(code, mode):
            problems.append(
                f"mode {mode!r} is impossible at {code} (no airport there)"
            )

    try:
        depart = to_utc_instant(offer["depart_time"])
        fetched = to_utc_instant(offer["fetched_at"])
        if depart <= fetched:
            problems.append(
                f"depart_time ({offer['depart_time']}) is not after fetched_at "
                f"({offer['fetched_at']}) - we cannot sell a seat on a departed service"
            )
    except (ValueError, TypeError) as exc:
        problems.append(f"timestamp problem: {exc}")

    try:
        expected = canonical_offer_id(
            offer["carrier"], mode, origin, destination, offer["depart_time"]
        )
        if offer["offer_id"] != expected:
            problems.append(
                f"offer_id {offer['offer_id']!r} does not match this offer's "
                f"content (expected {expected!r})"
            )
    except (ValueError, TypeError) as exc:
        problems.append(f"offer_id could not be recomputed: {exc}")

    return problems


def is_valid_offer(offer: dict[str, Any]) -> bool:
    try:
        validate_offer(offer)
        return True
    except OfferContractError:
        return False
