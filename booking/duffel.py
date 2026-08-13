"""Duffel sandbox client - the one real booking API in this project.

Two safety rules are enforced in code, not by convention:

1. **Sandbox tokens only.** assert_sandbox_only() runs in the constructor, so a
   live token cannot be used even by a caller that skipped startup checks.
2. **Hold orders only.** A Duffel order is either `instant` (pay now) or `hold`
   (reserve the seat, pay later). This client only ever sends `type: "hold"`,
   and it refuses any Duffel offer that requires instant payment. Payment
   processing is out of scope for this project, so the code has no path to it.

Booking a flight takes two calls, not one:

    POST /air/offer_requests   search - ask what is flying, get Duffel's own
                               offer IDs. Read-only, so always safe to retry.
    POST /air/orders           the actual booking. Retrying this is what could
                               produce two tickets, so it is treated far more
                               carefully.

Our idempotency key is written into the order's `metadata`. That is what makes
an `unknown` booking recoverable: reconcile() lists recent orders and looks for
one carrying the key, which answers "did that lost request book a seat?" with
evidence rather than a guess.
"""

from __future__ import annotations

from typing import Any

import httpx

from booking.providers import (
    BookingResult,
    Passenger,
    ProviderMisconfigured,
    ProviderRejected,
    ProviderTimeout,
    ProviderUnavailable,
    ProviderUnreachable,
)
from common.config import (
    DUFFEL_API_TOKEN,
    DUFFEL_API_VERSION,
    assert_sandbox_only,
)

BASE_URL = "https://api.duffel.com"

# How many recent orders reconcile() scans. Duffel returns newest first, and an
# unknown booking is minutes old, so the key is found long before this runs out.
RECONCILE_PAGE_SIZE = 100


