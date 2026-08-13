"""Natural-language search: free text -> {origin, dest, date, preference}.

    "cheapest way to Abuja next Friday"
        -> {origin: LOS, dest: ABV, date: 2026-09-18, preference: cheapest}

Two strategies behind one contract:

  * **Claude** (`claude-opus-5`) when ANTHROPIC_API_KEY is configured. Uses
    structured outputs, so the model is constrained to the schema rather than
    asked politely to emit JSON and hoped at.
  * **A deterministic rule parser** otherwise.

The fallback is not a stub. It handles the phrasings this project actually
sees, it needs no network, and it makes the test suite deterministic - a test
that depends on a live model is testing the model, not the code. Claude earns
its place on the phrasings rules cannot reach ("I need to be in Abuja before
my Friday meeting"), and both paths are held to the same contract test.
"""

from __future__ import annotations

import re
from datetime import date as date_type
from datetime import timedelta
from typing import Literal

from pydantic import BaseModel, Field

from common.config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL
from common.places import PLACES

Preference = Literal["cheapest", "fastest"]


class SearchQuery(BaseModel):
    """The structured form of a plain-English travel request."""

    origin: str = Field(description="3-letter place code, e.g. LOS")
    destination: str = Field(description="3-letter place code, e.g. ABV")
    date: str | None = Field(
        default=None, description="Departure date as YYYY-MM-DD, or null if unstated"
    )
    preference: Preference = Field(
        default="cheapest",
        description="'cheapest' unless the user asked for speed",
    )


class ParsedQuery(BaseModel):
    """What the endpoint returns: the query plus how it was worked out."""

    query: SearchQuery
    strategy: Literal["claude", "rules"]
    confident: bool = True
    note: str = ""


# --------------------------------------------------------------- vocabulary --

# Spellings a Nigerian traveller actually types, mapped to place codes.
PLACE_WORDS: dict[str, str] = {
    "lagos": "LOS", "eko": "LOS", "los": "LOS", "ikeja": "LOS",
    "abuja": "ABV", "abv": "ABV", "fct": "ABV",
    "onitsha": "ONI", "oni": "ONI",
    "port harcourt": "PHC", "portharcourt": "PHC", "phc": "PHC",
    "port-harcourt": "PHC", "ph": "PHC",
    "london": "LHR", "lhr": "LHR", "heathrow": "LHR", "uk": "LHR",
}

FASTEST_WORDS = (
    "fastest", "quickest", "quick", "fast", "soonest", "shortest",
    "in a hurry", "asap", "least time", "get there quick",
)
CHEAPEST_WORDS = (
    "cheapest", "cheap", "budget", "affordable", "least expensive",
    "lowest", "save money", "economical",
)

WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
    "mon": 0, "tue": 1, "tues": 1, "wed": 2, "thu": 3, "thur": 3,
    "thurs": 3, "fri": 4, "sat": 5, "sun": 6,
}

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12, "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


# ------------------------------------------------------------- rule parser --


def _find_places(text: str) -> list[tuple[int, str]]:
    """Every place mentioned, in the order it appears.

    Longest spellings are matched first so "port harcourt" is not read as the
    "ph" inside it, and matches are anchored on word boundaries so the "los"
    in "closest" is not a booking to Lagos.
    """
    found: list[tuple[int, str]] = []
    for word in sorted(PLACE_WORDS, key=len, reverse=True):
        for match in re.finditer(rf"\b{re.escape(word)}\b", text):
            if not any(abs(match.start() - pos) < len(word) for pos, _ in found):
                found.append((match.start(), PLACE_WORDS[word]))
    return sorted(found)


def _place_after(text: str, preposition: str) -> str | None:
    """The place named directly after 'from' or 'to', if any.

    Longest spellings first so 'to port harcourt' does not match the 'ph'
    inside it. A couple of filler words are tolerated: 'to the Abuja airport'.
    """
    words = "|".join(re.escape(w) for w in sorted(PLACE_WORDS, key=len, reverse=True))
    match = re.search(rf"\b{preposition}\s+(?:the\s+)?({words})\b", text)
    return PLACE_WORDS[match.group(1)] if match else None


