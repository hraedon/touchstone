"""Read models for the web face.

Everything the UI displays is assembled here, as plain data, by functions that
take a session and return frozen dataclasses. Templates get no ORM objects and no
opportunity to run a query of their own, which is what keeps the honesty rules in
one auditable place instead of scattered across markup.

The rules this module exists to enforce, from ``docs/measurement-model.md``:

* Aggregates are **read from ``scan_aggregate``**, never derived from ``listing``
  rows. There is no code path here that could recompute one.
* An aggregate over fewer than ``MIN_COHORT_N`` listings is **suppressed** — the
  count is still shown, because the count is a fact; the statistic is not shown,
  because a median over two listings is one seller's asking price in a costume.
* Two **discontinuities** are surfaced rather than smoothed over. Both are
  artifacts of how touchstone sampled, and an unmarked artifact is indistinguishable
  from a market move.
* The **disappearance series is carried separately** from the asking series and is
  never merged into it. A listing that vanished may have sold, been ended, or
  expired, and nothing here can tell those apart.
"""

from __future__ import annotations

import enum
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import assert_never

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from touchstone.db.models import (
    Deal,
    ExtractionMethod,
    ItemSpec,
    Listing,
    ListingDisappearance,
    ListingObservation,
    Query,
    RateBudget,
    Scan,
    ScanAggregate,
    ScanStatus,
    Watch,
)
from touchstone.ebay.budget import DEFAULT_DAILY_LIMIT, RESERVE, today_utc
from touchstone.extract.cohort import UNSPECCED, is_unspecced
from touchstone.extract.specs import DEAL_CONFIDENCE_FLOOR
from touchstone.scan.aggregate import MIN_COHORT_N

# Plan 001 keyed cohorts by query and condition alone, before titles were read.
# Plan 002 replaced that with the full spec tuple. Old rows keep their old keys and
# are never rewritten, so a series simply stops and another begins — which looks
# exactly like a market that went quiet unless the boundary is drawn.
LEGACY_COHORT_PREFIX = "q="


def is_legacy_cohort_key(key: str) -> bool:
    """True for a Plan 001 ``q=<id>|cond=<x>`` key.

    Deliberately a positive test for the old shape rather than a negative test for
    the new one: an unrecognized future format should not be silently relabelled as
    ancient history.
    """
    return key.startswith(LEGACY_COHORT_PREFIX)


class DiscontinuityKind(enum.StrEnum):
    """Why a series may not be comparable across a point in time."""

    FEEDBACK_FLOOR = "feedback_floor"
    COHORT_KEY_FORMAT = "cohort_key_format"
    SELLER_EXCLUSIONS = "seller_exclusions"


def discontinuity_label(kind: DiscontinuityKind) -> str:
    match kind:
        case DiscontinuityKind.FEEDBACK_FLOOR:
            return "Seller-feedback floor changed"
        case DiscontinuityKind.COHORT_KEY_FORMAT:
            return "Cohort definition changed"
        case DiscontinuityKind.SELLER_EXCLUSIONS:
            return "Seller exclusion list changed"
        case _:
            assert_never(kind)


def discontinuity_detail(kind: DiscontinuityKind) -> str:
    """Why the reader should not read across this line."""
    match kind:
        case DiscontinuityKind.FEEDBACK_FLOOR:
            return (
                "A different set of sellers was sampled from here on. A step in the "
                "figures across this line is a change in who was measured, not "
                "necessarily a change in what they were asking."
            )
        case DiscontinuityKind.COHORT_KEY_FORMAT:
            return (
                "Cohorts before this point were grouped by query and condition only, "
                "before titles were read for capacity. Series either side of this "
                "line describe differently-defined groups and do not continue one "
                "another."
            )
        case DiscontinuityKind.SELLER_EXCLUSIONS:
            return (
                "The operator's list of excluded sellers changed here, so a different "
                "set of listings was sampled from this point on. Who is on that list "
                "is deliberately not recorded — only that it changed, and how many "
                "names it held."
            )
        case _:
            assert_never(kind)


