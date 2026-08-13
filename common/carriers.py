"""Carrier registry - the eight operators in scope.

`access` is filled in from the Phase 0 audit (scripts/audit_sources.py) and is
the authority for how each carrier's data is obtained. Anything not confirmed
live is served by the price simulator, which is a first-class component here,
not a fallback hack.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Mode = Literal["road", "air"]
# poll     = request an API built for machines
# scrape   = extract from pages built for humans (only where robots.txt allows)
# simulate = price simulator drives this carrier
Access = Literal["poll", "scrape", "simulate", "undecided"]


@dataclass(frozen=True)
class Carrier:
    key: str            # kafka-topic-safe, e.g. 'airpeace' -> raw.airpeace
    name: str           # display name used in the canonical `carrier` field
    mode: Mode
    domain: str
    routes: list[tuple[str, str]]
    # PLANNED access method, decided by the Phase 0 audit. What actually runs
    # may be lower - see effective_access(), which downgrades to the simulator
    # when a source needs credentials we do not have.
    access: Access = "undecided"
    # Which credential this carrier's live path depends on, if any.
    requires_credential: str = ""
    refresh_seconds: int = 300
    notes: str = ""
    robots_paths: list[str] = field(default_factory=list)  # paths to test in the audit


# Nigerian domestic road routes. Onitsha and Port Harcourt are road-served;
# every road route in scope originates in Lagos.
_ROAD_ROUTES = [("LOS", "ABV"), ("LOS", "ONI"), ("LOS", "PHC")]
_DOMESTIC_AIR = [("LOS", "ABV"), ("LOS", "PHC")]

CARRIERS: list[Carrier] = [
    # ------------------------------------------------------------ road ----
    Carrier(
        key="gigm",
        access="scrape",   # Playwright: gigm.com is a React app, robots.txt allows /
        name="GIGM",
        mode="road",
        domain="gigm.com",
        routes=_ROAD_ROUTES,
        refresh_seconds=900,
        robots_paths=["/", "/booking", "/trip-search"],
        notes="Largest Nigerian interstate operator. Booking flow is JS-heavy.",
    ),
    Carrier(
        key="abctransport",
        access="scrape",   # Scrapy: server-rendered HTML, robots.txt allows /
        name="ABC Transport",
        mode="road",
        domain="abctransport.com",
        routes=_ROAD_ROUTES,
        refresh_seconds=900,
        robots_paths=["/", "/booking"],
        notes="Listed operator; long-running interstate coach service.",
    ),
    Carrier(
        key="crosscountry",
        access="simulate", # no resolvable domain found (5 candidates probed)
        name="Cross Country",
        mode="road",
        domain="crosscountrylimited.com",
        routes=[("LOS", "ABV"), ("LOS", "ONI")],
        refresh_seconds=900,
        robots_paths=["/", "/booking"],
    ),
    Carrier(
        key="chisco",
        access="scrape",   # Scrapy: server-rendered HTML, robots.txt allows /
        name="CHISCO",
        mode="road",
        domain="chiscotransport.com",
        routes=[("LOS", "ONI"), ("LOS", "PHC")],
        refresh_seconds=900,
        robots_paths=["/", "/booking"],
        notes="Strong on the south-east corridor, hence Onitsha.",
    ),
    # ------------------------------------------------------------- air ----
    Carrier(
        key="airpeace",
        access="poll",     # via Sky Scrapper / Travelpayouts aggregator API
        requires_credential="RAPIDAPI_KEY",
        name="Air Peace",
        mode="air",
        domain="flyairpeace.com",
        routes=_DOMESTIC_AIR,
        refresh_seconds=300,
        robots_paths=["/", "/booking", "/flight-search"],
        notes="Template connector - built end-to-end first, per Phase 1.",
    ),
    Carrier(
        key="ibomair",
        access="poll",     # via aggregator API
        requires_credential="RAPIDAPI_KEY",
        name="Ibom Air",
        mode="air",
        domain="ibomair.com",
        routes=_DOMESTIC_AIR,
        refresh_seconds=300,
        robots_paths=["/", "/booking"],
    ),
    Carrier(
        key="arik",
        access="poll",     # arikair.com serves no robots.txt -> never scraped
        requires_credential="RAPIDAPI_KEY",
        name="Arik Air",
        mode="air",
        domain="arikair.com",
        routes=_DOMESTIC_AIR,
        refresh_seconds=300,
        robots_paths=["/", "/booking"],
    ),
    Carrier(
        key="britishairways",
        access="poll",     # BA blocks bots -> aggregator API only, never scraped
        requires_credential="RAPIDAPI_KEY",
        name="British Airways",
        mode="air",
        domain="britishairways.com",
        routes=[("LOS", "LHR")],
        refresh_seconds=600,
        robots_paths=["/", "/travel/book/execclub/_gf/en_gb"],
        notes="Only carrier on the London route; fares quoted in GBP, so the "
        "FX conversion path is exercised here.",
    ),
]

CARRIERS_BY_KEY = {c.key: c for c in CARRIERS}
CARRIERS_BY_NAME = {c.name: c for c in CARRIERS}


def raw_topic(carrier_key: str) -> str:
    """One Kafka topic per carrier, e.g. raw.airpeace."""
    if carrier_key not in CARRIERS_BY_KEY:
        raise KeyError(f"unknown carrier {carrier_key!r}")
    return f"raw.{carrier_key}"


OFFERS_TOPIC = "offers"


def all_topics() -> list[str]:
    return [raw_topic(c.key) for c in CARRIERS] + [OFFERS_TOPIC]


def effective_access(carrier: Carrier) -> Access:
    """What this carrier will ACTUALLY use right now.

    A carrier planned as 'poll' still needs an API key. Without it the honest
    answer is 'simulate' - and saying so out loud beats a connector that starts,
    silently fetches nothing, and leaves an empty topic nobody notices until the
    demo.
    """
    import os

    if carrier.requires_credential and not os.getenv(carrier.requires_credential, "").strip():
        return "simulate"
    return carrier.access


def access_summary() -> dict[str, list[str]]:
    """Carrier names grouped by what they will actually do, for the report."""
    summary: dict[str, list[str]] = {}
    for carrier in CARRIERS:
        summary.setdefault(effective_access(carrier), []).append(carrier.name)
    return summary