def _parse_date(text: str, today: date_type) -> str | None:
    """Resolve a date phrase relative to `today`.

    `today` is a parameter, not `date.today()`, so tests are deterministic and
    a search made just before midnight cannot resolve differently mid-request.
    """
    if match := re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text):
        return match.group(0)

    # Longest phrase first: "day after tomorrow" contains "tomorrow", so
    # checking the short one first silently answers a day early.
    if "day after tomorrow" in text:
        return (today + timedelta(days=2)).isoformat()
    if "today" in text or "tonight" in text:
        return today.isoformat()
    if "tomorrow" in text:
        return (today + timedelta(days=1)).isoformat()

    # "next friday" is next week's Friday; a bare "friday" is the coming one.
    if match := re.search(r"\b(next|this|coming)?\s*(" + "|".join(WEEKDAYS) + r")\b", text):
        qualifier, day_word = match.group(1), match.group(2)
        ahead = (WEEKDAYS[day_word] - today.weekday()) % 7
        if ahead == 0:
            ahead = 7                      # "friday" said on a Friday means next one
        if qualifier == "next":
            ahead += 7 if ahead <= 6 else 0
        return (today + timedelta(days=ahead)).isoformat()

    # "14 September" / "September 14" / "on the 14th"
    month_names = "|".join(MONTHS)
    if match := re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({month_names})\b", text):
        day, month = int(match.group(1)), MONTHS[match.group(2)]
        return _resolve_calendar(today, month, day)
    if match := re.search(rf"\b({month_names})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", text):
        month, day = MONTHS[match.group(1)], int(match.group(2))
        return _resolve_calendar(today, month, day)
    if match := re.search(r"\bthe (\d{1,2})(?:st|nd|rd|th)\b", text):
        return _resolve_calendar(today, today.month, int(match.group(1)))

    if "next week" in text:
        return (today + timedelta(days=7)).isoformat()
    return None


def _resolve_calendar(today: date_type, month: int, day: int) -> str | None:
    """Assume the next occurrence: a date already past means next year."""
    for year in (today.year, today.year + 1):
        try:
            candidate = date_type(year, month, day)
        except ValueError:
            return None
        if candidate >= today:
            return candidate.isoformat()
    return None


def _parse_preference(text: str) -> Preference:
    """Cheapest unless speed was clearly asked for.

    Cheapest is the default because it is what most travellers on these routes
    actually want, and because guessing 'fastest' wrongly shows someone a
    flight costing four times what they meant to spend.
    """
    fastest_at = min((text.find(w) for w in FASTEST_WORDS if w in text), default=-1)
    cheapest_at = min((text.find(w) for w in CHEAPEST_WORDS if w in text), default=-1)

    if fastest_at >= 0 and cheapest_at >= 0:
        return "fastest" if fastest_at < cheapest_at else "cheapest"
    return "fastest" if fastest_at >= 0 else "cheapest"