@dataclass(frozen=True)
class Discontinuity:
    at: datetime
    kind: DiscontinuityKind
    note: str

    @property
    def label(self) -> str:
        return discontinuity_label(self.kind)

    @property
    def detail(self) -> str:
        return discontinuity_detail(self.kind)


@dataclass(frozen=True)
class AskingPoint:
    """One cohort's asking-price statistics as recorded by one scan.

    Named for what it is. These are the prices sellers are *asking*; the pool is
    biased upward because inventory that is priced correctly sells and leaves it.
    """

    scan_id: int
    observed_at: datetime
    n: int
    currency: str
    price_min: float
    price_p10: float
    price_p25: float
    price_median: float
    price_mean: float
    per_gb_min: float | None
    per_gb_p10: float | None
    per_gb_p25: float | None
    per_gb_median: float | None
    per_gb_mean: float | None
    capped: bool
    min_seller_feedback: int

    @property
    def suppressed(self) -> bool:
        """Too few listings for the statistics to describe anything but themselves."""
        return self.n < MIN_COHORT_N


@dataclass(frozen=True)
class DisappearancePoint:
    """How many tracked listings left the active pool in one interval.

    An inference, and the weakest thing touchstone reports. A listing leaves because
    it sold, because the seller ended or revised it, or because it expired. Kept in
    its own series, with its own scale, so it can never be read as a sold price.
    """

    detected_at: datetime
    count: int


@dataclass(frozen=True)
class CohortSeries:
    cohort_key: str
    currency: str
    points: tuple[AskingPoint, ...]

    @property
    def legacy_key(self) -> bool:
        return is_legacy_cohort_key(self.cohort_key)

    @property
    def unspecced(self) -> bool:
        return is_unspecced(self.cohort_key)

    @property
    def latest(self) -> AskingPoint:
        return self.points[-1]

    @property
    def shown_points(self) -> tuple[AskingPoint, ...]:
        """Points whose statistics may be drawn. Suppressed ones are still counted."""
        return tuple(point for point in self.points if not point.suppressed)


@dataclass(frozen=True)
class ScanRow:
    id: int
    started_at: datetime
    status: ScanStatus
    result_count: int
    excluded_low_feedback: int
    min_seller_feedback: int
    api_calls: int
    capped: bool
    error: str | None
    excluded_sellers_count: int = 0
    excluded_sellers_digest: str | None = None


@dataclass(frozen=True)
class QueryTrend:
    query: Query
    series: tuple[CohortSeries, ...]
    disappearances: tuple[DisappearancePoint, ...]
    discontinuities: tuple[Discontinuity, ...]
    scans: tuple[ScanRow, ...]

    @property
    def capped_scan_count(self) -> int:
        return sum(1 for scan in self.scans if scan.capped)


def _scan_rows(session: Session, query_id: int, limit: int = 200) -> list[ScanRow]:
    stmt = (
        select(Scan)
        .where(Scan.query_id == query_id)
        .order_by(Scan.started_at.desc())
        .limit(limit)
    )
    return [
        ScanRow(
            id=scan.id,
            started_at=scan.started_at,
            status=scan.status,
            result_count=scan.result_count,
            excluded_low_feedback=scan.excluded_low_feedback,
            min_seller_feedback=scan.min_seller_feedback,
            api_calls=scan.api_calls,
            capped=scan.capped,
            error=scan.error,
            excluded_sellers_count=scan.excluded_sellers_count,
            excluded_sellers_digest=scan.excluded_sellers_digest,
        )
        for scan in session.scalars(stmt)
    ]


def feedback_floor_discontinuities(scans: list[ScanRow]) -> list[Discontinuity]:
    """One marker at each scan whose seller-feedback floor differed from the last.

    Only complete scans count. A failed or budget-skipped scan sampled nothing, so
    treating its recorded floor as a change would draw a boundary across a series
    that never moved.
    """
    ordered = sorted(
        (scan for scan in scans if scan.status is ScanStatus.COMPLETE),
        key=lambda scan: scan.started_at,
    )
    markers: list[Discontinuity] = []
    previous: int | None = None
    for scan in ordered:
        if previous is not None and scan.min_seller_feedback != previous:
            markers.append(
                Discontinuity(
                    at=scan.started_at,
                    kind=DiscontinuityKind.FEEDBACK_FLOOR,
                    note=f"floor {previous} to {scan.min_seller_feedback}",
                )
            )
        previous = scan.min_seller_feedback
    return markers


