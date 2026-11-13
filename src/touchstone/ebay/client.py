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
import enum
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx

log = logging.getLogger("touchstone.ebay")


def _describe_filter(filter_expr: str | None) -> str:
    """A filter's shape, never its contents.

    An exclusion list is a list of names; the useful operational facts are that one
    was applied and how big it was, neither of which requires naming anyone.
    """
    if not filter_expr:
        return "-"
    excluded = filter_expr.count("|") + 1 if "excludeSellers:{" in filter_expr else 0
    if excluded:
        return f"<{len(filter_expr)} chars, {excluded} seller exclusions>"
    return f"<{len(filter_expr)} chars>"


def configure_logging(verbose: bool = False) -> None:
    """Set up logging so no request URL reaches the log.

    httpx logs every request line, including the full query string, at INFO. The
    Browse filter travels in that query string and can carry the seller exclusion
    list, so httpx's own logger is held at WARNING and the client emits its own
    redacted line instead. Raising this to DEBUG for debugging will log URLs in full
    — including any exclusion list — so do it deliberately and not in the cluster.
    """
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s %(message)s",
    )
    if not verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)

PRODUCTION = "https://api.ebay.com"
SCOPE = "https://api.ebay.com/oauth/api_scope"

# eBay's hard ceilings. Not tuning knobs.
MAX_LIMIT = 200
MAX_RESULT_SET = 10_000
DEFAULT_BUYING_OPTIONS_FILTER = "buyingOptions:{AUCTION|FIXED_PRICE}"
DAILY_RATE_WINDOW_SECONDS = 86_400
_BUYING_OPTIONS_CLAUSE = re.compile(r"(?:\A|,)\s*buyingOptions\s*:", re.IGNORECASE)


def _with_default_buying_options(filter_expr: str | None) -> str:
    """Make Browse's sampled buying-option population explicit.

    Browse otherwise returns only listings with FIXED_PRICE. An operator-supplied
    buyingOptions clause is authoritative and is never replaced.
    """
    if filter_expr and _BUYING_OPTIONS_CLAUSE.search(filter_expr):
        return filter_expr
    return f"{filter_expr},{DEFAULT_BUYING_OPTIONS_FILTER}" if filter_expr else (
        DEFAULT_BUYING_OPTIONS_FILTER
    )


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
    total_cost: float | None
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


class ItemAvailability(enum.StrEnum):
    """What one ``getItem`` call reported about a listing.

    ``unavailable`` means the listing cannot be bought: its end date has
    passed, its stock estimate is out, or the API no longer returns it at all.
    Ended, sold, and purged are not distinguishable from outside, and this enum
    does not claim to tell them apart. ``unknown`` is a failure or an ambiguous response —
    not evidence of anything.
    """

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


# ``estimatedAvailabilities[].estimatedAvailabilityStatus`` values. Matched
# case-insensitively; anything unrecognized maps to UNKNOWN rather than being
# guessed at.
_IN_STOCK_STATUSES = frozenset({"IN_STOCK", "LIMITED_STOCK"})
_OUT_OF_STOCK_STATUS = "OUT_OF_STOCK"


def _parse_api_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    # A naive stamp has no defensible zone; refusing it beats guessing one.
    return parsed if parsed.tzinfo is not None else None


