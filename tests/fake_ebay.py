"""A fake eBay, good enough to exercise the whole scan path offline.

Serves the three endpoints touchstone calls: the OAuth token mint, Browse
``item_summary/search`` (with real pagination semantics), and the Developer
Analytics rate-limit read.

The generation mechanism is what makes the diff testable: ``advance()`` swaps which
snapshot is served, so a test can express "this listing was here, then it wasn't"
without waiting a day.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse


def item(
    item_id: str,
    *,
    price: float,
    title: str = "32GB 2Rx4 PC4-2400T ECC REG Server Memory",
    shipping: float | None = 0.0,
    shipping_options_without_cost: bool = False,
    seller: str = "seller_one",
    condition: str = "Used",
    condition_id: str = "3000",
    currency: str = "USD",
    buying_options: list[str] | None = None,
    feedback_score: int | None = 1234,
) -> dict[str, Any]:
    """Build one ItemSummary in the documented response shape.

    ``feedback_score=None`` omits the field entirely, which is how eBay behaves for
    some listings and is distinct from a score of zero.
    """
    seller_node: dict[str, Any] = {"username": seller, "feedbackPercentage": "99.5"}
    if feedback_score is not None:
        seller_node["feedbackScore"] = feedback_score
    node: dict[str, Any] = {
        "itemId": item_id,
        "title": title,
        "price": {"value": f"{price:.2f}", "currency": currency},
        "seller": seller_node,
        "condition": condition,
        "conditionId": condition_id,
        "buyingOptions": buying_options if buying_options is not None else ["FIXED_PRICE"],
        "itemWebUrl": f"https://www.ebay.com/itm/{item_id}",
        "image": {"imageUrl": f"https://i.ebayimg.com/{item_id}.jpg"},
        "categories": [{"categoryId": "170083"}],
    }
    if shipping is not None:
        node["shippingOptions"] = [
            {"shippingCost": {"value": f"{shipping:.2f}", "currency": currency}}
        ]
    elif shipping_options_without_cost:
        # Observed in production: the option can exist without shippingCost. This
        # differs from both a missing option and an explicit 0.00 (free shipping).
        node["shippingOptions"] = [{}]
    return node


@dataclass
class Generation:
    """One snapshot of the marketplace, plus what eBay claims the total is.

    ``reported_total`` exists separately from ``len(items)`` so a test can simulate
    the 10,000-cap case without materializing ten thousand fixtures.
    """

    items: list[dict[str, Any]]
    reported_total: int | None = None

    @property
    def total(self) -> int:
        return self.reported_total if self.reported_total is not None else len(self.items)


@dataclass
class FakeEbay:
    generations: list[Generation]
    rate_limit_remaining: int | None = 5000
    rate_limit_total: int = 5000
    # Set to fail the rate-limit read, to exercise the ledger fallback.
    rate_limit_fails: bool = False

    index: int = 0
    token_mints: int = field(default=0, init=False)
    search_calls: int = field(default=0, init=False)
    rate_limit_calls: int = field(default=0, init=False)
    filters_seen: list[str] = field(default_factory=list, init=False)
    _server: HTTPServer | None = field(default=None, init=False)
    _thread: threading.Thread | None = field(default=None, init=False)

    @property
    def current(self) -> Generation:
        return self.generations[min(self.index, len(self.generations) - 1)]

    def advance(self) -> None:
        self.index += 1

    # -- server lifecycle ---------------------------------------------------

    def start(self) -> str:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _send(self, code: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:
                if urlparse(self.path).path == "/identity/v1/oauth2/token":
                    length = int(self.headers.get("Content-Length", 0))
                    self.rfile.read(length)
                    fake.token_mints += 1
                    self._send(200, {"access_token": "fake-token", "expires_in": 7200})
                    return
                self._send(404, {"error": "not found"})

            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                params = parse_qs(parsed.query)

                if parsed.path == "/developer/analytics/v1_beta/rate_limit/":
                    fake.rate_limit_calls += 1
                    if fake.rate_limit_fails or fake.rate_limit_remaining is None:
                        self._send(500, {"error": "rate limit unavailable"})
                        return
                    self._send(
                        200,
                        {
                            "rateLimits": [
                                {
                                    "apiName": "Browse",
                                    "apiContext": "buy",
                                    "resources": [
                                        {
                                            "name": "buy.browse",
                                            "rates": [
                                                {
                                                    "limit": fake.rate_limit_total,
                                                    "remaining": fake.rate_limit_remaining,
                                                    "reset": "2026-08-31T00:00:00.000Z",
                                                    "timeWindow": 86400,
                                                }
                                            ],
                                        }
                                    ],
                                }
                            ]
                        },
                    )
                    return

                if parsed.path == "/buy/browse/v1/item_summary/search":
                    fake.filters_seen.append(params.get("filter", [""])[0])
                    fake.search_calls += 1
                    limit = int((params.get("limit") or ["200"])[0])
                    offset = int((params.get("offset") or ["0"])[0])
                    generation = fake.current
                    window = generation.items[offset : offset + limit]
                    self._send(
                        200,
                        {
                            "href": self.path,
                            "total": generation.total,
                            "limit": limit,
                            "offset": offset,
                            "itemSummaries": window,
                        },
                    )
                    return

                self._send(404, {"error": "not found"})

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        host, port = self._server.server_address[0], self._server.server_address[1]
        return f"http://{host!s}:{port!s}"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