def seller_exclusion_discontinuities(scans: list[ScanRow]) -> list[Discontinuity]:
    """One marker wherever the operator's exclusion list changed between scans.

    Detected from the recorded digest, which identifies the configuration and not the
    people in it. Same reasoning as the seller-feedback floor: changing who is
    sampled makes the series discontinuous, and an unmarked change of that kind is
    indistinguishable from the market moving.
    """
    ordered = sorted(
        (scan for scan in scans if scan.status is ScanStatus.COMPLETE),
        key=lambda scan: scan.started_at,
    )
    markers: list[Discontinuity] = []
    previous: tuple[int, str | None] | None = None
    for scan in ordered:
        current = (scan.excluded_sellers_count, scan.excluded_sellers_digest)
        if previous is not None and current != previous:
            markers.append(
                Discontinuity(
                    at=scan.started_at,
                    kind=DiscontinuityKind.SELLER_EXCLUSIONS,
                    note=f"{previous[0]} to {current[0]} excluded",
                )
            )
        previous = current
    return markers


def cohort_format_discontinuity(series: list[CohortSeries]) -> list[Discontinuity]:
    """A single marker where Plan 001 keys give way to Plan 002 keys, if both exist.

    Placed at the earliest current-format observation, which is where a reader would
    otherwise see every old series end and new ones appear from nowhere.
    """
    current_starts = [
        s.points[0].observed_at for s in series if s.points and not s.legacy_key
    ]
    has_legacy = any(s.legacy_key and s.points for s in series)
    if not has_legacy or not current_starts:
        return []
    return [
        Discontinuity(
            at=min(current_starts),
            kind=DiscontinuityKind.COHORT_KEY_FORMAT,
            note="query+condition to full spec tuple",
        )
    ]


def query_trend(
    session: Session,
    query_id: int,
    *,
    since: datetime | None = None,
    cohort_key: str | None = None,
) -> QueryTrend | None:
    """Assemble everything the trend page shows for one query.

    Reads ``scan_aggregate`` and joins ``scan`` only for the two per-scan quality
    flags the aggregate does not carry. It never touches ``listing_observation``:
    the statistics are what was recorded at the time, and recomputing them from
    surviving listings is exactly the thing that would rewrite history.
    """
    query = session.get(Query, query_id)
    if query is None:
        return None

    stmt = (
        select(ScanAggregate, Scan.capped, Scan.min_seller_feedback)
        .join(Scan, Scan.id == ScanAggregate.scan_id)
        .where(ScanAggregate.query_id == query_id)
        .order_by(ScanAggregate.cohort_key, ScanAggregate.observed_at)
    )
    if since is not None:
        stmt = stmt.where(ScanAggregate.observed_at >= since)
    if cohort_key is not None:
        stmt = stmt.where(ScanAggregate.cohort_key == cohort_key)

    grouped: dict[tuple[str, str], list[AskingPoint]] = defaultdict(list)
    for aggregate, capped, floor in session.execute(stmt):
        grouped[(aggregate.cohort_key, aggregate.currency)].append(
            AskingPoint(
                scan_id=aggregate.scan_id,
                observed_at=aggregate.observed_at,
                n=aggregate.n,
                currency=aggregate.currency,
                price_min=float(aggregate.price_min),
                price_p10=float(aggregate.price_p10),
                price_p25=float(aggregate.price_p25),
                price_median=float(aggregate.price_median),
                price_mean=float(aggregate.price_mean),
                per_gb_min=_maybe_float(aggregate.per_gb_min),
                per_gb_p10=_maybe_float(aggregate.per_gb_p10),
                per_gb_p25=_maybe_float(aggregate.per_gb_p25),
                per_gb_median=_maybe_float(aggregate.per_gb_median),
                per_gb_mean=_maybe_float(aggregate.per_gb_mean),
                capped=bool(capped),
                min_seller_feedback=int(floor),
            )
        )

    series = [
        CohortSeries(cohort_key=key, currency=currency, points=tuple(points))
        for (key, currency), points in grouped.items()
    ]
    # Most-observed cohorts first; a reader wants the well-sampled ones on screen.
    series.sort(key=lambda s: (-s.latest.n, s.cohort_key))

    scans = _scan_rows(session, query_id)
    discontinuities = (
        feedback_floor_discontinuities(scans)
        + seller_exclusion_discontinuities(scans)
        + cohort_format_discontinuity(series)
    )
    discontinuities.sort(key=lambda marker: marker.at)

    return QueryTrend(
        query=query,
        series=tuple(series),
        disappearances=tuple(_disappearances(session, query_id, since=since,
                                             cohort_key=cohort_key)),
        discontinuities=tuple(discontinuities),
        scans=tuple(scans),
    )


