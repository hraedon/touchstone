"""eBay API client — OAuth application token, Browse search, rate limits.

httpx throughout, never urllib. A ``urllib`` TLS fingerprint draws a Cloudflare 1010
that is indistinguishable from the provider being down; this estate has twice filed
work items against the wrong cause because of it. Probe with the library the app uses.

Two documented limits shape everything here:

* Browse: 5,000 calls/day, application-wide. ``search`` returns at most 200 items per
  call and at most 10,000 per result set, and ``offset`` must be a multiple of
  ``limit``.
* OAuth token endpoint: 1,000 calls/day. Token caching is therefore mandatory, not an
  optimization — an uncached mint per search would exhaust the token budget long
  before the search budget.
"""

from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger("touchstone.ebay")

PRODUCTION = "https://api.ebay.com"
SCOPE = "https://api.ebay.com/oauth/api_scope"

# eBay's hard ceilings. Not tuning knobs.
MAX_LIMIT = 200
MAX_RESULT_SET = 10_000


class EbayError(RuntimeError):
    """An eBay API call failed in a way the caller must handle."""


@dataclass(frozen=True)
class Credentials:
    client_id: str
    client_secret: str


@dataclass
class ParsedListing:
    """One ItemSummary, reduced to the fields touchstone records.

    Everything here is copied verbatim from the API response. Nothing is inferred,
    estimated, or filled in — this is the truth path.

    **The seller is deliberately absent.** ``ItemSummary`` carries ``seller.username``
    and we drop it here rather than downstream, so an eBay user identifier never
    enters the process at all. See ``docs/deletion-compliance.md``: data we never
    hold needs no deletion, no purge list, and cannot come back from a backup.
    """

    item_id: str
    title: str
    price: float
    currency: str
    shipping_cost: float | None
    total_cost: float
    condition: str | None
    condition_id: str | None
    buying_options: tuple[str, ...]
    item_web_url: str | None
    image_url: str | None
    category_id: str | None


@dataclass
class SearchPage:
    listings: list[ParsedListing]
    total: int
    offset: int
    limit: int
    # Listings dropped for insufficient seller feedback. Counted so a filter that
    # quietly eats most of a result set is visible rather than merely effective.
    excluded_low_feedback: int = 0


@dataclass
class RateLimit:
    limit: int
    remaining: int
    reset: str | None