class DuffelSandbox:
    name = "duffel_sandbox"

    def __init__(
        self,
        token: str | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 20.0,
        base_url: str = BASE_URL,
    ):
        # Refuse a live token here as well as at startup. This is the last line
        # before real money.
        assert_sandbox_only()
        self._token = token if token is not None else DUFFEL_API_TOKEN
        if not self._token:
            raise ProviderMisconfigured(
                "No DUFFEL_API_TOKEN configured, so nothing can be booked "
                "through the sandbox."
            )
        self._transport = transport
        self._timeout = timeout
        self._base_url = base_url

    # ----------------------------------------------------------- transport ---

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self._base_url,
            timeout=self._timeout,
            transport=self._transport,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Duffel-Version": DUFFEL_API_VERSION,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        read_only: bool,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """One HTTP call, with every failure translated into our vocabulary.

        `read_only` changes what a timeout MEANS. A search that times out
        created nothing, so it is merely unreachable and safe to repeat. An
        order that times out may have booked a seat, so it is ambiguous and must
        not be repeated.
        """
        headers = {}
        if idempotency_key:
            # Sent in case Duffel honours it. Nothing here depends on that -
            # the ledger and the metadata below are what actually keep us safe.
            headers["Idempotency-Key"] = idempotency_key

        try:
            with self._client() as client:
                response = client.request(
                    method, path, json=json, params=params, headers=headers
                )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            # The connection never opened, so no request was ever sent.
            raise ProviderUnreachable(f"could not reach Duffel: {exc}") from exc
        except httpx.TimeoutException as exc:
            if read_only:
                raise ProviderUnreachable(f"Duffel search timed out: {exc}") from exc
            raise ProviderTimeout(
                f"Duffel accepted the order request but never answered: {exc}"
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderUnreachable(f"transport failure talking to Duffel: {exc}") from exc

        return self._unwrap(response, read_only=read_only)

    @staticmethod
    def _unwrap(response: httpx.Response, *, read_only: bool) -> dict[str, Any]:
        status = response.status_code

        if 200 <= status < 300:
            try:
                return response.json()["data"]
            except (ValueError, KeyError) as exc:
                # A 2xx we cannot read is worse than an error: for an order we
                # genuinely do not know what happened.
                if read_only:
                    raise ProviderUnavailable(
                        f"Duffel returned unreadable JSON: {exc}"
                    ) from exc
                raise ProviderTimeout(
                    f"Duffel accepted the order but its response was unreadable: {exc}"
                ) from exc

        detail = _error_detail(response)

        if status in (401, 403):
            raise ProviderMisconfigured(f"Duffel rejected our credentials: {detail}")
        if status == 429:
            raise ProviderUnavailable(f"Duffel rate-limited us: {detail}")
        if status >= 500:
            raise ProviderUnavailable(f"Duffel is having trouble ({status}): {detail}")
        raise ProviderRejected(f"Duffel declined ({status}): {detail}")

    # -------------------------------------------------------------- booking ---

    def create(
        self, offer: dict[str, Any], passenger: Passenger, idempotency_key: str
    ) -> BookingResult:
        search = self._search(offer)
        duffel_offer, passenger_id = self._pick_holdable(search, offer)
        return self._hold(duffel_offer, passenger_id, passenger, offer, idempotency_key)

    def _search(self, offer: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "POST",
            "/air/offer_requests",
            params={"return_offers": "true"},
            json={
                "data": {
                    "slices": [{
                        "origin": offer["origin"],
                        "destination": offer["destination"],
                        "departure_date": offer["depart_time"][:10],
                    }],
                    "passengers": [{"type": "adult"}],
                    "cabin_class": "economy",
                }
            },
            read_only=True,
        )

    @staticmethod
    def _pick_holdable(
        search: dict[str, Any], offer: dict[str, Any]
    ) -> tuple[dict[str, Any], str]:
        """Choose a Duffel offer that can be held without paying.

        Preferring the same carrier the traveller picked matters: they chose Air
        Peace at a price they saw, and silently booking a different airline
        because it sorted first would be a bait and switch.
        """
        offers = search.get("offers") or []
        if not offers:
            raise ProviderRejected(
                f"Duffel found no flights for {offer['origin']}->{offer['destination']} "
                f"on {offer['depart_time'][:10]}."
            )

        holdable = [
            o for o in offers
            if not (o.get("payment_requirements") or {}).get(
                "requires_instant_payment", True
            )
        ]
        if not holdable:
            raise ProviderRejected(
                "Every Duffel offer for this flight requires instant payment. "
                "This project takes no payments, so the booking was not made - "
                "the traveller is sent to the carrier's checkout instead."
            )

        wanted = offer.get("carrier", "").casefold()
        same_carrier = [
            o for o in holdable
            if wanted and wanted in ((o.get("owner") or {}).get("name", "")).casefold()
        ]
        candidates = same_carrier or holdable

        chosen = min(candidates, key=lambda o: float(o.get("total_amount") or "inf"))

        passengers = search.get("passengers") or []
        if not passengers or not passengers[0].get("id"):
            raise ProviderRejected(
                "Duffel's offer request came back without a passenger id, so "
                "the order cannot name who is travelling."
            )
        return chosen, passengers[0]["id"]

    def _hold(
        self,
        duffel_offer: dict[str, Any],
        passenger_id: str,
        passenger: Passenger,
        offer: dict[str, Any],
        idempotency_key: str,
    ) -> BookingResult:
        person: dict[str, Any] = {
            "id": passenger_id,
            "given_name": passenger.given_name,
            "family_name": passenger.family_name,
            "email": passenger.email,
        }
        # Airlines require these; coach operators do not, so they are only sent
        # when the caller actually supplied them.
        for field, value in (
            ("born_on", passenger.born_on),
            ("phone_number", passenger.phone),
            ("title", passenger.title),
            ("gender", passenger.gender),
        ):
            if value:
                person[field] = value

        order = self._request(
            "POST",
            "/air/orders",
            json={
                "data": {
                    # Never "instant". A hold reserves the seat and takes no money.
                    "type": "hold",
                    "selected_offers": [duffel_offer["id"]],
                    "passengers": [person],
                    # The recovery mechanism: this is how reconcile() proves
                    # whether a lost request booked anything.
                    "metadata": {
                        "idempotency_key": idempotency_key,
                        "offer_id": offer["offer_id"],
                    },
                }
            },
            read_only=False,
            idempotency_key=idempotency_key,
        )

        reference = order.get("booking_reference") or order.get("id")
        expires = order.get("payment_status", {}).get("payment_required_by")
        detail = (
            f"Held with {offer['carrier']} through the Duffel sandbox. "
            f"Reference {reference}. No payment was taken."
        )
        if expires:
            detail += f" The hold would need paying by {expires}."

        return BookingResult(
            provider=self.name,
            state="confirmed",
            reference=reference,
            detail=detail,
        )

    # ---------------------------------------------------------- reconcile ---

    def find_by_idempotency_key(self, idempotency_key: str) -> str | None:
        """Did a booking carrying this key actually get created?

        This is what turns `unknown` from a dead end into a question with an
        answer. Returns the booking reference if Duffel has an order tagged with
        our key, None if it genuinely has none.
        """
        page = self._request(
            "GET",
            "/air/orders",
            params={"limit": RECONCILE_PAGE_SIZE},
            read_only=True,
        )
        orders = page if isinstance(page, list) else page.get("orders", [])
        for order in orders:
            metadata = order.get("metadata") or {}
            if metadata.get("idempotency_key") == idempotency_key:
                return order.get("booking_reference") or order.get("id")
        return None


def _error_detail(response: httpx.Response) -> str:
    """Duffel's own error message, when it sent one we can read."""
    try:
        errors = response.json().get("errors") or []
    except ValueError:
        return response.text[:200] or "no response body"
    if not errors:
        return response.text[:200] or "no error detail"
    return "; ".join(
        f"{e.get('title', '?')}: {e.get('message', '')}".strip(": ") for e in errors
    )[:400]