def _maybe_float(value: float | None) -> float | None:
    return None if value is None else float(value)


def _disappearances(
    session: Session,
    query_id: int,
    *,
    since: datetime | None = None,
    cohort_key: str | None = None,
) -> list[DisappearancePoint]:
    stmt = (
        select(
            ListingDisappearance.detected_at,
            func.count().label("count"),
        )
        .where(ListingDisappearance.query_id == query_id)
        .group_by(ListingDisappearance.detected_at)
        .order_by(ListingDisappearance.detected_at)
    )
    if since is not None:
        stmt = stmt.where(ListingDisappearance.detected_at >= since)
    if cohort_key is not None:
        stmt = stmt.where(ListingDisappearance.cohort_key == cohort_key)
    return [
        DisappearancePoint(detected_at=detected_at, count=int(count))
        for detected_at, count in session.execute(stmt)
    ]


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QueryRow:
    query: Query
    last_scan: ScanRow | None
    scan_count: int

    @property
    def projected_daily_calls(self) -> int:
        """What this query costs a day if it runs on cadence and pages to the limit.

        An over-eager query is obvious here, before it is saved, rather than after
        it has spent the application's allowance. Deliberately the worst case: a
        scan that finds fewer pages spends less, and a disabled query spends nothing.
        """
        if not self.query.enabled or self.query.cadence_minutes <= 0:
            return 0
        scans_per_day = (24 * 60) // self.query.cadence_minutes
        return int(scans_per_day * max(self.query.max_pages, 1))


def query_rows(session: Session) -> list[QueryRow]:
    counts: dict[int, int] = {
        int(query_id): int(count)
        for query_id, count in session.execute(
            select(Scan.query_id, func.count(Scan.id)).group_by(Scan.query_id)
        ).all()
    }
    rows: list[QueryRow] = []
    for query in session.scalars(select(Query).order_by(Query.name)):
        last = session.scalars(
            select(Scan)
            .where(Scan.query_id == query.id)
            .order_by(Scan.started_at.desc())
            .limit(1)
        ).first()
        rows.append(
            QueryRow(
                query=query,
                last_scan=(
                    None
                    if last is None
                    else ScanRow(
                        id=last.id,
                        started_at=last.started_at,
                        status=last.status,
                        result_count=last.result_count,
                        excluded_low_feedback=last.excluded_low_feedback,
                        min_seller_feedback=last.min_seller_feedback,
                        api_calls=last.api_calls,
                        capped=last.capped,
                        error=last.error,
                        excluded_sellers_count=last.excluded_sellers_count,
                        excluded_sellers_digest=last.excluded_sellers_digest,
                    )
                ),
                scan_count=int(counts.get(query.id, 0)),
            )
        )
    return rows


