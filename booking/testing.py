"""A stand-in for the Duffel sandbox, used by the tests and the demo script.

This is NOT a mock of our own client. The real `DuffelSandbox` runs against this
through httpx's transport layer, so the URLs it builds, the headers it sends,
the JSON body it posts and the way it reads responses back are all genuinely
exercised. Only the network hop is replaced.

What it therefore does NOT prove: that Duffel's live sandbox agrees with the
shapes encoded here. That needs a real DUFFEL_API_TOKEN, which this project does
not have. The distinction is recorded in the session report rather than glossed
over - a stub that passes is evidence about our code, not about theirs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass
class FakeDuffel:
    """Scriptable Duffel. Every request it receives is kept for inspection."""

    # Whether the returned offers can be held without paying. When False, our
    # client must refuse the booking rather than fall back to instant payment.
    holdable: bool = True
    any_offers: bool = True
    carrier_name: str = "Air Peace"

    # Failures to serve before succeeding. An int is an HTTP status; an
    # exception instance is raised from the transport, which is how a timeout or
    # a refused connection is simulated.
    order_failures: list[Any] = field(default_factory=list)
    search_failures: list[Any] = field(default_factory=list)

    # Orders Duffel believes exist. `orphan()` adds one WITHOUT our client
    # having seen the response - exactly what a lost reply looks like.
    orders: list[dict[str, Any]] = field(default_factory=list)
    requests: list[httpx.Request] = field(default_factory=list)

    _next_reference: int = 1000

    # ------------------------------------------------------------ wiring ---

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/air/offer_requests":
            return self._pop(self.search_failures) or self._search()
        if path == "/air/orders" and request.method == "POST":
            return self._pop(self.order_failures) or self._create(request)
        if path == "/air/orders":
            return httpx.Response(200, json={"data": self.orders})
        return httpx.Response(404, json={"errors": [{"title": "no such endpoint"}]})

    @staticmethod
    def _pop(queue: list[Any]) -> httpx.Response | None:
        if not queue:
            return None
        scripted = queue.pop(0)
        if isinstance(scripted, Exception):
            raise scripted
        return httpx.Response(
            scripted, json={"errors": [{"title": "scripted", "message": f"HTTP {scripted}"}]}
        )

    # --------------------------------------------------------- endpoints ---

    def _search(self) -> httpx.Response:
        if not self.any_offers:
            return httpx.Response(200, json={
                "data": {"id": "orq_fake", "offers": [], "passengers": [{"id": "pas_1"}]}
            })
        return httpx.Response(200, json={"data": {
            "id": "orq_fake",
            "passengers": [{"id": "pas_1", "type": "adult"}],
            "offers": [
                {
                    "id": "off_cheap_other",
                    "owner": {"name": "Some Other Airline"},
                    "total_amount": "90000.00",
                    "total_currency": "NGN",
                    "payment_requirements": {"requires_instant_payment": not self.holdable},
                },
                {
                    "id": "off_wanted",
                    "owner": {"name": self.carrier_name},
                    "total_amount": "145000.00",
                    "total_currency": "NGN",
                    "payment_requirements": {"requires_instant_payment": not self.holdable},
                },
            ],
        }})

    def _create(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)["data"]
        order = self._record(
            metadata=body.get("metadata", {}),
            selected=body["selected_offers"][0],
            order_type=body["type"],
        )
        return httpx.Response(201, json={"data": order})

    def _record(self, metadata: dict[str, Any], selected: str, order_type: str
                ) -> dict[str, Any]:
        self._next_reference += 1
        order = {
            "id": f"ord_{self._next_reference}",
            "booking_reference": f"NF{self._next_reference}",
            "type": order_type,
            "selected_offer": selected,
            "metadata": metadata,
            "payment_status": {
                "awaiting_payment": True,
                "payment_required_by": "2026-08-14T12:00:00Z",
            },
        }
        self.orders.append(order)
        return order

    # ------------------------------------------------------------ helpers ---

    def orphan(self, idempotency_key: str, offer_id: str) -> str:
        """Create an order our client never learned about.

        This is the situation the `unknown` state exists for: the carrier DID
        book a seat, and the reply was lost. Reconciliation has to be able to
        find it.
        """
        order = self._record(
            metadata={"idempotency_key": idempotency_key, "offer_id": offer_id},
            selected="off_wanted",
            order_type="hold",
        )
        return order["booking_reference"]

    @property
    def order_attempts(self) -> int:
        """How many times our client tried to CREATE an order.

        The number that matters: it must never exceed one per booking that
        could have succeeded.
        """
        return sum(
            1 for r in self.requests
            if r.url.path == "/air/orders" and r.method == "POST"
        )

    def client(self, **kwargs: Any):
        """The real DuffelSandbox, pointed at this stub."""
        from booking.duffel import DuffelSandbox

        return DuffelSandbox(
            token="duffel_test_stub", transport=self.transport(), timeout=1.0, **kwargs
        )
