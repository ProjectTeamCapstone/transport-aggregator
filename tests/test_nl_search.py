"""The rule parser, and the contract both parsing strategies must meet.

Only the rule path is exercised. A test that called Claude would be testing the
model - non-deterministic, slow, and billed - and the point of the fallback is
that it needs no network. What is asserted here is the CONTRACT, which the
Claude path is held to as well: a SearchQuery with valid place codes, a
resolvable date, and a preference.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from api import nl_search
from common.places import PLACES


def parse(text: str):
    parsed = nl_search.parse_query(text)
    assert parsed.strategy == "rules", "a test reached the model"
    return parsed.query


# ------------------------------------------------------------------ places --

@pytest.mark.parametrize("phrase, expected", [
    ("cheapest way to Abuja", "ABV"),
    ("get me to port harcourt", "PHC"),
    ("travel to PHC tomorrow", "PHC"),
    ("flight to london", "LHR"),
    ("bus to onitsha", "ONI"),
])
def test_destinations_are_recognised(phrase, expected):
    assert parse(phrase).destination == expected


def test_a_place_name_inside_another_word_is_not_a_booking():
    """"closest" contains "los". Matching without word boundaries would read
    that as Lagos and quietly search the wrong route."""
    query = parse("what is the closest option to Abuja")
    assert query.destination == "ABV"


def test_the_longer_spelling_wins():
    """"port harcourt" contains "ph". Matching the shorter alias first would
    turn a Port Harcourt search into... a Port Harcourt search by accident,
    and a "phc" inside another word into a real one."""
    assert parse("from lagos to port harcourt").destination == "PHC"


def test_from_and_to_are_read_in_the_right_direction():
    query = parse("from abuja to lagos on friday")
    assert (query.origin, query.destination) == ("ABV", "LOS")


def test_origin_defaults_when_only_a_destination_is_named():
    query = parse("cheapest way to Abuja")
    assert query.origin in PLACES
    assert query.origin != query.destination


# ------------------------------------------------------------------- dates --

def test_tomorrow():
    expected = (date.today() + timedelta(days=1)).isoformat()
    assert parse("bus to Abuja tomorrow").date == expected


def test_a_named_weekday_resolves_to_a_future_date():
    query = parse("cheapest to Abuja on friday")
    resolved = date.fromisoformat(query.date)

    assert resolved.weekday() == 4
    assert resolved >= date.today(), "a weekday resolved into the past"


def test_next_week_is_in_the_future():
    query = parse("cheapest way to Abuja next week")
    assert date.fromisoformat(query.date) > date.today()


def test_an_unstated_date_stays_null():
    """Null, not today. Inventing a date the traveller did not give would
    silently narrow their search."""
    assert parse("cheapest way to Abuja").date is None


# ------------------------------------------------------------- preference ---

@pytest.mark.parametrize("phrase, expected", [
    ("cheapest way to Abuja", "cheapest"),
    ("fastest route to Abuja", "fastest"),
    ("quickest way to Abuja", "fastest"),
    ("I'm in a hurry to get to Abuja", "fastest"),
    ("budget travel to Abuja", "cheapest"),
    ("how do I get to Abuja", "cheapest"),      # the default
])
def test_preference(phrase, expected):
    assert parse(phrase).preference == expected


# -------------------------------------------------------------- validation --

def test_a_query_naming_no_place_is_rejected_with_a_reason():
    parsed = nl_search.parse_query("I would like to go somewhere nice")
    problems = nl_search.validate(parsed)

    assert problems, "an unusable query passed validation"
    assert all(isinstance(p, str) and p for p in problems)


def test_an_out_of_scope_route_is_left_for_the_search_endpoint_to_reject():
    """`validate` checks that the PARSE is usable, not that the route is covered.

    Abuja to Port Harcourt parses perfectly - two real places, right direction -
    so there is nothing here to report. Whether the route is in scope is
    `/search`'s judgement, and it already returns a 400 naming every route the
    project covers. Duplicating that rule here would give two places to change
    it and one of them would eventually be wrong.
    """
    parsed = nl_search.parse_query("from abuja to port harcourt")

    assert nl_search.validate(parsed) == []
    assert (parsed.query.origin, parsed.query.destination) == ("ABV", "PHC")


def test_a_usable_query_has_no_problems():
    assert nl_search.validate(nl_search.parse_query("cheapest to Abuja")) == []


def test_every_parse_returns_codes_the_search_endpoint_accepts():
    """The contract, asserted across the phrasings the project actually sees.

    Both strategies promise this: whatever comes back, `/search` can be called
    with it. A parser that emitted "Lagos" instead of "LOS" would satisfy every
    individual test above and still 400 on every real request.
    """
    phrasings = [
        "cheapest way to Abuja next week",
        "fastest route from lagos to phc",
        "bus to onitsha tomorrow",
        "flight to london on friday",
        "get me to abuja",
    ]
    for phrase in phrasings:
        query = nl_search.parse_query(phrase).query
        assert query.origin in PLACES, phrase
        assert query.destination in PLACES, phrase
        assert query.preference in ("cheapest", "fastest"), phrase
        if query.date is not None:
            date.fromisoformat(query.date)      # raises if not YYYY-MM-DD
