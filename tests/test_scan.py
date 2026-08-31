"""End-to-end scan behavior against a fake eBay.

The most important test in this file is
``test_aggregates_survive_a_listing_purge_unchanged``. Every claim touchstone makes
about historical trends rests on that property.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from tests.fake_ebay import FakeEbay, Generation, item
from touchstone.db.models import (
    Listing,
    ListingDisappearance,
    ListingObservation,
    Query,
    RateBudget,
    Scan,
    ScanAggregate,
    ScanStatus,
)
from touchstone.ebay.budget import BudgetGuard, today_utc
from touchstone.ebay.client import Credentials, EbayClient, parse_item_summary
from touchstone.scan.runner import ScanResult, ScanSkipped, run_scan

MakeQuery = Callable[..., Query]


@pytest.fixture
def make_query(session: Session) -> MakeQuery:
    counter = {"n": 0}

    def _make(max_pages: int = 5) -> Query:
        counter["n"] += 1
        query = Query(
            name=f"ecc-ddr4-{counter['n']}",
            q="ECC DDR4 server memory",
            max_pages=max_pages,
        )
        session.add(query)
        session.flush()
        return query

    return _make


def run(session: Session, fake: FakeEbay, query: Query, base_url: str) -> ScanResult:
    with EbayClient(credentials=Credentials("id", "secret"), base_url=base_url) as client:
        return run_scan(session, client, query, budget=BudgetGuard(session, client))


def test_scan_records_observations_and_aggregates(
    session: Session, make_query: MakeQuery
) -> None:
    fake = FakeEbay(
        generations=[
            Generation(
                items=[
                    item("v1|1|0", price=100.0),
                    item("v1|2|0", price=200.0),
                    item("v1|3|0", price=300.0, condition="New", condition_id="1000"),
                ]
            )
        ]
    )
    url = fake.start()
    query = make_query()
    try:
        result = run(session, fake, query, url)
    finally:
        fake.stop()

    assert result.status is ScanStatus.COMPLETE
    assert result.observed == 3
    assert result.new_listings == 3

    observations = session.scalars(select(ListingObservation)).all()
    assert len(observations) == 3
    # total_cost includes shipping (0.00 in these fixtures)
    totals = [o.total_cost for o in observations]
    assert None not in totals
    assert sorted(float(total) for total in totals if total is not None) == [
        100.0,
        200.0,
        300.0,
    ]

    # Two conditions -> two cohorts.
    aggregates = session.scalars(select(ScanAggregate)).all()
    assert len(aggregates) == 2
    used = {a.cohort_key: a for a in aggregates}
    new_cohort = next(a for k, a in used.items() if k.endswith("cond=1000"))
    assert new_cohort.n == 1
    assert float(new_cohort.price_median) == 300.0


def test_shipping_is_included_in_total_cost(
    session: Session, make_query: MakeQuery
) -> None:
    fake = FakeEbay(generations=[Generation(items=[item("v1|1|0", price=90.0, shipping=12.5)])])
    url = fake.start()
    query = make_query()
    try:
        run(session, fake, query, url)
    finally:
        fake.stop()

    (obs,) = session.scalars(select(ListingObservation)).all()
    assert float(obs.price) == 90.0
    assert obs.shipping_cost is not None
    assert float(obs.shipping_cost) == 12.5
    assert obs.total_cost is not None
    assert float(obs.total_cost) == 102.5


def test_missing_shipping_keeps_total_unknown_and_out_of_aggregates(
    session: Session, make_query: MakeQuery
) -> None:
    """Unknown shipping is not free shipping.

    The listing remains an observation because its item price is a fact, but it must
    not contribute a fabricated total to the asking-cost distribution.
    """
    unknown = item(
        "v1|unknown|0",
        price=1.0,
        shipping=None,
        shipping_options_without_cost=True,
    )
    parsed = parse_item_summary(unknown)
    assert parsed is not None
    assert parsed.shipping_cost is None
    assert parsed.total_cost is None

    free = parse_item_summary(item("v1|free|0", price=1.0, shipping=0.0))
    assert free is not None
    assert free.shipping_cost == 0.0
    assert free.total_cost == 1.0

    fake = FakeEbay(
        generations=[
            Generation(
                items=[
                    item("v1|known-1|0", price=100.0),
                    item("v1|known-2|0", price=200.0),
                    unknown,
                ]
            )
        ]
    )
    url = fake.start()
    query = make_query()
    try:
        result = run(session, fake, query, url)
    finally:
        fake.stop()

    assert result.observed == 3
    observation = session.scalars(
        select(ListingObservation).where(ListingObservation.listing_id == "v1|unknown|0")
    ).one()
    assert observation.price == 1.0
    assert observation.shipping_cost is None
    assert observation.total_cost is None

    (aggregate,) = session.scalars(select(ScanAggregate)).all()
    assert aggregate.n == 2
    assert float(aggregate.price_min) == 100.0


def test_disappearance_preserves_an_unknown_last_total(
    session: Session, make_query: MakeQuery
) -> None:
    fake = FakeEbay(
        generations=[
            Generation(items=[item("v1|unknown|0", price=100.0, shipping=None)]),
            Generation(items=[]),
        ]
    )
    url = fake.start()
    query = make_query()
    try:
        run(session, fake, query, url)
        fake.advance()
        run(session, fake, query, url)
    finally:
        fake.stop()

    (gone,) = session.scalars(select(ListingDisappearance)).all()
    assert float(gone.last_price) == 100.0
    assert gone.last_total_cost is None


def test_price_change_across_scans_yields_two_observations(
    session: Session, make_query: MakeQuery
) -> None:
    fake = FakeEbay(
        generations=[
            Generation(items=[item("v1|1|0", price=100.0)]),
            Generation(items=[item("v1|1|0", price=85.0)]),
        ]
    )
    url = fake.start()
    query = make_query()
    try:
        run(session, fake, query, url)
        fake.advance()
        run(session, fake, query, url)
    finally:
        fake.stop()

    # One listing, two observations at two prices.
    assert len(session.scalars(select(Listing)).all()) == 1
    prices = sorted(
        float(o.price)
        for o in session.scalars(
            select(ListingObservation).where(ListingObservation.listing_id == "v1|1|0")
        ).all()
    )
    assert prices == [85.0, 100.0]


def test_vanished_listing_is_recorded_as_a_disappearance(
    session: Session, make_query: MakeQuery
) -> None:
    fake = FakeEbay(
        generations=[
            Generation(items=[item("v1|1|0", price=100.0), item("v1|2|0", price=250.0)]),
            Generation(items=[item("v1|1|0", price=100.0)]),
        ]
    )
    url = fake.start()
    query = make_query()
    try:
        run(session, fake, query, url)
        fake.advance()
        result = run(session, fake, query, url)
    finally:
        fake.stop()

    assert result.disappearances == 1
    (gone,) = session.scalars(select(ListingDisappearance)).all()
    assert gone.listing_item_id == "v1|2|0"
    assert gone.last_total_cost is not None
    assert float(gone.last_total_cost) == 250.0


def test_capped_scan_records_capped_and_suppresses_disappearances(
    session: Session, make_query: MakeQuery
) -> None:
    """Past eBay's 10,000-item ceiling, absence is a paging artifact."""
    fake = FakeEbay(
        generations=[
            Generation(items=[item("v1|1|0", price=100.0)], reported_total=25_000),
            Generation(items=[item("v1|9|0", price=100.0)], reported_total=25_000),
        ]
    )
    url = fake.start()
    query = make_query(max_pages=1)
    try:
        first = run(session, fake, query, url)
        fake.advance()
        second = run(session, fake, query, url)
    finally:
        fake.stop()

    assert first.capped is True
    assert second.capped is True
    # v1|1|0 vanished, but the scan was capped, so it is not a market event.
    assert second.disappearances == 0
    assert session.scalars(select(ListingDisappearance)).all() == []