def parse_with_rules(text: str, today: date_type) -> ParsedQuery:
    lowered = text.lower().strip()
    places = _find_places(lowered)
    notes: list[str] = []
    confident = True

    # Prepositions beat word order. "to Abuja from Lagos" names the
    # destination first, and an earlier version that compared string positions
    # got it backwards whenever the sentence began with "to".
    anchored_from = _place_after(lowered, "from")
    anchored_to = _place_after(lowered, "to")

    if anchored_from and anchored_to:
        origin, destination = anchored_from, anchored_to
    elif anchored_to:
        others = [code for _, code in places if code != anchored_to]
        origin, destination = (others[0] if others else "LOS"), anchored_to
    elif anchored_from and len(places) >= 2:
        others = [code for _, code in places if code != anchored_from]
        origin, destination = anchored_from, others[0]
    elif len(places) >= 2:
        origin, destination = places[0][1], places[1][1]
    elif len(places) == 1:
        # Every route in this project starts or ends in Lagos, so a single
        # named place is unambiguous: the other end is Lagos.
        only = places[0][1]
        origin, destination = ("LOS", only) if only != "LOS" else ("LOS", "")
        if not destination:
            confident = False
            notes.append("only Lagos was named, so the destination is unknown")
    else:
        confident = False
        notes.append("no place could be identified")
        origin, destination = "LOS", ""

    parsed_date = _parse_date(lowered, today)
    if parsed_date is None:
        notes.append("no date was given, so all upcoming departures are searched")

    return ParsedQuery(
        query=SearchQuery(
            origin=origin, destination=destination,
            date=parsed_date, preference=_parse_preference(lowered),
        ),
        strategy="rules",
        confident=confident and bool(destination),
        note="; ".join(notes),
    )


# ------------------------------------------------------------------ Claude --

SYSTEM_PROMPT = """\
You turn a traveller's plain-English request into a structured search for a \
Nigerian travel price comparison service.

Place codes: LOS (Lagos), ABV (Abuja), ONI (Onitsha), PHC (Port Harcourt), \
LHR (London). These are the only places available.

Rules:
- Every route starts or ends in Lagos. If the traveller names only one place, \
the other end is Lagos (LOS).
- If no origin is stated, assume the traveller is in Lagos.
- Resolve relative dates against the reference date given in the request. \
Return the date as YYYY-MM-DD, or null when no date was stated. Never invent \
a date the traveller did not imply.
- preference is "fastest" only when the traveller asked for speed. Otherwise \
"cheapest" - guessing "fastest" wrongly shows someone a flight costing several \
times what they meant to spend.
"""


def parse_with_claude(text: str, today: date_type) -> ParsedQuery:
    """Ask Claude for the structured form, constrained to the schema.

    Uses structured outputs rather than asking for JSON in prose, so the reply
    is schema-valid by construction instead of parsed hopefully and retried.
    """
    import anthropic

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.parse(
        model=ANTHROPIC_MODEL,
        # Generous: thinking is on by default on Opus 5 and max_tokens caps
        # thinking AND the reply together, so a tight limit truncates the
        # answer rather than saving anything meaningful on a request this size.
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": f"Reference date (today): {today.isoformat()}\n\nRequest: {text}",
        }],
        output_format=SearchQuery,
    )
    return ParsedQuery(query=response.parsed_output, strategy="claude")


# ----------------------------------------------------------------- public --


def parse_query(text: str, today: date_type | None = None,
                prefer_claude: bool = True) -> ParsedQuery:
    """Turn free text into a structured search.

    Falls back to rules if Claude is unavailable OR errors: a search box that
    stops working because a third-party API is down is worse than one that
    handles the common phrasings itself.
    """
    today = today or date_type.today()

    if prefer_claude and ANTHROPIC_API_KEY:
        try:
            return parse_with_claude(text, today)
        except Exception as exc:  # noqa: BLE001 - never fail the search box
            fallback = parse_with_rules(text, today)
            fallback.note = (
                f"Claude unavailable ({type(exc).__name__}), used rules instead. "
                + fallback.note
            ).strip("; ")
            return fallback

    return parse_with_rules(text, today)


def is_claude_available() -> bool:
    return bool(ANTHROPIC_API_KEY)


def validate(parsed: ParsedQuery) -> list[str]:
    """Problems that should stop the query reaching /search."""
    problems: list[str] = []
    query = parsed.query
    for label, code in (("origin", query.origin), ("destination", query.destination)):
        if not code:
            problems.append(f"no {label} could be identified")
        elif code not in PLACES:
            problems.append(f"{label} {code!r} is not a place this service covers")
    if query.origin and query.origin == query.destination:
        problems.append("origin and destination are the same place")
    return problems
