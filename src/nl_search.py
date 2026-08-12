"""
Natural-language query parser for search, e.g.
  "cheapest Lagos to Abuja tomorrow morning"
  "fastest flight from Lagos to London next week"

In production this is where Anthropic Claude is called to turn free text into a
structured query. To keep the prototype fully offline and dependency-free, we
ship a rule-based parser with the SAME output contract Claude would return:

    {origin, destination, sort, mode, date, part_of_day}

If ANTHROPIC_API_KEY is set and the `anthropic` package is installed, set
USE_CLAUDE=1 to route parsing through Claude instead (see parse_with_claude).
"""
from __future__ import annotations
import os
import re
from datetime import date, timedelta

CITY_ALIASES = {
    "lagos": "LOS", "los": "LOS",
    "abuja": "ABV", "abv": "ABV",
    "onitsha": "ONI", "oni": "ONI",
    "port harcourt": "PHC", "portharcourt": "PHC", "phc": "PHC", "port-harcourt": "PHC",
    "london": "LON", "lon": "LON",
}
PART_OF_DAY = {
    "morning": (5, 11), "afternoon": (12, 16),
    "evening": (17, 20), "night": (21, 23),
}


def _find_cities(text: str):
    hits = []
    for name, code in sorted(CITY_ALIASES.items(), key=lambda kv: -len(kv[0])):
        for m in re.finditer(r"\b" + re.escape(name) + r"\b", text):
            hits.append((m.start(), code))
    hits.sort()
    # dedupe consecutive same codes
    ordered = []
    for _, code in hits:
        if not ordered or ordered[-1] != code:
            ordered.append(code)
    return ordered


def _find_date(text: str):
    today = date.today()
    if "day after tomorrow" in text:
        return today + timedelta(days=2)
    if "tomorrow" in text:
        return today + timedelta(days=1)
    if "today" in text:
        return today
    if "next week" in text:
        return today + timedelta(days=7)
    if "this weekend" in text or "weekend" in text:
        return today + timedelta(days=(5 - today.weekday()) % 7)
    m = re.search(r"in (\d+) days", text)
    if m:
        return today + timedelta(days=int(m.group(1)))
    m = re.search(r"(\d{4}-\d{2}-\d{2})", text)
    if m:
        return date.fromisoformat(m.group(1))
    return None


def parse(text: str) -> dict:
    t = text.lower().strip()
    cities = _find_cities(t)
    origin = cities[0] if len(cities) >= 1 else None
    destination = cities[1] if len(cities) >= 2 else None
    # "from X to Y" ordering safety
    fm = re.search(r"from\s+([a-z\- ]+?)\s+to\s+([a-z\- ]+)", t)
    if fm:
        o = _find_cities(fm.group(1))
        d = _find_cities(fm.group(2))
        origin = o[0] if o else origin
        destination = d[0] if d else destination

    sort = "fastest" if re.search(r"\bfast|quick|short", t) else "cheapest"
    mode = None
    if re.search(r"\bflight|fly|air|plane", t):
        mode = "air"
    elif re.search(r"\bbus|road|coach|drive", t):
        mode = "road"

    part = next((p for p in PART_OF_DAY if p in t), None)
    d = _find_date(t)
    return {
        "origin": origin,
        "destination": destination,
        "sort": sort,
        "mode": mode,
        "date": d.isoformat() if d else None,
        "part_of_day": part,
    }


def parse_with_claude(text: str) -> dict:  # pragma: no cover - optional path
    """Route parsing through Anthropic Claude when configured."""
    import anthropic
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    tools = [{
        "name": "structured_query",
        "description": "Structured travel search query",
        "input_schema": {
            "type": "object",
            "properties": {
                "origin": {"type": "string", "description": "IATA-like code: LOS/ABV/ONI/PHC/LON"},
                "destination": {"type": "string"},
                "sort": {"type": "string", "enum": ["cheapest", "fastest"]},
                "mode": {"type": ["string", "null"], "enum": ["air", "road", None]},
                "date": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
                "part_of_day": {"type": ["string", "null"]},
            },
            "required": ["sort"],
        },
    }]
    msg = client.messages.create(
        model="claude-sonnet-5", max_tokens=300, tools=tools,
        tool_choice={"type": "tool", "name": "structured_query"},
        messages=[{"role": "user", "content":
                   f"Today is {date.today().isoformat()}. Parse this travel search: {text}"}],
    )
    for block in msg.content:
        if block.type == "tool_use":
            return block.input
    return parse(text)


def route_query(text: str) -> dict:
    if os.environ.get("USE_CLAUDE") == "1" and os.environ.get("ANTHROPIC_API_KEY"):
        try:
            return parse_with_claude(text)
        except Exception:
            pass
    return parse(text)