def test_aggregates_survive_a_listing_deletion_unchanged(
    session: Session, make_query: MakeQuery
) -> None:
    """Recorded history must not change when listing rows go away.

    The original reason was an account-deletion purge; that reason is gone, because
    no seller data is stored and a deletion now erases nothing. The property is
    still load-bearing for retention pruning: old listing rows will eventually be
    dropped to bound table growth, and if aggregates were derived from them every
    historical chart would silently rewrite itself. They are materialized at scan
    time precisely so they don't.
    """
    fake = FakeEbay(
        generations=[
            Generation(
                items=[
                    item("v1|1|0", price=100.0),
                    item("v1|2|0", price=200.0),
                    item("v1|3|0", price=300.0),
                ]
            )
        ]
    )
    url = fake.start()
    query = make_query()
    try:
        run(session, fake, query, url)
    finally:
        fake.stop()

    before = [
        (a.cohort_key, a.n, float(a.price_median), float(a.price_mean), float(a.price_min))
        for a in session.scalars(select(ScanAggregate).order_by(ScanAggregate.id)).all()
    ]
    assert before and before[0][1] == 3

    # Prune a listing, as a retention pass eventually will.
    session.execute(delete(Listing).where(Listing.item_id == "v1|1|0"))
    session.flush()

    assert session.get(Listing, "v1|1|0") is None
    # Observations cascade with the listing — no orphans left behind.
    remaining = session.scalars(
        select(ListingObservation).where(ListingObservation.listing_id == "v1|1|0")
    ).all()
    assert remaining == []

    after = [
        (a.cohort_key, a.n, float(a.price_median), float(a.price_mean), float(a.price_min))
        for a in session.scalars(select(ScanAggregate).order_by(ScanAggregate.id)).all()
    ]
    assert after == before, "deleting a listing must not rewrite recorded history"


