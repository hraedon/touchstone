"""Builders for the web tests.

Deliberately writes ``scan_aggregate`` directly rather than running a scan: the UI's
whole contract is that it renders what was recorded, so the tests hand it recorded
rows and check what comes out.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from touchstone.db.models import (
    Listing,
    ListingObservation,
    Query,
    Scan,
    ScanAggregate,
    ScanStatus,
)

BASE_TIME = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


def make_query(session: Session, name: str = "ddr4-rdimm", **kwargs: object) -> Query:
    query = Query(name=name, q="ddr4 ecc rdimm", **kwargs)
    session.add(query)
    session.flush()
    return query


def make_scan(
    session: Session,
    query: Query,
    *,
    offset_hours: int = 0,
    min_seller_feedback: int = 1,
    capped: bool = False,
    status: ScanStatus = ScanStatus.COMPLETE,
    result_count: int = 40,
) -> Scan:
    scan = Scan(
        query_id=query.id,
        started_at=BASE_TIME + timedelta(hours=offset_hours),
        finished_at=BASE_TIME + timedelta(hours=offset_hours),
        status=status,
        result_count=result_count,
        api_calls=3,
        min_seller_feedback=min_seller_feedback,
        capped=capped,
    )
    session.add(scan)
    session.flush()
    return scan


def make_aggregate(
    session: Session,
    scan: Scan,
    *,
    cohort_key: str = "gen=DDR4|ff=RDIMM|ecc=y|reg=y|cap=32|mt=2400|rank=2Rx4|cond=3000",
    n: int = 12,
    price_median: float = 60.0,
    per_gb_median: float | None = 1.9,
    per_gb_p10: float | None = 1.4,
) -> ScanAggregate:
    aggregate = ScanAggregate(
        scan_id=scan.id,
        query_id=scan.query_id,
        observed_at=scan.started_at,
        cohort_key=cohort_key,
        n=n,
        currency="USD",
        price_min=price_median * 0.7,
        price_p10=price_median * 0.8,
        price_p25=price_median * 0.9,
        price_median=price_median,
        price_mean=price_median * 1.02,
        per_gb_min=None if per_gb_p10 is None else per_gb_p10 * 0.9,
        per_gb_p10=per_gb_p10,
        per_gb_p25=None if per_gb_p10 is None else per_gb_p10 * 1.1,
        per_gb_median=per_gb_median,
        per_gb_mean=per_gb_median,
    )
    session.add(aggregate)
    session.flush()
    return aggregate


def make_listing(
    session: Session,
    item_id: str,
    title: str,
    *,
    title_hash_value: str | None = None,
    condition_id: str = "3000",
) -> Listing:
    from touchstone.extract.normalize import title_hash

    listing = Listing(
        item_id=item_id,
        title=title,
        title_hash=title_hash_value or title_hash(title),
        condition_id=condition_id,
        item_web_url=f"https://www.ebay.com/itm/{item_id}",
    )
    session.add(listing)
    session.flush()
    return listing


def make_observation(
    session: Session,
    listing: Listing,
    scan: Scan,
    *,
    price: float = 50.0,
    shipping_cost: float | None = 5.0,
) -> ListingObservation:
    observation = ListingObservation(
        listing_id=listing.item_id,
        scan_id=scan.id,
        observed_at=scan.started_at,
        price=price,
        shipping_cost=shipping_cost,
        total_cost=None if shipping_cost is None else price + shipping_cost,
        currency="USD",
    )
    session.add(observation)
    session.flush()
    return observation