def parse_item_availability(item: object, *, now: datetime) -> ItemAvailability | None:
    """Map a getItem response body to an outcome, or None if it says nothing usable.

    The Item resource has no listing-status field. What it does carry, observed
    against the live API: an ended listing keeps answering 200 for weeks, with
    ``itemEndDate`` in the past, and its ``estimatedAvailabilityStatus`` may be
    ``OUT_OF_STOCK`` (the quantity went) or still ``IN_STOCK`` (it ended unsold).
    So the end date is read first and outranks the stock estimate; a live
    good-'til-cancelled listing carries no end date at all.

    None — no end date in the past and no stock status this parser recognizes —
    must be treated by the caller as unknown, never guessed.
    """
    if not isinstance(item, dict):
        return None
    ended_at = _parse_api_timestamp(item.get("itemEndDate"))
    if ended_at is not None and ended_at <= now:
        return ItemAvailability.UNAVAILABLE

    statuses: set[str] = set()
    for entry in item.get("estimatedAvailabilities") or []:
        status = entry.get("estimatedAvailabilityStatus") if isinstance(entry, dict) else None
        if isinstance(status, str) and status.strip():
            statuses.add(status.strip().upper())
    if statuses & _IN_STOCK_STATUSES:
        return ItemAvailability.AVAILABLE
    if statuses == {_OUT_OF_STOCK_STATUS}:
        return ItemAvailability.UNAVAILABLE
    return None


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
        try:
            body = resp.json()
        except ValueError as exc:
            raise EbayError("token response was not JSON") from exc
        if not isinstance(body, dict):
            raise EbayError("token response was not a JSON object")
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
    currency = node.get("currency")
    if raw is None or not isinstance(currency, str) or not currency.strip():
        return None
    try:
        # Currency is copied as returned. Absence is unknown, never an assumed USD.
        return float(raw), currency
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
            # ParsedListing has one currency. A shipping amount in any other
            # currency cannot be represented or added without mislabeling it.
            if parsed is not None and parsed[1] == currency:
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
        # Missing shipping is unknown, not free. Preserve the missingness through
        # persistence and keep this listing out of total-cost statistics.
        total_cost=price + shipping if shipping is not None else None,
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

    def _headers(self, marketplace_id: str | None = None) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.tokens.token()}",
            "X-EBAY-C-MARKETPLACE-ID": marketplace_id or self.marketplace_id,
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
        marketplace_id: str | None = None,
    ) -> SearchPage:
        """One Browse search call. Costs exactly one unit of the daily budget."""
        if limit > MAX_LIMIT:
            raise ValueError(f"limit {limit} exceeds eBay's maximum of {MAX_LIMIT}")
        if offset % limit != 0:
            # eBay rejects this, but with an opaque error. Fail here where the
            # cause is legible.
            raise ValueError(f"offset {offset} must be a multiple of limit {limit}")

        effective_filter = _with_default_buying_options(filter_expr)
        params: dict[str, str] = {"q": q, "limit": str(limit), "offset": str(offset)}
        if category_ids:
            params["category_ids"] = category_ids
        params["filter"] = effective_filter

        # Logged by us, with the filter reduced to its shape. httpx logs the whole
        # request URL at INFO, and the filter can carry the operator's seller
        # exclusion list — which would put those usernames into container logs on
        # every scan, several times a day, retained by the cluster. That would be a
        # second and far less controlled copy of the one thing we promise is held
        # only in a Secret. `configure_logging` silences httpx's own line.
        log.info(
            "browse search: q=%r offset=%d limit=%d category=%s filter=%s",
            q,
            offset,
            limit,
            category_ids or "-",
            _describe_filter(effective_filter),
        )
        # A token/local-validation failure never dispatches Browse and costs nothing.
        # Once dispatch begins, transport, HTTP, and parse failures all spend the call.
        headers = self._headers(marketplace_id)
        self.calls_made += 1
        try:
            resp = self.http.get(
                f"{self.base_url}/buy/browse/v1/item_summary/search",
                params=params,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise EbayError(f"search failed (transport): {exc}") from exc
        if resp.status_code != 200:
            raise EbayError(f"search failed: {resp.status_code} {resp.text[:300]}")
        try:
            body = resp.json()
        except ValueError as exc:
            raise EbayError("search response was not JSON") from exc
        if not isinstance(body, dict):
            raise EbayError("search response was not a JSON object")

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

    def get_item(self, item_id: str) -> ItemAvailability:
        """One Browse ``getItem`` call. Costs exactly one unit of the daily budget.

        Never raises: the three outcomes are the whole contract. The response
        carries seller data; this boundary reads the end date and the stock
        estimate and discards everything else, exactly as the search path
        drops the username.
        """
        log.info("browse get_item: item_id=%s", item_id)
        try:
            headers = self._headers()
            self.calls_made += 1
            resp = self.http.get(
                f"{self.base_url}/buy/browse/v1/item/{quote(item_id, safe='')}",
                headers=headers,
            )
        except (httpx.HTTPError, EbayError) as exc:
            log.warning("get_item failed (transport): %s", exc)
            return ItemAvailability.UNKNOWN
        if resp.status_code in (404, 410):
            return ItemAvailability.UNAVAILABLE
        if resp.status_code != 200:
            log.warning("get_item failed: %s %s", resp.status_code, resp.text[:200])
            return ItemAvailability.UNKNOWN
        try:
            body = resp.json()
        except ValueError:
            log.warning("get_item response was not JSON")
            return ItemAvailability.UNKNOWN
        outcome = parse_item_availability(body, now=datetime.now(UTC))
        if outcome is None:
            log.warning("get_item response carried no end date or recognizable stock status")
            return ItemAvailability.UNKNOWN
        return outcome

    def rate_limit(self, api_name: str = "browse", api_context: str = "buy") -> RateLimit | None:
        """Authoritative daily Browse quota, or None when the answer is ambiguous.

        None means "unknown", and callers must treat unknown as a reason to fall
        back to the local ledger — never as permission to proceed unlimited. The
        local ledger is daily, so an hourly or otherwise unidentified window cannot
        be substituted for the daily rate merely because it appeared first.
        """
        try:
            resp = self.http.get(
                f"{self.base_url}/developer/analytics/v1_beta/rate_limit/",
                params={"api_name": api_name, "api_context": api_context},
                headers=self._headers(),
            )
        except (httpx.HTTPError, EbayError) as exc:
            log.warning("rate limit read failed (transport): %s", exc)
            return None
        if resp.status_code != 200:
            log.warning("rate limit read failed: %s %s", resp.status_code, resp.text[:200])
            return None
        try:
            body = resp.json()
        except ValueError:
            log.warning("rate limit response was not JSON")
            return None
        if not isinstance(body, dict) or not isinstance(body.get("rateLimits"), list):
            log.warning("rate limit response had no rateLimits list")
            return None

        expected_name = api_name.casefold()
        expected_context = api_context.casefold()
        expected_resource = f"{api_context}.{api_name}".casefold()
        daily: list[dict[str, Any]] = []
        for group in body["rateLimits"]:
            if not isinstance(group, dict):
                continue
            if (
                not isinstance(group.get("apiName"), str)
                or group["apiName"].casefold() != expected_name
                or not isinstance(group.get("apiContext"), str)
                or group["apiContext"].casefold() != expected_context
                or not isinstance(group.get("resources"), list)
            ):
                continue
            for resource in group["resources"]:
                if (
                    not isinstance(resource, dict)
                    or not isinstance(resource.get("name"), str)
                    or resource["name"].casefold() != expected_resource
                    or not isinstance(resource.get("rates"), list)
                ):
                    continue
                daily.extend(
                    rate
                    for rate in resource["rates"]
                    if isinstance(rate, dict)
                    and rate.get("timeWindow") == DAILY_RATE_WINDOW_SECONDS
                )

        # Ambiguity anywhere in the target resource poisons the whole answer:
        # picking one valid-looking entry could overstate the remaining allowance.
        if len(daily) != 1:
            if not daily:
                log.warning("rate limit response contained no daily Browse rate")
            else:
                log.warning("rate limit response contained multiple daily Browse rates")
            return None

        rate = daily[0]
        raw_limit = rate.get("limit")
        raw_remaining = rate.get("remaining")
        if (
            raw_limit is None
            or raw_remaining is None
            or isinstance(raw_limit, bool)
            or isinstance(raw_remaining, bool)
            or (isinstance(raw_limit, float) and not raw_limit.is_integer())
            or (isinstance(raw_remaining, float) and not raw_remaining.is_integer())
        ):
            log.warning("rate limit response contained non-integral quota values")
            return None
        try:
            limit = int(raw_limit)
            remaining = int(raw_remaining)
        except (TypeError, ValueError, OverflowError):
            log.warning("rate limit response contained malformed quota values")
            return None
        if limit < 0 or not 0 <= remaining <= limit:
            log.warning("rate limit response contained impossible quota values")
            return None
        reset = rate.get("reset")
        return RateLimit(
            limit=limit,
            remaining=remaining,
            reset=reset if isinstance(reset, str) else None,
        )