def test_scan_is_skipped_when_budget_is_exhausted(
    session: Session, make_query: MakeQuery
) -> None:
    fake = FakeEbay(generations=[Generation(items=[item("v1|1|0", price=100.0)])])
    fake.rate_limit_remaining = 0
    url = fake.start()
    query = make_query()
    try:
        with pytest.raises(ScanSkipped):
            run(session, fake, query, url)
    finally:
        fake.stop()

    # The refusal is recorded, not silent.
    (scan,) = session.scalars(select(Scan)).all()
    assert scan.status is ScanStatus.SKIPPED_BUDGET
    assert session.scalars(select(ListingObservation)).all() == []


def test_exhausted_budget_does_not_fall_through_to_scanning(
    session: Session, make_query: MakeQuery
) -> None:
    """An unreadable quota with a spent ledger must still refuse."""
    fake = FakeEbay(generations=[Generation(items=[item("v1|1|0", price=100.0)])])
    fake.rate_limit_fails = True
    session.add(RateBudget(day=today_utc(), calls_used=5000, calls_limit=5000))
    session.flush()
    url = fake.start()
    query = make_query()
    try:
        with pytest.raises(ScanSkipped):
            run(session, fake, query, url)
    finally:
        fake.stop()
    assert fake.search_calls == 0


def test_token_is_minted_once_across_multiple_pages(
    session: Session, make_query: MakeQuery
) -> None:
    """The OAuth endpoint allows 1,000 calls/day; an uncached mint per search would
    exhaust it long before the search budget."""
    items = [item(f"v1|{i}|0", price=100.0 + i) for i in range(5)]
    fake = FakeEbay(generations=[Generation(items=items)])
    url = fake.start()
    query = make_query(max_pages=3)
    try:
        with EbayClient(
            credentials=Credentials("id", "secret"), base_url=url
        ) as client:
            run_scan(session, client, query, budget=BudgetGuard(session, client), page_limit=2)
    finally:
        fake.stop()

    assert fake.search_calls >= 2
    assert fake.token_mints == 1


def test_listing_seen_twice_in_one_scan_yields_one_observation(
    session: Session, make_query: MakeQuery
) -> None:
    """eBay's paging can show the same item twice when the result set shifts
    mid-scan. The unique constraint would reject the second row."""
    duplicated = [item("v1|1|0", price=100.0), item("v1|1|0", price=100.0)]
    fake = FakeEbay(generations=[Generation(items=duplicated)])
    url = fake.start()
    query = make_query(max_pages=2)
    try:
        with EbayClient(
            credentials=Credentials("id", "secret"), base_url=url
        ) as client:
            result = run_scan(
                session, client, query, budget=BudgetGuard(session, client), page_limit=1
            )
    finally:
        fake.stop()

    assert result.observed == 1
    assert len(session.scalars(select(ListingObservation)).all()) == 1


def test_budget_is_charged_for_calls_actually_made(
    session: Session, make_query: MakeQuery
) -> None:
    fake = FakeEbay(generations=[Generation(items=[item("v1|1|0", price=100.0)])])
    fake.rate_limit_fails = True  # force ledger accounting
    url = fake.start()
    query = make_query(max_pages=1)
    try:
        result = run(session, fake, query, url)
    finally:
        fake.stop()

    row = session.get(RateBudget, today_utc())
    assert row is not None
    assert row.calls_used == result.api_calls
    assert row.calls_used >= 1