class TokenCache:
    """Client-credentials application token, cached until shortly before expiry."""

    def __init__(self, creds: Credentials, http: httpx.Client, base_url: str = PRODUCTION):
        self._creds = creds
        self._http = http
        self._base_url = base_url.rstrip("/")
        self._token: str | None = None
        self._expires_at: float = 0.0
        self.mint_count = 0

    def token(self) -> str:
        if self._token is not None and time.time() < self._expires_at:
            return self._token
        return self._mint()

    def _mint(self) -> str:
        raw = f"{self._creds.client_id}:{self._creds.client_secret}".encode()
        auth = base64.b64encode(raw).decode("ascii")
        resp = self._http.post(
            f"{self._base_url}/identity/v1/oauth2/token",
            headers={
                "Authorization": f"Basic {auth}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"grant_type": "client_credentials", "scope": SCOPE},
        )
        if resp.status_code != 200:
            raise EbayError(f"token mint failed: {resp.status_code} {resp.text[:200]}")
        body = resp.json()
        token = body.get("access_token")
        if not token:
            raise EbayError("token response contained no access_token")
        # 120s margin so an in-flight call cannot straddle the expiry.
        self._expires_at = time.time() + int(body.get("expires_in", 7200)) - 120
        self._token = str(token)
        self.mint_count += 1
        return self._token


def _money(node: Any) -> tuple[float, str] | None:
    """Parse an eBay {value, currency} amount. Returns None if absent or malformed."""
    if not isinstance(node, dict):
        return None
    raw = node.get("value")
    if raw is None:
        return None
    try:
        return float(raw), str(node.get("currency") or "USD")
    except (TypeError, ValueError):
        return None


def seller_feedback_score(item: dict[str, Any]) -> int | None:
    """The seller's feedback count, or None if eBay did not supply one.

    Read here and discarded here. The score is used to decide whether to keep the
    listing and is never returned to the caller, so it cannot reach the database —
    the same boundary that keeps the username out.
    """
    seller = item.get("seller")
    if not isinstance(seller, dict):
        return None
    raw = seller.get("feedbackScore")
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.strip().isdigit():
        return int(raw.strip())
    return None


def parse_item_summary(
    item: dict[str, Any], *, min_seller_feedback: int = 0
) -> ParsedListing | None:
    """Reduce an ItemSummary to a ParsedListing, or None if it should be skipped.

    Dropped when the summary has no id or no price (it cannot be an observation),
    or when the seller's feedback score is below ``min_seller_feedback``.

    A missing feedback score is treated as *unknown, therefore kept*. Dropping on
    absence would silently discard legitimate listings whenever eBay omits the
    field, and this is a noise filter rather than a security control — it should
    fail toward including too much, where the effect is visible in the data, not
    toward excluding too much, where it is invisible.
    """
    item_id = item.get("itemId")
    priced = _money(item.get("price"))
    if not item_id or priced is None:
        return None

    if min_seller_feedback > 0:
        score = seller_feedback_score(item)
        if score is not None and score < min_seller_feedback:
            return None

    price, currency = priced

    shipping: float | None = None
    options = item.get("shippingOptions")
    if isinstance(options, list) and options:
        first = options[0]
        if isinstance(first, dict):
            parsed = _money(first.get("shippingCost"))
            if parsed is not None:
                shipping = parsed[0]

    # item["seller"] is deliberately not read. See ParsedListing.

    category_id = None
    categories = item.get("categories")
    if isinstance(categories, list) and categories:
        first_cat = categories[0]
        if isinstance(first_cat, dict) and first_cat.get("categoryId"):
            category_id = str(first_cat["categoryId"])

    image_url = None
    image = item.get("image")
    if isinstance(image, dict) and image.get("imageUrl"):
        image_url = str(image["imageUrl"])

    raw_options = item.get("buyingOptions")
    buying_options = tuple(str(o) for o in raw_options) if isinstance(raw_options, list) else ()

    return ParsedListing(
        item_id=str(item_id),
        title=str(item.get("title") or ""),
        price=price,
        currency=currency,
        shipping_cost=shipping,
        total_cost=price + (shipping or 0.0),
        condition=str(item["condition"]) if item.get("condition") else None,
        condition_id=str(item["conditionId"]) if item.get("conditionId") else None,
        buying_options=buying_options,
        item_web_url=str(item["itemWebUrl"]) if item.get("itemWebUrl") else None,
        image_url=image_url,
        category_id=category_id,
    )


def _below_feedback(item: dict[str, Any], minimum: int) -> bool:
    """Whether this summary was dropped for feedback rather than for being unusable.

    Only used to attribute a drop to the right counter; the score itself goes no
    further.
    """
    score = seller_feedback_score(item)
    return score is not None and score < minimum


@dataclass
class EbayClient:
    """Browse search and rate-limit reads against one marketplace."""

    credentials: Credentials
    base_url: str = PRODUCTION
    marketplace_id: str = "EBAY_US"
    timeout: float = 30.0
    http: httpx.Client = field(init=False)
    tokens: TokenCache = field(init=False)
    calls_made: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")
        self.http = httpx.Client(timeout=self.timeout)
        self.tokens = TokenCache(self.credentials, self.http, self.base_url)

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> EbayClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.tokens.token()}",
            "X-EBAY-C-MARKETPLACE-ID": self.marketplace_id,
            "Accept": "application/json",
        }

    def search_page(
        self,
        q: str,
        *,
        offset: int = 0,
        limit: int = MAX_LIMIT,
        category_ids: str | None = None,
        filter_expr: str | None = None,
        min_seller_feedback: int = 0,
    ) -> SearchPage:
        """One Browse search call. Costs exactly one unit of the daily budget."""
        if limit > MAX_LIMIT:
            raise ValueError(f"limit {limit} exceeds eBay's maximum of {MAX_LIMIT}")
        if offset % limit != 0:
            # eBay rejects this, but with an opaque error. Fail here where the
            # cause is legible.
            raise ValueError(f"offset {offset} must be a multiple of limit {limit}")

        params: dict[str, str] = {"q": q, "limit": str(limit), "offset": str(offset)}
        if category_ids:
            params["category_ids"] = category_ids
        if filter_expr:
            params["filter"] = filter_expr

        resp = self.http.get(
            f"{self.base_url}/buy/browse/v1/item_summary/search",
            params=params,
            headers=self._headers(),
        )
        self.calls_made += 1
        if resp.status_code != 200:
            raise EbayError(f"search failed: {resp.status_code} {resp.text[:300]}")
        body = resp.json()

        summaries = body.get("itemSummaries") or []
        listings: list[ParsedListing] = []
        low_feedback = 0
        unusable = 0
        for raw in summaries:
            if not isinstance(raw, dict):
                unusable += 1
                continue
            parsed = parse_item_summary(raw, min_seller_feedback=min_seller_feedback)
            if parsed is not None:
                listings.append(parsed)
            elif min_seller_feedback > 0 and _below_feedback(raw, min_seller_feedback):
                low_feedback += 1
            else:
                unusable += 1
        if unusable:
            log.warning("dropped %d unusable item summaries (no id or no price)", unusable)
        if low_feedback:
            log.info(
                "excluded %d listings from sellers below %d feedback",
                low_feedback,
                min_seller_feedback,
            )

        return SearchPage(
            listings=listings,
            total=int(body.get("total") or 0),
            offset=int(body.get("offset") or offset),
            limit=int(body.get("limit") or limit),
            excluded_low_feedback=low_feedback,
        )

    def rate_limit(self, api_name: str = "Browse", api_context: str = "buy") -> RateLimit | None:
        """Authoritative remaining quota, or None if it could not be read.

        None means "unknown", and callers must treat unknown as a reason to fall
        back to the local ledger — never as permission to proceed unlimited.
        """
        try:
            resp = self.http.get(
                f"{self.base_url}/developer/analytics/v1_beta/rate_limit/",
                params={"api_name": api_name, "api_context": api_context},
                headers=self._headers(),
            )
        except httpx.HTTPError as exc:
            log.warning("rate limit read failed (transport): %s", exc)
            return None
        if resp.status_code != 200:
            log.warning("rate limit read failed: %s %s", resp.status_code, resp.text[:200])
            return None

        for group in resp.json().get("rateLimits") or []:
            for resource in group.get("resources") or []:
                for rate in resource.get("rates") or []:
                    if rate.get("limit") is None or rate.get("remaining") is None:
                        continue
                    return RateLimit(
                        limit=int(rate["limit"]),
                        remaining=int(rate["remaining"]),
                        reset=rate.get("reset"),
                    )
        log.warning("rate limit response contained no usable rate entry")
        return None