@dataclass(frozen=True)
class BudgetView:
    """The budget as the web face may report it.

    Deliberately ledger-only. ``BudgetGuard.state()`` is authoritative when it can
    reach eBay, but reaching eBay costs a call against a 1,000/day token allowance
    and blocks the request on a third party — neither of which a page view may do.
    So this reads the stored ledger and shows *when* it was last reconciled, which
    is the honest version: a figure plus its staleness, never a fresh-looking number
    that is actually a guess.
    """

    day: str
    calls_used: int
    calls_limit: int
    last_authoritative_read: datetime | None
    last_authoritative_remaining: int | None
    reserve: int = RESERVE

    @property
    def remaining(self) -> int:
        return max(0, self.calls_limit - self.calls_used)

    @property
    def usable(self) -> int:
        return max(0, self.remaining - self.reserve)

    @property
    def authoritative(self) -> bool:
        """Always false here. The web face never spends a call to find out."""
        return False


def budget_view(session: Session) -> BudgetView:
    row = session.get(RateBudget, today_utc())
    if row is None:
        return BudgetView(
            day=today_utc(),
            calls_used=0,
            calls_limit=DEFAULT_DAILY_LIMIT,
            last_authoritative_read=None,
            last_authoritative_remaining=None,
        )
    return BudgetView(
        day=row.day,
        calls_used=row.calls_used,
        calls_limit=row.calls_limit,
        last_authoritative_read=row.last_authoritative_read,
        last_authoritative_remaining=row.last_authoritative_remaining,
    )


# ---------------------------------------------------------------------------
# Deals
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SpecView:
    """How a listing's capacity was decided, and how much to trust it.

    Shown beside every deal because a deal is only as good as its capacity parse:
    reading "Lot of 4 x 32GB" as 32GB rather than 128GB manufactures a fourfold
    bargain that does not exist.
    """

    title_hash: str
    normalized_title: str
    total_gb: int | None
    capacity_per_module_gb: int | None
    module_count: int | None
    ddr_gen: str | None
    speed_mt: int | None
    form_factor: str | None
    rank_org: str | None
    ecc: bool | None
    registered: bool | None
    method: ExtractionMethod | None
    confidence: float | None
    corrected_by: str | None

    @property
    def method_label(self) -> str:
        return extraction_method_label(self.method)

    @property
    def trusted(self) -> bool:
        if self.method is ExtractionMethod.MANUAL:
            return True
        return self.confidence is not None and self.confidence >= DEAL_CONFIDENCE_FLOOR


def extraction_method_label(method: ExtractionMethod | None) -> str:
    if method is None:
        return "not extracted"
    match method:
        case ExtractionMethod.REGEX:
            return "pattern"
        case ExtractionMethod.LLM:
            return "model"
        case ExtractionMethod.MANUAL:
            return "corrected by hand"
        case _:
            assert_never(method)


def _spec_view(spec: ItemSpec | None) -> SpecView | None:
    if spec is None:
        return None
    return SpecView(
        title_hash=spec.title_hash,
        normalized_title=spec.normalized_title,
        total_gb=spec.total_gb,
        capacity_per_module_gb=spec.capacity_per_module_gb,
        module_count=spec.module_count,
        ddr_gen=spec.ddr_gen,
        speed_mt=spec.speed_mt,
        form_factor=spec.form_factor,
        rank_org=spec.rank_org,
        ecc=spec.ecc,
        registered=spec.registered,
        method=spec.method,
        confidence=_maybe_float(spec.confidence),
        corrected_by=spec.corrected_by,
    )


@dataclass(frozen=True)
class DealView:
    deal: Deal
    listing: Listing | None
    spec: SpecView | None
    cohort_median_per_gb: float | None
    cohort_min_per_gb: float | None

    @property
    def per_gb(self) -> float | None:
        return _maybe_float(self.deal.per_gb)

    @property
    def cohort_p10(self) -> float:
        return float(self.deal.cohort_p10)

    @property
    def dismissed(self) -> bool:
        return self.deal.dismissed_at is not None


