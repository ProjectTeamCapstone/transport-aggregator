"""The two ways this system can book a seat, and the failure vocabulary.

Only two providers exist, because only two honest options exist:

    DuffelSandbox   a real API that returns a real booking reference. Sandbox
                    only, hold orders only, so no money is ever involved.
    DeepLink        a pre-filled link to the carrier's own checkout. Books
                    nothing by itself - it hands the traveller over.

The distinction matters in the ledger. A Duffel order becomes `confirmed`
because a reference exists. A deep link becomes `pending`, because at that
moment nobody has a ticket: the traveller still has to finish on the carrier's
site. Recording a handoff as `confirmed` would be the most misleading thing
this system could do.

--------------------------------------------------------------------------
The failure vocabulary
--------------------------------------------------------------------------
Two independent questions decide what happens after a failed call, and
collapsing them into one "did it work?" flag is how duplicate tickets happen:

    ambiguous  - might the carrier have created a booking anyway?
    retryable  - is asking again safe?

A timeout is ambiguous and NOT retryable: the request was sent, so a second
one could produce a second ticket. A refused TCP connection is retryable and
NOT ambiguous: nothing was ever sent, so nothing was created and asking again
costs nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from common.config import DUFFEL_API_TOKEN


# ------------------------------------------------------------------ errors ---

class ProviderError(Exception):
    """Base class. Subclasses answer the two questions above."""

    ambiguous: bool = False
    retryable: bool = False


class ProviderUnreachable(ProviderError):
    """We never got a request out - DNS failure, refused connection.

    Nothing was created, and asking again is free.
    """

    ambiguous = False
    retryable = True


class ProviderUnavailable(ProviderError):
    """The carrier answered, but with 'not now' - 5xx, or 429 rate limiting.

    Probably nothing was created. Not provably nothing, so still ambiguous if
    we run out of attempts.
    """

    ambiguous = True
    retryable = True


class ProviderTimeout(ProviderError):
    """We sent the request and never learned the outcome.

    The one case that must never be retried blindly: the carrier may have
    booked the seat and simply failed to tell us in time.
    """

    ambiguous = True
    retryable = False


class ProviderRejected(ProviderError):
    """A definite no - offer expired, passenger details invalid, sold out."""

    ambiguous = False
    retryable = False


class ProviderMisconfigured(ProviderError):
    """Our fault, not theirs - missing token, unsupported request.

    Retrying cannot fix it, and nothing was booked.
    """

    ambiguous = False
    retryable = False


# ------------------------------------------------------------------ result ---

@dataclass(frozen=True)
class BookingResult:
    provider: str
    state: str                    # 'confirmed' (a reference exists) or 'pending'
    reference: str | None
    detail: str                   # one plain sentence, shown to the traveller
    handoff_url: str | None = None


@dataclass(frozen=True)
class Passenger:
    """The minimum needed to reserve a seat. No payment fields, ever.

    The optional four are what an airline needs on a ticket and a coach
    operator does not. They stay optional so a road booking never has to ask
    for a date of birth it has no use for; when a flight needs one and it is
    missing, Duffel says so and the booking fails with that reason recorded.
    """

    given_name: str
    family_name: str
    email: str
    born_on: str | None = None      # YYYY-MM-DD, required on airline tickets
    phone: str | None = None        # E.164, e.g. +2348012345678
    title: str | None = None        # mr / ms / mrs / miss / dr
    gender: str | None = None       # 'm' or 'f', as the airline systems encode it


class Provider(Protocol):
    name: str

    def create(
        self, offer: dict[str, Any], passenger: Passenger, idempotency_key: str
    ) -> BookingResult: ...


# --------------------------------------------------------------- deep link ---

class DeepLinkProvider:
    """Hands the traveller to the carrier's checkout with the trip pre-filled.

    The journey details go into the URL; the passenger's name and email do NOT.
    A query string ends up in the carrier's server logs and the traveller's
    browser history, and none of it is needed to show them the right departure.
    """

    name = "deeplink"

    def create(
        self, offer: dict[str, Any], passenger: Passenger, idempotency_key: str
    ) -> BookingResult:
        url = self.build_url(offer)
        return BookingResult(
            provider=self.name,
            # Nobody has a ticket yet. The traveller has to finish on the
            # carrier's site, and we will never find out whether they did.
            state="pending",
            reference=None,
            handoff_url=url,
            detail=(
                f"{offer['carrier']} has no booking API we can use, so this is a "
                "pre-filled link to their own checkout. Your seat is not "
                "reserved until you complete it there."
            ),
        )

    @staticmethod
    def build_url(offer: dict[str, Any]) -> str:
        """Add the journey to the carrier's booking URL, keeping its own params.

        The parameter NAMES here are our own convention. Verifying each
        carrier's real checkout parameters would mean holding an account with
        all eight, which this project does not, so the link lands the traveller
        on the right page with our details attached rather than deep inside a
        checkout we have never seen. Where a carrier ignores them the page
        simply opens unfilled, which is still a working link.
        """
        parts = urlparse(offer["booking_url"])
        params = dict(parse_qsl(parts.query))
        params.update({
            "from": offer["origin"],
            "to": offer["destination"],
            "date": offer["depart_time"][:10],
            "depart": offer["depart_time"][11:16],
            "passengers": "1",
            "class": "economy",
        })
        return urlunparse(parts._replace(query=urlencode(params)))


# ----------------------------------------------------------------- routing ---

def choose_provider(offer: dict[str, Any]) -> Provider:
    """Duffel for flights when a sandbox token exists; deep links otherwise.

    Duffel sells air travel only, so no road carrier could ever route through
    it. Without a token every carrier deep-links, which is why the deep-link
    path is the one this project can actually demonstrate today.
    """
    if offer.get("mode") == "air" and DUFFEL_API_TOKEN:
        from booking.duffel import DuffelSandbox

        return DuffelSandbox()
    return DeepLinkProvider()


def provider_reason(offer: dict[str, Any]) -> str:
    """Why this offer books the way it does, in one sentence, for the API."""
    if offer.get("mode") != "air":
        return (
            "Road carriers have no booking API available to this project, so "
            "the traveller is handed to the carrier's own checkout."
        )
    if not DUFFEL_API_TOKEN:
        return (
            "No Duffel sandbox token is configured, so this flight deep-links "
            "instead. Set DUFFEL_API_TOKEN to book it through the sandbox."
        )
    return "Booked through the Duffel sandbox as a hold order, which takes no payment."
