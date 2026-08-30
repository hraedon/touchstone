"""End to end: scan -> extract -> scan -> a deal is flagged.

The Plan 002 exit criterion. Everything upstream is machinery; this is the question
the product actually answers.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.fake_ebay import FakeEbay, Generation, item
from touchstone.db.models import Deal, ItemSpec, Query, ScanAggregate
from touchstone.ebay.budget import BudgetGuard
from touchstone.ebay.client import Credentials, EbayClient
from touchstone.extract.runner import run_extraction
from touchstone.scan.runner import ScanResult, run_scan

SPEC = "32GB 2Rx4 PC4-2400T-R DDR4 ECC REG RDIMM Server Memory"
OTHER_SPEC = "16GB 1Rx4 PC4-2133P DDR4 ECC REG RDIMM Server Memory"


def market(n: int = 8, price: float = 100.0) -> list[dict[str, object]]:
    """A cohort of comparable listings clustered around one price."""
    return [
        item(f"v1|{i}|0", price=price + i, title=SPEC, seller=f"seller_{i}")
        for i in range(n)
    ]


@pytest.fixture
def query(session: Session) -> Query:
    q = Query(name="ecc-ddr4-deals", q="ECC DDR4 server memory", max_pages=1)
    session.add(q)
    session.flush()
    return q


def scan(session: Session, url: str, query: Query) -> ScanResult:
    with EbayClient(credentials=Credentials("id", "secret"), base_url=url) as client:
        return run_scan(session, client, query, budget=BudgetGuard(session, client))


def test_a_planted_bargain_is_flagged_and_nothing_else_is(
    session: Session, query: Query
) -> None:
    listings = market(n=8, price=100.0)
    # Same spec, a third of the going rate.
    listings.append(item("v1|bargain|0", price=32.0, title=SPEC, seller="cheap_seller"))

    fake = FakeEbay(generations=[Generation(items=listings)])
    url = fake.start()
    try:
        # First scan: listings exist, no specs yet, so everything is unspecced and
        # nothing can be scored. That is the correct cold-start behavior.
        first = scan(session, url, query)
        assert first.deals == 0

        run_extraction(session, extractor=None)

        second = scan(session, url, query)
    finally:
        fake.stop()

    assert second.deals == 1
    (deal,) = session.scalars(select(Deal)).all()
    assert deal.listing_id == "v1|bargain|0"
    assert deal.per_gb is not None
    assert float(deal.per_gb) == pytest.approx(1.0)  # $32 / 32GB
    assert deal.cohort_n == 9


def test_a_deal_is_flagged_once_not_every_scan(session: Session, query: Query) -> None:
    """Re-alerting on the same listing every hour trains you to ignore the feed."""
    listings = market(n=8, price=100.0)
    listings.append(item("v1|bargain|0", price=32.0, title=SPEC, seller="cheap_seller"))
    fake = FakeEbay(generations=[Generation(items=listings)])
    url = fake.start()
    try:
        scan(session, url, query)
        run_extraction(session, extractor=None)
        scan(session, url, query)
        third = scan(session, url, query)
    finally:
        fake.stop()

    assert third.deals == 0
    assert len(session.scalars(select(Deal)).all()) == 1


def test_cheap_listing_in_a_different_cohort_is_not_a_deal(
    session: Session, query: Query
) -> None:
    """A 16GB module at half the price of a 32GB one is not a bargain; it is a
    smaller module. Cohorting is what stops that being flagged."""
    listings = market(n=8, price=100.0)
    listings.append(item("v1|small|0", price=50.0, title=OTHER_SPEC, seller="s_small"))

    fake = FakeEbay(generations=[Generation(items=listings)])
    url = fake.start()
    try:
        scan(session, url, query)
        run_extraction(session, extractor=None)
        result = scan(session, url, query)
    finally:
        fake.stop()

    # $50/16GB = $3.13/GB vs the 32GB cohort's ~$3.16/GB — comparable per GB, and in
    # its own cohort of one it cannot be scored at all.
    assert result.deals == 0
    assert session.scalars(select(Deal)).all() == []


def test_thin_cohort_produces_no_deals_however_cheap(
    session: Session, query: Query
) -> None:
    listings = market(n=3, price=100.0)
    listings.append(item("v1|bargain|0", price=5.0, title=SPEC, seller="cheap_seller"))

    fake = FakeEbay(generations=[Generation(items=listings)])
    url = fake.start()
    try:
        scan(session, url, query)
        run_extraction(session, extractor=None)
        result = scan(session, url, query)
    finally:
        fake.stop()

    # Four listings is under MIN_COHORT_N; a p10 over that is an artifact.
    assert result.deals == 0


def test_unspecced_listings_land_in_their_own_cohort(
    session: Session, query: Query
) -> None:
    """An unreadable title must not be mixed into a real cohort, where it would
    corrupt the reference every other listing is scored against."""
    listings = market(n=6, price=100.0)
    listings.append(
        item("v1|vague|0", price=10.0, title="server memory job lot as pictured")
    )
    fake = FakeEbay(generations=[Generation(items=listings)])
    url = fake.start()
    try:
        scan(session, url, query)
        run_extraction(session, extractor=None)
        scan(session, url, query)
    finally:
        fake.stop()

    keys = {a.cohort_key for a in session.scalars(select(ScanAggregate)).all()}
    assert any(k.startswith("unspecced") for k in keys)
    real = [k for k in keys if not k.startswith("unspecced")]
    assert real, "the specced listings should still form a real cohort"
    # The cheap unreadable listing did not drag the real cohort's floor down.
    assert session.scalars(select(Deal)).all() == []


def test_per_gb_is_recorded_on_aggregates_once_specs_exist(
    session: Session, query: Query
) -> None:
    fake = FakeEbay(generations=[Generation(items=market(n=6, price=96.0))])
    url = fake.start()
    try:
        scan(session, url, query)
        run_extraction(session, extractor=None)
        scan(session, url, query)
    finally:
        fake.stop()

    aggregates = session.scalars(
        select(ScanAggregate).order_by(ScanAggregate.id.desc())
    ).all()
    latest = aggregates[0]
    assert latest.per_gb_median is not None
    # ~$96-101 over 32GB
    assert 2.9 < float(latest.per_gb_median) < 3.3


def test_extraction_covers_the_titles_seen(session: Session, query: Query) -> None:
    fake = FakeEbay(generations=[Generation(items=market(n=4, price=100.0))])
    url = fake.start()
    try:
        scan(session, url, query)
        run = run_extraction(session, extractor=None)
    finally:
        fake.stop()

    # Four listings, one shared title -> one spec.
    assert run.considered == 1
    assert run.by_regex == 1
    spec = session.scalars(select(ItemSpec)).one()
    assert spec.total_gb == 32
    assert spec.form_factor == "RDIMM"
