"""Running one scan: snapshot → observations → disappearances → aggregates.

The truth path. Everything recorded here is copied from what the API returned; no
value is inferred, estimated, or model-derived.

Order matters. Aggregates are computed from the rows just observed and written
before the scan closes, because they must never be derivable from ``listing`` rows
afterwards — that is what keeps a chart honest across a deletion.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import assert_never

from sqlalchemy import select
from sqlalchemy.orm import Session

from touchstone.db.models import (
    BuyingOption,
    Deal,
    ExtractionMethod,
    ItemSpec,
    Listing,
    ListingDisappearance,
    ListingObservation,
    PriceUnit,
    Query,
    Scan,
    ScanAggregate,
    ScanStatus,
    SpecCategory,
)
from touchstone.ebay import exclusions
from touchstone.ebay.budget import BudgetGuard
from touchstone.ebay.client import MAX_LIMIT, MAX_RESULT_SET, EbayClient, ParsedListing
from touchstone.extract.cohort import (
    CohortFields,
    StorageFields,
    cohort_key,
    item_key,
    storage_key,
)
from touchstone.extract.normalize import title_hash
from touchstone.scan.aggregate import Priced, Stats, cohort_stats
from touchstone.scan.deals import evaluate
from touchstone.scan.diff import PreviousListing, ScanWindow, find_disappearances

log = logging.getLogger("touchstone.scan")


class ScanSkipped(RuntimeError):
    """The scan did not run. Carries the reason for the operator."""


@dataclass
class ScanResult:
    scan_id: int
    status: ScanStatus
    observed: int
    new_listings: int
    disappearances: int
    cohorts: int
    api_calls: int
    capped: bool
    truncated: bool
    deals: int = 0
    excluded_low_feedback: int = 0


def _buying_option(options: tuple[str, ...]) -> BuyingOption:
    """Reduce eBay's buyingOptions list to the one that governs the price.

    A listing can be both AUCTION and BEST_OFFER; precedence picks the option the
    observed price actually refers to. Unknown values become OTHER rather than
    raising, because eBay may add options and a scan must not die on one.
    """
    for candidate in (
        BuyingOption.FIXED_PRICE,
        BuyingOption.AUCTION,
        BuyingOption.CLASSIFIED_AD,
        BuyingOption.BEST_OFFER,
    ):
        if candidate.value in options:
            return candidate
    return BuyingOption.OTHER


def _spec_index(session: Session, hashes: set[str]) -> dict[str, ItemSpec]:
    """Specs for the titles in this scan, keyed by title hash.

    Missing entries are normal and expected: extraction runs on its own schedule, so
    a listing first seen this scan has no spec yet. It lands in the unspecced cohort
    until the next extraction pass, which is the correct behavior — an unknown
    quantity must not be mixed into a real cohort.
    """
    if not hashes:
        return {}
    rows = session.scalars(select(ItemSpec).where(ItemSpec.title_hash.in_(hashes))).all()
    return {row.title_hash: row for row in rows}


def _cohort_of(spec: ItemSpec | None, condition_id: str | None) -> str:
    """The cohort key for a listing's spec, dispatched on its category.

    The memory format is frozen (see extract/cohort.py); storage and per-item
    formats are prefixed with their own leads. A spec with no category is a
    pre-category row the backfill missed, which cohorts as unspecced rather
    than being guessed into a market.
    """
    if spec is None or spec.category is None:
        return cohort_key(None, condition_id)
    match spec.category:
        case SpecCategory.MEMORY:
            return cohort_key(
                CohortFields(
                    ddr_gen=spec.ddr_gen,
                    form_factor=spec.form_factor,
                    ecc=spec.ecc,
                    registered=spec.registered,
                    capacity_per_module_gb=spec.capacity_per_module_gb,
                    speed_mt=spec.speed_mt,
                    rank_org=spec.rank_org,
                ),
                condition_id,
            )
        case SpecCategory.STORAGE:
            return storage_key(
                StorageFields(
                    storage_type=spec.storage_type,
                    capacity_tb=float(spec.capacity_tb) if spec.capacity_tb else None,
                    storage_iface=spec.storage_iface,
                    storage_form=spec.storage_form,
                ),
                condition_id,
            )
        case SpecCategory.OTHER:
            return item_key(spec.product_model, condition_id)
        case _:
            assert_never(spec.category)


def _units_of(spec: ItemSpec | None) -> float | None:
    """The normalization denominator a spec's category prices by.

    Memory divides by GB, storage by decimal TB (lot count included), and a
    per-item good divides by 1 — its normalized price *is* its delivered price.
    """
    if spec is None or spec.category is None:
        return None
    match spec.category:
        case SpecCategory.MEMORY:
            if spec.total_gb is None:
                return None
            return float(spec.total_gb)
        case SpecCategory.STORAGE:
            if spec.capacity_tb is None:
                return None
            count = spec.module_count or 1
            return float(spec.capacity_tb) * count
        case SpecCategory.OTHER:
            return float(spec.module_count or 1)
        case _:
            assert_never(spec.category)


def _unit_of(spec: ItemSpec | None) -> PriceUnit | None:
    if spec is None or spec.category is None:
        return None
    match spec.category:
        case SpecCategory.MEMORY:
            return PriceUnit.GB
        case SpecCategory.STORAGE:
            return PriceUnit.TB
        case SpecCategory.OTHER:
            return PriceUnit.ITEM
        case _:
            assert_never(spec.category)


def query_signature(query: Query) -> str:
    """A digest of what the query asks eBay for, to detect an edit between scans.

    Covers exactly the stored definition. The operator's seller exclusions are
    combined into the filter only at call time and are deliberately not hashed
    here: they have their own keyed digest, and an unkeyed hash over a filter
    carrying usernames could be reversed by guessing.
    """
    definition = json.dumps(
        [query.q, query.category_ids or "", query.filter_expr or "", query.marketplace_id],
        separators=(",", ":"),
    )
    return hashlib.sha256(definition.encode("utf-8")).hexdigest()


def _previous_scan(session: Session, query_id: int, before_scan_id: int) -> Scan | None:
    stmt = (
        select(Scan)
        .where(
            Scan.query_id == query_id,
            Scan.id != before_scan_id,
            Scan.status == ScanStatus.COMPLETE,
        )
        .order_by(Scan.started_at.desc())
        .limit(1)
    )
    return session.scalars(stmt).first()


def _previous_snapshot(session: Session, scan_id: int) -> list[PreviousListing]:
    """What the previous scan saw, reduced to what the diff needs."""
    stmt = (
        select(
            ListingObservation.listing_id,
            ListingObservation.price,
            ListingObservation.total_cost,
            ListingObservation.currency,
            Listing.condition_id,
            Listing.title_hash,
        )
        .join(Listing, Listing.item_id == ListingObservation.listing_id)
        .where(ListingObservation.scan_id == scan_id)
    )
    rows = session.execute(stmt).all()
    specs = _spec_index(session, {str(row.title_hash) for row in rows})
    return [
        PreviousListing(
            item_id=str(row.listing_id),
            cohort_key=_cohort_of(specs.get(str(row.title_hash)), row.condition_id),
            last_price=float(row.price),
            last_total_cost=float(row.total_cost) if row.total_cost is not None else None,
            currency=str(row.currency),
        )
        for row in rows
    ]


def _upsert_listing(session: Session, parsed: ParsedListing, observed_at: datetime) -> bool:
    """Insert or refresh a Listing. Returns True if it is new to us."""
    existing = session.get(Listing, parsed.item_id)
    if existing is not None:
        existing.last_seen_at = observed_at
        # Titles and prices get revised in place by sellers; keep the latest.
        if existing.title != parsed.title:
            existing.title = parsed.title
            existing.title_hash = title_hash(parsed.title)
        existing.condition = parsed.condition
        existing.condition_id = parsed.condition_id
        return False

    session.add(
        Listing(
            item_id=parsed.item_id,
            title=parsed.title,
            title_hash=title_hash(parsed.title),
            condition=parsed.condition,
            condition_id=parsed.condition_id,
            buying_option=_buying_option(parsed.buying_options),
            item_web_url=parsed.item_web_url,
            image_url=parsed.image_url,
            category_id=parsed.category_id,
            first_seen_at=observed_at,
            last_seen_at=observed_at,
        )
    )
    return True


def _flag_deals(
    session: Session,
    scan_id: int,
    seen: dict[str, ParsedListing],
    cohort_of: dict[str, str],
    units_of: dict[str, float | None],
    unit_of: dict[str, PriceUnit | None],
    specs: dict[str, ItemSpec],
    per_unit_by_cohort: dict[str, Stats],
) -> int:
    """Flag listings below their cohort's p10, once each, ever.

    Re-flagging a listing every scan trains the operator to ignore the feed, so a
    listing already carrying a Deal row is skipped rather than re-scored.
    """
    already = set(
        session.scalars(
            select(Deal.listing_id).where(Deal.listing_id.in_(list(seen)))
        ).all()
    )
    flagged = 0
    for parsed in seen.values():
        if parsed.item_id in already:
            continue
        key = cohort_of[parsed.item_id]
        stats = per_unit_by_cohort.get(key)
        units = units_of[parsed.item_id]
        unit = unit_of[parsed.item_id]
        if (
            parsed.total_cost is None
            or stats is None
            or units is None
            or units <= 0
            or unit is None
        ):
            continue

        spec = specs.get(title_hash(parsed.title))
        candidate = evaluate(
            listing_id=parsed.item_id,
            cohort_key=key,
            per_unit=parsed.total_cost / units,
            cohort_p10=stats.p10,
            cohort_median=stats.median,
            cohort_n=stats.n,
            confidence=float(spec.confidence) if spec and spec.confidence else None,
            manual=bool(spec and spec.method is ExtractionMethod.MANUAL),
        )
        if candidate is None:
            continue
        session.add(
            Deal(
                listing_id=candidate.listing_id,
                scan_id=scan_id,
                cohort_key=candidate.cohort_key,
                total_cost=parsed.total_cost,
                per_unit=candidate.per_unit,
                unit=unit,
                cohort_p10=candidate.cohort_p10,
                cohort_n=candidate.cohort_n,
                score=candidate.score,
            )
        )
        flagged += 1
    session.flush()
    return flagged


def run_scan(
    session: Session,
    client: EbayClient,
    query: Query,
    *,
    budget: BudgetGuard | None = None,
    page_limit: int = MAX_LIMIT,
) -> ScanResult:
    """Execute one scan of one query.

    Raises ScanSkipped when the budget will not permit even a single page — the
    caller must not interpret that as an empty result.
    """
    guard = budget if budget is not None else BudgetGuard(session, client)

    # The operator's exclusion list, read from the environment at call time and never
    # persisted. Applied server-side so eBay never returns these listings at all:
    # they are not paged through, not parsed, and never reach the database layer.
    excluded_sellers = exclusions.from_env()
    effective_filter = exclusions.combine(query.filter_expr, excluded_sellers)

    wanted_pages = max(1, query.max_pages)
    allowed_pages = guard.check(wanted_pages)
    if allowed_pages <= 0:
        state = guard.state()
        scan = Scan(
            query_id=query.id,
            status=ScanStatus.SKIPPED_BUDGET,
            finished_at=datetime.now(UTC),
            error=(
                f"budget exhausted (remaining={state.remaining}, "
                f"authoritative={state.authoritative})"
            ),
        )
        session.add(scan)
        session.flush()
        raise ScanSkipped(str(scan.error))
    if allowed_pages < wanted_pages:
        log.warning(
            "budget allows %d of %d pages for query %s; scanning shallower",
            allowed_pages,
            wanted_pages,
            query.name,
        )

    scan = Scan(query_id=query.id, status=ScanStatus.RUNNING)
    session.add(scan)
    session.flush()

    observed_at = datetime.now(UTC)
    seen: dict[str, ParsedListing] = {}
    calls = 0
    total_reported = 0
    capped = False
    # Set when paging reaches eBay's own total. Running out of pages first —
    # the query's max_pages, or a short allowance — leaves a best-match window.
    reached_end = False
    excluded = 0

    try:
        for page_index in range(allowed_pages):
            offset = page_index * page_limit
            if offset >= MAX_RESULT_SET:
                capped = True
                break

            page = client.search_page(
                query.q,
                offset=offset,
                limit=page_limit,
                category_ids=query.category_ids,
                filter_expr=effective_filter,
                min_seller_feedback=query.min_seller_feedback,
            )
            calls += 1
            total_reported = page.total
            excluded += page.excluded_low_feedback

            for parsed in page.listings:
                # Within one scan a listing is recorded once, even if eBay's
                # paging shows it twice (it does, when the result set shifts
                # mid-scan). The unique constraint would reject the second row.
                seen.setdefault(parsed.item_id, parsed)

            # `page.listings` can be empty because everything on the page was
            # filtered out, which is not the end of the result set. Page on until
            # eBay's own total says we are done.
            if offset + page_limit >= page.total:
                reached_end = True
                break

        # The result set exceeds what eBay will page through, so what we see is a
        # moving window rather than a stable population.
        if total_reported > MAX_RESULT_SET:
            capped = True
        # Distinct from capped on purpose: the remedy differs. Capped means narrow
        # the query; truncated means raise its page budget (or it ran short of
        # allowance). Both mean the scan cannot vouch for absence.
        truncated = not capped and not reached_end
        if truncated:
            log.warning(
                "query %s stopped at %d of %d pages before eBay's total of %d; "
                "this scan saw a window, not the result set",
                query.name,
                allowed_pages,
                -(-total_reported // page_limit),
                total_reported,
            )
        signature = query_signature(query)

        new_count = 0
        for parsed in seen.values():
            if _upsert_listing(session, parsed, observed_at):
                new_count += 1
            session.add(
                ListingObservation(
                    listing_id=parsed.item_id,
                    scan_id=scan.id,
                    observed_at=observed_at,
                    price=parsed.price,
                    shipping_cost=parsed.shipping_cost,
                    total_cost=parsed.total_cost,
                    currency=parsed.currency,
                )
            )
        session.flush()

        # --- disappearances -------------------------------------------------
        prior = _previous_scan(session, query.id, scan.id)
        disappeared = 0
        if prior is not None:
            gone = find_disappearances(
                _previous_snapshot(session, prior.id),
                set(seen),
                previous_window=ScanWindow(
                    capped=prior.capped,
                    truncated=prior.truncated,
                    query_signature=prior.query_signature,
                ),
                current_window=ScanWindow(
                    capped=capped, truncated=truncated, query_signature=signature
                ),
            )
            for item in gone:
                session.add(
                    ListingDisappearance(
                        listing_item_id=item.item_id,
                        query_id=query.id,
                        cohort_key=item.cohort_key,
                        last_seen_at=prior.started_at,
                        detected_at=observed_at,
                        last_price=item.last_price,
                        last_total_cost=item.last_total_cost,
                        currency=item.currency,
                    )
                )
            disappeared = len(gone)

        # --- cohorts from stored specs ---------------------------------------
        specs = _spec_index(session, {title_hash(p.title) for p in seen.values()})
        cohort_of: dict[str, str] = {}
        units_of: dict[str, float | None] = {}
        unit_of: dict[str, PriceUnit | None] = {}
        # Every member of a cohort shares its category by construction of the
        # key, so the first member's unit is the cohort's unit.
        unit_by_cohort: dict[str, PriceUnit | None] = {}
        for parsed in seen.values():
            spec = specs.get(title_hash(parsed.title))
            key = _cohort_of(spec, parsed.condition_id)
            cohort_of[parsed.item_id] = key
            units_of[parsed.item_id] = _units_of(spec)
            unit_of[parsed.item_id] = _unit_of(spec)
            if key not in unit_by_cohort:
                unit_by_cohort[key] = _unit_of(spec)

        # --- aggregates: computed here, written once, never recomputed -------
        priced = [
            Priced(
                cohort_key=cohort_of[parsed.item_id],
                total_cost=parsed.total_cost,
                currency=parsed.currency,
                units=units_of[parsed.item_id],
            )
            for parsed in seen.values()
        ]
        cohorts = cohort_stats(priced)
        per_unit_by_cohort: dict[str, Stats] = {
            c.cohort_key: c.per_unit for c in cohorts if c.per_unit is not None
        }
        for cohort in cohorts:
            session.add(
                ScanAggregate(
                    scan_id=scan.id,
                    query_id=query.id,
                    observed_at=observed_at,
                    cohort_key=cohort.cohort_key,
                    n=cohort.price.n,
                    currency=cohort.currency,
                    price_min=cohort.price.minimum,
                    price_p10=cohort.price.p10,
                    price_p25=cohort.price.p25,
                    price_median=cohort.price.median,
                    price_mean=cohort.price.mean,
                    # Recorded at write time with the numbers it explains; an
                    # unspecced cohort has neither a unit nor normalized stats.
                    unit=(
                        unit_by_cohort.get(cohort.cohort_key)
                        if cohort.per_unit is not None
                        else None
                    ),
                    per_unit_min=cohort.per_unit.minimum if cohort.per_unit else None,
                    per_unit_p10=cohort.per_unit.p10 if cohort.per_unit else None,
                    per_unit_p25=cohort.per_unit.p25 if cohort.per_unit else None,
                    per_unit_median=cohort.per_unit.median if cohort.per_unit else None,
                    per_unit_mean=cohort.per_unit.mean if cohort.per_unit else None,
                )
            )

        # --- deal flagging ----------------------------------------------------
        flagged = _flag_deals(
            session, scan.id, seen, cohort_of, units_of, unit_of, specs, per_unit_by_cohort
        )

        scan.status = ScanStatus.COMPLETE
        scan.finished_at = datetime.now(UTC)
        scan.api_calls = calls
        scan.result_count = len(seen)
        scan.capped = capped
        scan.truncated = truncated
        scan.query_signature = signature
        scan.min_seller_feedback = query.min_seller_feedback
        scan.excluded_sellers_count = len(excluded_sellers)
        scan.excluded_sellers_digest = exclusions.digest(excluded_sellers) or None
        scan.excluded_low_feedback = excluded
        query.last_scanned_at = observed_at
        query.scan_requested_at = None
        guard.record(calls)
        session.flush()

        return ScanResult(
            scan_id=scan.id,
            status=ScanStatus.COMPLETE,
            observed=len(seen),
            new_listings=new_count,
            disappearances=disappeared,
            cohorts=len(cohorts),
            api_calls=calls,
            capped=capped,
            truncated=truncated,
            deals=flagged,
            excluded_low_feedback=excluded,
        )

    except Exception as exc:
        scan.status = ScanStatus.FAILED
        scan.finished_at = datetime.now(UTC)
        scan.api_calls = calls
        scan.error = f"{type(exc).__name__}: {exc}"
        # Calls already spent count against the budget even though the scan failed.
        guard.record(calls)
        session.flush()
        raise
