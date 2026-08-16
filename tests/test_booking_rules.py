"""Booking logic that needs no database: routing, the failure vocabulary,
deep-link construction, and the request contract.

The database-backed guarantee lives in test_ledger_idempotency.py. Everything
here is decidable from the code alone, so it runs on any laptop.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from booking import ledger, providers
from tests.conftest import make_offer


# ------------------------------------------------- the failure vocabulary ---

def test_every_error_answers_both_questions():
    """`ambiguous` and `retryable` are independent, and collapsing them into
    one "did it work?" flag is how duplicate tickets happen."""
    errors = [
        providers.ProviderUnreachable, providers.ProviderUnavailable,
        providers.ProviderTimeout, providers.ProviderRejected,
        providers.ProviderMisconfigured,
    ]
    for error in errors:
        assert isinstance(error.ambiguous, bool), error.__name__
        assert isinstance(error.retryable, bool), error.__name__


def test_a_timeout_is_ambiguous_and_not_retryable():
    """The single most important pair of flags in the project. The request was
    sent, so a seat may exist; sending it again could produce a second."""
    assert providers.ProviderTimeout.ambiguous is True
    assert providers.ProviderTimeout.retryable is False


def test_unreachable_is_retryable_and_not_ambiguous():
    """Nothing was ever sent, so nothing was created and asking again is free."""
    assert providers.ProviderUnreachable.ambiguous is False
    assert providers.ProviderUnreachable.retryable is True


def test_no_error_is_both_retryable_and_unambiguously_created():
    """A guard against a future error class that would licence a blind retry
    after something may have been booked."""
    for error in (providers.ProviderTimeout, providers.ProviderRejected):
        assert not (error.retryable and error.ambiguous)


# ----------------------------------------------------------------- routing --

def test_road_offers_always_deep_link(monkeypatch):
    monkeypatch.setattr(providers, "DUFFEL_API_TOKEN", "duffel_test_abc")
    provider = providers.choose_provider(make_offer(mode="road"))
    assert provider.name == "deeplink"


def test_flights_deep_link_when_no_token_is_configured(monkeypatch):
    monkeypatch.setattr(providers, "DUFFEL_API_TOKEN", "")
    provider = providers.choose_provider(make_offer(mode="air"))
    assert provider.name == "deeplink"


def test_the_reason_is_a_sentence_a_traveller_could_read():
    reason = providers.provider_reason(make_offer(mode="road"))
    assert reason.endswith(".") and len(reason.split()) > 5


# --------------------------------------------------------------- deep links --

def test_a_deep_link_carries_the_journey_and_never_the_passenger():
    """A query string ends up in the carrier's logs and the traveller's browser
    history. The journey has to go there; the person does not."""
    offer = make_offer(origin="LOS", destination="ABV")
    url = providers.DeepLinkProvider.build_url(offer)
    params = parse_qs(urlparse(url).query)

    assert params["from"] == ["LOS"] and params["to"] == ["ABV"]
    blob = url.lower()
    for leak in ("ada", "okoro", "@", "email", "name"):
        assert leak not in blob, f"{leak!r} reached the deep-link URL"


def test_the_carriers_own_query_parameters_survive():
    offer = make_offer()
    offer["booking_url"] = "https://carrier.test/book?affiliate=naijafares"

    params = parse_qs(urlparse(providers.DeepLinkProvider.build_url(offer)).query)

    assert params["affiliate"] == ["naijafares"]
    assert params["from"] == ["LOS"]


def test_a_handoff_is_pending_and_says_no_seat_is_held():
    """Recording a handoff as `confirmed` would be the most misleading thing
    this system could do: nobody has a ticket at that moment."""
    result = providers.DeepLinkProvider().create(
        make_offer(),
        providers.Passenger("Ada", "Okoro", "ada@example.test"),
        "key-1",
    )

    assert result.state == "pending"
    assert result.reference is None
    assert result.handoff_url
    assert "not reserved" in result.detail.lower()


# ---------------------------------------------------------- derived keys ----

def test_derive_key_is_stable_and_normalises_the_address():
    a = ledger.derive_key("offer-1", "Ada@Example.test", 25_000.0)
    b = ledger.derive_key("offer-1", "  ada@example.test  ", 25_000.00)

    assert a == b, "the same request produced two different keys"
    assert a.startswith("auto-v1-")


def test_a_different_price_derives_a_different_key():
    """A traveller who has accepted a new number is making a new request."""
    a = ledger.derive_key("offer-1", "ada@example.test", 25_000.0)
    b = ledger.derive_key("offer-1", "ada@example.test", 27_000.0)
    assert a != b


def test_a_different_traveller_derives_a_different_key():
    a = ledger.derive_key("offer-1", "ada@example.test", 25_000.0)
    b = ledger.derive_key("offer-1", "obi@example.test", 25_000.0)
    assert a != b


# --------------------------------------------------- the request contract ---

def test_a_request_carrying_card_details_is_rejected():
    """`extra="forbid"` is a scope guard, not a nicety. Payment is out of scope,
    so a card number must be REFUSED rather than silently ignored - this API
    must never be mistakable for one that takes payments."""
    from pydantic import ValidationError

    from api.main import BookingRequest

    with pytest.raises(ValidationError):
        BookingRequest(
            offer_id="gigm-road-LOS-ABV-1",
            quoted_price_ngn=25_000.0,
            passenger={
                "given_name": "Ada", "family_name": "Okoro",
                "email": "ada@example.test",
                "card_number": "4242424242424242",
            },
        )


def test_a_free_price_is_rejected():
    from pydantic import ValidationError

    from api.main import BookingRequest

    with pytest.raises(ValidationError):
        BookingRequest(
            offer_id="gigm-road-LOS-ABV-1",
            quoted_price_ngn=0,
            passenger={
                "given_name": "Ada", "family_name": "Okoro",
                "email": "ada@example.test",
            },
        )


@pytest.mark.parametrize("address", ["nope", "a@b", "@example.test", "ada@"])
def test_a_malformed_email_is_rejected(address):
    from pydantic import ValidationError

    from api.main import PassengerIn

    with pytest.raises(ValidationError):
        PassengerIn(given_name="Ada", family_name="Okoro", email=address)


# --------------------------------------------------------- the sandbox gate --

def test_a_live_duffel_token_refuses_to_start(monkeypatch):
    """Payment processing is out of scope, so a live token is a configuration
    error rather than an option. It must fail at startup, not at booking time."""
    from common import config

    monkeypatch.setattr(config, "DUFFEL_API_TOKEN", "duffel_live_abc123")

    with pytest.raises(RuntimeError, match="sandbox"):
        config.assert_sandbox_only()


def test_a_sandbox_token_is_accepted(monkeypatch):
    from common import config

    monkeypatch.setattr(config, "DUFFEL_API_TOKEN", "duffel_test_abc123")
    config.assert_sandbox_only()