def deal_feed(session: Session, *, include_dismissed: bool = False, limit: int = 100
              ) -> list[DealView]:
    stmt = select(Deal).order_by(Deal.score.desc()).limit(limit)
    if not include_dismissed:
        stmt = stmt.where(Deal.dismissed_at.is_(None))
    deals = list(session.scalars(stmt))
    if not deals:
        return []

    listings = {
        listing.item_id: listing
        for listing in session.scalars(
            select(Listing).where(Listing.item_id.in_([d.listing_id for d in deals]))
        )
    }
    specs = {
        spec.title_hash: spec
        for spec in session.scalars(
            select(ItemSpec).where(
                ItemSpec.title_hash.in_(
                    [listing.title_hash for listing in listings.values()]
                )
            )
        )
    }
    # The cohort context is read back from the aggregate the same scan wrote, not
    # rebuilt from listings. Same row, same numbers, whatever has been pruned since.
    aggregates = {
        (aggregate.scan_id, aggregate.cohort_key): aggregate
        for aggregate in session.scalars(
            select(ScanAggregate).where(
                ScanAggregate.scan_id.in_([d.scan_id for d in deals])
            )
        )
    }

    views: list[DealView] = []
    for deal in deals:
        listing = listings.get(deal.listing_id)
        aggregate = aggregates.get((deal.scan_id, deal.cohort_key))
        views.append(
            DealView(
                deal=deal,
                listing=listing,
                spec=_spec_view(
                    specs.get(listing.title_hash) if listing is not None else None
                ),
                cohort_median_per_gb=(
                    None if aggregate is None else _maybe_float(aggregate.per_gb_median)
                ),
                cohort_min_per_gb=(
                    None if aggregate is None else _maybe_float(aggregate.per_gb_min)
                ),
            )
        )
    return views


# ---------------------------------------------------------------------------
# Spec correction worklist
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SpecWorkRow:
    title_hash: str
    title: str
    listing_count: int
    spec: SpecView | None

    @property
    def reason(self) -> str:
        if self.spec is None:
            return "no spec"
        if self.spec.total_gb is None:
            return "no capacity"
        return "low confidence"


def spec_worklist(session: Session, limit: int = 100) -> list[SpecWorkRow]:
    """Titles worth an operator's attention, most-shared first.

    Ordering by how many listings share the title is the whole point: correcting one
    template used by two hundred listings moves two hundred listings out of the
    unspecced bucket, and correcting a one-off moves one.
    """
    counts_stmt = (
        select(
            Listing.title_hash,
            func.min(Listing.title).label("title"),
            func.count(Listing.item_id).label("listing_count"),
        )
        .group_by(Listing.title_hash)
        .order_by(func.count(Listing.item_id).desc())
    )
    rows = list(session.execute(counts_stmt))
    if not rows:
        return []

    specs = {
        spec.title_hash: spec
        for spec in session.scalars(
            select(ItemSpec).where(ItemSpec.title_hash.in_([row[0] for row in rows]))
        )
    }

    work: list[SpecWorkRow] = []
    for title_hash, title, listing_count in rows:
        spec = specs.get(title_hash)
        view = _spec_view(spec)
        needs_attention = (
            view is None
            or view.total_gb is None
            or (view.method is not ExtractionMethod.MANUAL and not view.trusted)
        )
        if not needs_attention:
            continue
        work.append(
            SpecWorkRow(
                title_hash=title_hash,
                title=title,
                listing_count=int(listing_count),
                spec=view,
            )
        )
        if len(work) >= limit:
            break
    return work


def spec_detail(session: Session, title_hash: str) -> tuple[SpecView | None, str, int]:
    """The spec for one title, plus a sample title and how many listings share it."""
    row = session.execute(
        select(func.min(Listing.title), func.count(Listing.item_id))
        .where(Listing.title_hash == title_hash)
    ).one()
    spec = session.get(ItemSpec, title_hash)
    return _spec_view(spec), (row[0] or ""), int(row[1] or 0)


# ---------------------------------------------------------------------------
# Watchlist
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ObservationPoint:
    observed_at: datetime
    price: float
    shipping_cost: float | None
    total_cost: float | None
    currency: str

    @property
    def shipping_known(self) -> bool:
        """Missing shipping is unknown, not free.

        Rendering an absent shipping cost as 0.00 would understate the delivered
        price and bias every derived $/GB downward.
        """
        return self.shipping_cost is not None


