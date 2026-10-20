"""End to end: scan -> extract -> scan -> a deal is flagged.

The Plan 002 exit criterion. Everything upstream is machinery; this is the question
the product actually answers.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.fake_ebay import FakeEbay, Generation, item, item_detail
from tests.test_extract_runner import FakeUmans
from touchstone.db.models import Deal, ItemSpec, PriceUnit, Query, ScanAggregate, VerifyOutcome
from touchstone.ebay.budget import BudgetGuard
from touchstone.ebay.client import Credentials, EbayClient
from touchstone.extract.llm import UmansExtractor
from touchstone.extract.runner import run_extraction
from touchstone.scan.runner import ScanResult, run_scan
from touchstone.scan.verify import run_verification
from touchstone.web import views

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
    assert deal.per_unit is not None
    assert float(deal.per_unit) == pytest.approx(1.0)  # $32 / 32GB
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


def test_a_deal_retires_when_its_listing_leaves_the_market(
    session: Session, query: Query
) -> None:
    """The loop the operator actually lives: flag -> listing gone -> flag retired
    from the open feed -> ground truth on request."""
    listings = market(n=8, price=100.0)
    listings.append(item("v1|bargain|0", price=32.0, title=SPEC, seller="cheap_seller"))
    # Next generation: the same market without the bargain.
    fake = FakeEbay(
        generations=[
            Generation(items=listings),
            Generation(items=market(n=8, price=100.0)),
        ]
    )
    # Ended unsold: the stock estimate still says IN_STOCK; the end date does not.
    fake.item_details["v1|bargain|0"] = item_detail(
        stock="IN_STOCK", end_date="2026-01-01T00:00:00.000Z"
    )
    url = fake.start()
    try:
        scan(session, url, query)
        run_extraction(session, extractor=None)
        flagged = scan(session, url, query)
        assert flagged.deals == 1

        fake.advance()
        gone = scan(session, url, query)
        assert gone.disappearances == 1

        # The recorded disappearance retires the flag from the open feed.
        assert views.deal_feed(session) == []
        (deal,) = session.scalars(select(Deal)).all()

        # Ground truth, on request: the scanner asks eBay about the listing.
        deal.verify_requested_at = datetime.now(UTC)
        session.flush()
        with EbayClient(credentials=Credentials("id", "secret"), base_url=url) as client:
            result = run_verification(session, client, BudgetGuard(session, client))
        assert result.verified == 1
    finally:
        fake.stop()

    session.refresh(deal)
    assert deal.verify_outcome == VerifyOutcome.UNAVAILABLE
    assert deal.verify_requested_at is None
    included = views.deal_feed(session, include_dismissed=True)
    assert [view.availability for view in included] == ["verified_unavailable"]


def test_unknown_shipping_cannot_manufacture_a_deal(
    session: Session, query: Query
) -> None:
    """A low item price with unknown delivery cost has no comparable total cost."""
    listings = market(n=8, price=100.0)
    listings.append(
        item(
            "v1|unknown-shipping|0",
            price=32.0,
            shipping=None,
            title=SPEC,
            seller="unknown_shipping_seller",
        )
    )

    fake = FakeEbay(generations=[Generation(items=listings)])
    url = fake.start()
    try:
        scan(session, url, query)
        run_extraction(session, extractor=None)
        result = scan(session, url, query)
    finally:
        fake.stop()

    assert result.deals == 0
    assert session.scalars(select(Deal)).all() == []


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


def test_per_unit_is_recorded_on_aggregates_once_specs_exist(
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
    assert latest.per_unit_median is not None
    # ~$96-101 over 32GB
    assert 2.9 < float(latest.per_unit_median) < 3.3


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


SSD_TITLE = "Micron 7450 PRO 3.84TB U.2 NVMe Enterprise SSD"
E810_TITLE = "Intel E810-XXVDA2 25GbE Dual Port PCI-E Network Adapter"


def ssd_market(n: int = 8, price: float = 180.0) -> list[dict[str, object]]:
    return [
        item(f"v1|ssd{i}|0", price=price + i, title=SSD_TITLE, seller=f"seller_{i}")
        for i in range(n)
    ]


def e810_market(n: int = 8, price: float = 450.0) -> list[dict[str, object]]:
    return [
        item(f"v1|nic{i}|0", price=price + i, title=E810_TITLE, seller=f"seller_{i}")
        for i in range(n)
    ]


def test_a_planted_cheap_ssd_is_flagged_in_a_per_tb_cohort(
    session: Session, query: Query
) -> None:
    listings = ssd_market(n=8, price=180.0)
    listings.append(item("v1|cheapssd|0", price=60.0, title=SSD_TITLE, seller="cheap_seller"))

    fake = FakeEbay(generations=[Generation(items=listings)])
    url = fake.start()
    try:
        scan(session, url, query)
        run_extraction(session, extractor=None)
        result = scan(session, url, query)
    finally:
        fake.stop()

    assert result.deals == 1
    (deal,) = session.scalars(select(Deal)).all()
    assert deal.unit == PriceUnit.TB
    assert deal.cohort_key.startswith("sto|type=SSD|cap=3.84|iface=NVMe|form=U.2")
    assert deal.per_unit is not None
    assert float(deal.per_unit) == pytest.approx(60.0 / 3.84, rel=1e-3)


def test_a_planted_cheap_e810_is_flagged_in_a_model_cohort(
    session: Session, query: Query
) -> None:
    """The per-item path: no regex can read a part number, so the model does."""
    listings = e810_market(n=8, price=450.0)
    listings.append(item("v1|cheapnic|0", price=180.0, title=E810_TITLE, seller="cheap_seller"))

    fake = FakeEbay(generations=[Generation(items=listings)])
    url = fake.start()
    llm = FakeUmans(replies=[json.dumps(
        {"category": "other", "product_model": "E810-XXVDA2", "module_count": 1}
    )])
    llm_url = llm.start()
    try:
        scan(session, url, query)
        with UmansExtractor(api_key="key", base_url=llm_url) as extractor:
            run_extraction(session, extractor=extractor)
        result = scan(session, url, query)
    finally:
        fake.stop()
        llm.stop()

    assert result.deals == 1
    (deal,) = session.scalars(select(Deal)).all()
    assert deal.unit == PriceUnit.ITEM
    assert deal.cohort_key == "itm|model=E810-XXVDA2|cond=3000"
    # Per item, the normalized price IS the delivered price.
    assert deal.per_unit is not None
    assert float(deal.per_unit) == pytest.approx(180.0)
    assert deal.cohort_n == 9