class TestSellerFeedbackFloor:
    """Excluding zero-feedback sellers.

    The score is read in the API client to make the decision and discarded there —
    it never reaches ParsedListing, so it cannot reach the database. Same boundary
    that keeps the username out.
    """

    def test_zero_feedback_sellers_are_excluded(
        self, session: Session, make_query: MakeQuery
    ) -> None:
        fake = FakeEbay(
            generations=[
                Generation(
                    items=[
                        item("v1|good|0", price=100.0, feedback_score=250),
                        item("v1|scam|0", price=5.0, feedback_score=0),
                    ]
                )
            ]
        )
        url = fake.start()
        query = make_query()
        query.min_seller_feedback = 1
        try:
            result = run(session, fake, query, url)
        finally:
            fake.stop()

        assert result.observed == 1
        assert result.excluded_low_feedback == 1
        assert session.get(Listing, "v1|good|0") is not None
        assert session.get(Listing, "v1|scam|0") is None

    def test_the_exclusion_count_is_recorded_on_the_scan(
        self, session: Session, make_query: MakeQuery
    ) -> None:
        """A filter that quietly eats most of a result set is otherwise invisible —
        the numbers just look calmer."""
        items = [item(f"v1|z{i}|0", price=5.0, feedback_score=0) for i in range(4)]
        items.append(item("v1|good|0", price=100.0, feedback_score=99))
        fake = FakeEbay(generations=[Generation(items=items)])
        url = fake.start()
        query = make_query()
        query.min_seller_feedback = 1
        try:
            run(session, fake, query, url)
        finally:
            fake.stop()

        (scan,) = session.scalars(select(Scan)).all()
        assert scan.excluded_low_feedback == 4
        assert scan.result_count == 1

    def test_the_threshold_used_is_recorded_per_scan(
        self, session: Session, make_query: MakeQuery
    ) -> None:
        """Changing the floor changes the population being sampled, which makes the
        series discontinuous. A discontinuity you cannot see is indistinguishable
        from a market move."""
        fake = FakeEbay(
            generations=[Generation(items=[item("v1|a|0", price=100.0, feedback_score=50)])]
        )
        url = fake.start()
        query = make_query()
        try:
            query.min_seller_feedback = 1
            run(session, fake, query, url)
            query.min_seller_feedback = 25
            run(session, fake, query, url)
        finally:
            fake.stop()

        thresholds = [
            s.min_seller_feedback
            for s in session.scalars(select(Scan).order_by(Scan.id)).all()
        ]
        assert thresholds == [1, 25]

    def test_a_missing_feedback_score_is_kept_not_dropped(
        self, session: Session, make_query: MakeQuery
    ) -> None:
        """Unknown is not the same as zero. Dropping on absence would silently
        discard legitimate listings whenever eBay omits the field; this is a noise
        filter, so it should fail toward including too much, where the effect shows
        up in the data, rather than excluding too much, where it does not."""
        fake = FakeEbay(
            generations=[Generation(items=[item("v1|a|0", price=100.0, feedback_score=None)])]
        )
        url = fake.start()
        query = make_query()
        query.min_seller_feedback = 1
        try:
            result = run(session, fake, query, url)
        finally:
            fake.stop()

        assert result.observed == 1
        assert result.excluded_low_feedback == 0

    def test_threshold_of_zero_disables_the_filter(
        self, session: Session, make_query: MakeQuery
    ) -> None:
        fake = FakeEbay(
            generations=[Generation(items=[item("v1|a|0", price=5.0, feedback_score=0)])]
        )
        url = fake.start()
        query = make_query()
        query.min_seller_feedback = 0
        try:
            result = run(session, fake, query, url)
        finally:
            fake.stop()

        assert result.observed == 1

    def test_the_feedback_score_never_reaches_a_parsed_listing(self) -> None:
        raw = item("v1|a|0", price=100.0, feedback_score=1234)
        parsed = parse_item_summary(raw, min_seller_feedback=1)
        assert parsed is not None
        assert "1234" not in repr(parsed)
        assert not any("feedback" in f for f in type(parsed).__dataclass_fields__)