@dataclass(frozen=True)
class WatchView:
    watch: Watch
    listing: Listing | None
    spec: SpecView | None
    observations: tuple[ObservationPoint, ...]

    @property
    def first_seen(self) -> datetime | None:
        return self.observations[0].observed_at if self.observations else None

    @property
    def latest(self) -> ObservationPoint | None:
        return self.observations[-1] if self.observations else None


def watch_list(session: Session) -> list[WatchView]:
    watches = list(session.scalars(select(Watch).order_by(Watch.created_at.desc())))
    return [_watch_view(session, watch) for watch in watches]


def watch_detail(session: Session, listing_id: str) -> WatchView | None:
    watch = session.scalars(select(Watch).where(Watch.listing_id == listing_id)).first()
    if watch is None:
        return None
    return _watch_view(session, watch)


def _watch_view(session: Session, watch: Watch) -> WatchView:
    listing = session.get(Listing, watch.listing_id)
    observations = tuple(
        ObservationPoint(
            observed_at=row.observed_at,
            price=float(row.price),
            shipping_cost=_maybe_float(row.shipping_cost),
            total_cost=_maybe_float(row.total_cost),
            currency=row.currency,
        )
        for row in session.scalars(
            select(ListingObservation)
            .where(ListingObservation.listing_id == watch.listing_id)
            .order_by(ListingObservation.observed_at)
        )
    )
    spec = (
        _spec_view(session.get(ItemSpec, listing.title_hash))
        if listing is not None
        else None
    )
    return WatchView(watch=watch, listing=listing, spec=spec, observations=observations)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Overview:
    queries: tuple[QueryRow, ...]
    open_deals: int
    unspecced_titles: int
    watched: int
    budget: BudgetView
    recent_scans: tuple[ScanRow, ...]


def overview(session: Session) -> Overview:
    unspecced = session.scalar(
        select(func.count(func.distinct(Listing.title_hash)))
        .outerjoin(ItemSpec, ItemSpec.title_hash == Listing.title_hash)
        .where((ItemSpec.title_hash.is_(None)) | (ItemSpec.total_gb.is_(None)))
    )
    recent = [
        ScanRow(
            id=scan.id,
            started_at=scan.started_at,
            status=scan.status,
            result_count=scan.result_count,
            excluded_low_feedback=scan.excluded_low_feedback,
            min_seller_feedback=scan.min_seller_feedback,
            api_calls=scan.api_calls,
            capped=scan.capped,
            error=scan.error,
            excluded_sellers_count=scan.excluded_sellers_count,
            excluded_sellers_digest=scan.excluded_sellers_digest,
        )
        for scan in session.scalars(
            select(Scan).order_by(Scan.started_at.desc()).limit(10)
        )
    ]
    return Overview(
        queries=tuple(query_rows(session)),
        open_deals=int(
            session.scalar(select(func.count(Deal.id)).where(Deal.dismissed_at.is_(None))) or 0
        ),
        unspecced_titles=int(unspecced or 0),
        watched=int(session.scalar(select(func.count(Watch.id))) or 0),
        budget=budget_view(session),
        recent_scans=tuple(recent),
    )


def default_since(days: int = 30) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)


__all__ = [
    "UNSPECCED",
    "AskingPoint",
    "BudgetView",
    "CohortSeries",
    "DealView",
    "DisappearancePoint",
    "Discontinuity",
    "DiscontinuityKind",
    "ObservationPoint",
    "Overview",
    "QueryRow",
    "QueryTrend",
    "ScanRow",
    "SpecView",
    "SpecWorkRow",
    "WatchView",
    "budget_view",
    "deal_feed",
    "default_since",
    "extraction_method_label",
    "is_legacy_cohort_key",
    "overview",
    "query_rows",
    "query_trend",
    "spec_detail",
    "spec_worklist",
    "watch_detail",
    "watch_list",
]
