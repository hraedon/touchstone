"""Availability verification for flagged deals: on request, and unasked when due.

One Browse ``getItem`` call per check, executed by the scanner, mapped to three
honest outcomes — ``available``, ``unavailable`` (the end date has passed, the
stock estimate is out, or the API no longer returns the listing), and
``unknown`` (a failure or an ambiguous response, which is not evidence of
anything).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.factories import make_listing, make_query, make_scan
from tests.fake_ebay import FakeEbay, Generation, item_detail
from touchstone.db.models import (
    Deal,
    ExtractionMethod,
    ItemSpec,
    ListingDisappearance,
    Query,
    VerifyOutcome,
)
from touchstone.ebay.budget import BudgetGuard
from touchstone.ebay.client import (
    Credentials,
    EbayClient,
    ItemAvailability,
    parse_item_availability,
)
from touchstone.scan.schedule import run_tick
from touchstone.scan.verify import RECHECK_AFTER, run_verification


@pytest.fixture
def fake() -> FakeEbay:
    return FakeEbay(generations=[Generation(items=[])])


def client_for(fake: FakeEbay) -> EbayClient:
    return EbayClient(credentials=Credentials("id", "secret"), base_url=fake.start())


def seed_deal(
    session: Session,
    item_id: str = "v1|1|0",
    *,
    verify_requested_at: datetime | None = None,
    last_seen_at: datetime | None = None,
    query_last_scanned_at: datetime | None = None,
    score: float = 1.02,
) -> Deal:
    """A flag whose query has never been scanned unless told otherwise, so the
    unasked half of a pass leaves it alone and only a request reaches it."""
    query = make_query(session, name=f"q-{item_id}", last_scanned_at=query_last_scanned_at)
    scan = make_scan(session, query)
    listing = make_listing(
        session, item_id=item_id, title=f"32GB 2Rx4 PC4-2400T ECC REG {item_id}"
    )
    if last_seen_at is not None:
        listing.last_seen_at = last_seen_at
    deal = Deal(
        listing_id=item_id,
        scan_id=scan.id,
        cohort_key="gen=DDR4|cap=32",
        total_cost=38.0,
        per_unit=1.1875,
        cohort_p10=1.8,
        cohort_n=14,
        score=score,
        verify_requested_at=verify_requested_at,
    )
    session.add(deal)
    session.flush()
    return deal


class TestRunVerification:
    def test_a_queued_request_is_honored_and_cleared(
        self, session: Session, fake: FakeEbay
    ) -> None:
        fake.item_details["v1|1|0"] = item_detail()
        requested = datetime.now(UTC) - timedelta(hours=1)
        deal = seed_deal(session, verify_requested_at=requested)
        with client_for(fake) as client:
            result = run_verification(session, client, BudgetGuard(session, client))
        session.refresh(deal)
        assert result.verified == 1
        assert deal.verify_outcome == VerifyOutcome.AVAILABLE
        assert deal.verified_at is not None
        assert deal.verify_requested_at is None

    def test_an_item_the_api_no_longer_returns_records_unavailable(
        self, session: Session, fake: FakeEbay
    ) -> None:
        deal = seed_deal(session, verify_requested_at=datetime.now(UTC))
        with client_for(fake) as client:
            result = run_verification(session, client, BudgetGuard(session, client))
        session.refresh(deal)
        assert result.verified == 1
        assert deal.verify_outcome == VerifyOutcome.UNAVAILABLE

    def test_each_verification_spends_exactly_one_call(
        self, session: Session, fake: FakeEbay
    ) -> None:
        fake.item_details["v1|1|0"] = item_detail()
        seed_deal(session, verify_requested_at=datetime.now(UTC))
        with client_for(fake) as client:
            result = run_verification(session, client, BudgetGuard(session, client))
        assert result.api_calls == 1
        assert fake.get_item_calls == ["v1|1|0"]
        assert fake.search_calls == 0

    def test_no_allowance_leaves_the_request_queued(
        self, session: Session, fake: FakeEbay
    ) -> None:
        """A refusal is not an answer: the request stays queued for a later pass."""
        fake.rate_limit_remaining = 50  # below the operator reserve
        requested = datetime.now(UTC)
        deal = seed_deal(session, verify_requested_at=requested)
        with client_for(fake) as client:
            result = run_verification(session, client, BudgetGuard(session, client))
        session.refresh(deal)
        assert result.verified == 0
        assert result.left_queued == 1
        assert fake.get_item_calls == []
        assert deal.verify_requested_at == requested
        assert deal.verified_at is None

    def test_limit_bounds_the_pass(self, session: Session, fake: FakeEbay) -> None:
        now = datetime.now(UTC)
        seed_deal(session, "v1|1|0", verify_requested_at=now)
        seed_deal(session, "v1|2|0", verify_requested_at=now + timedelta(minutes=1))
        with client_for(fake) as client:
            result = run_verification(
                session, client, BudgetGuard(session, client), limit=1
            )
        assert result.verified == 1
        assert result.left_queued == 1

    def test_deals_without_a_request_are_never_called(
        self, session: Session, fake: FakeEbay
    ) -> None:
        seed_deal(session, "v1|1|0")
        seed_deal(session, "v1|2|0", verify_requested_at=datetime.now(UTC))
        with client_for(fake) as client:
            run_verification(session, client, BudgetGuard(session, client))
        assert fake.get_item_calls == ["v1|2|0"]

    def test_the_scanner_pass_honors_queued_requests(
        self, session: Session, fake: FakeEbay
    ) -> None:
        """run_tick verifies after scanning, so a queued request is answered on
        the CronJob's next pass without any new machinery."""
        fake.item_details["v1|1|0"] = item_detail()
        seed_deal(session, verify_requested_at=datetime.now(UTC))
        # No query is due, so the pass has nothing to scan.
        moment = datetime.now(UTC)
        for query in session.scalars(select(Query)):
            query.last_scanned_at = moment
        session.flush()
        with client_for(fake) as client:
            result = run_tick(session, client)
        assert result.scanned == 0
        assert result.verified == 1


class TestAutomaticChecks:
    """Open flags that nothing else will settle are checked unasked.

    A capped query records no disappearances, so its flags can only retire by
    verification; the pass checks the ones whose listing their query's latest
    scan did not see, and only those.
    """

    NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)

    def unseen(self, session: Session, item_id: str = "v1|1|0", score: float = 1.02) -> Deal:
        return seed_deal(
            session,
            item_id,
            last_seen_at=self.NOW - timedelta(hours=6),
            query_last_scanned_at=self.NOW - timedelta(minutes=10),
            score=score,
        )

    def run(self, session: Session, fake: FakeEbay, auto_limit: int = 10) -> tuple[int, int]:
        with client_for(fake) as client:
            result = run_verification(
                session,
                client,
                BudgetGuard(session, client),
                auto_limit=auto_limit,
                now=self.NOW,
            )
        return result.verified, result.automatic

    def test_an_unseen_flag_is_checked_without_a_request(
        self, session: Session, fake: FakeEbay
    ) -> None:
        fake.item_details["v1|1|0"] = item_detail(stock="OUT_OF_STOCK")
        deal = self.unseen(session)
        assert self.run(session, fake) == (1, 1)
        session.refresh(deal)
        assert deal.verify_outcome == VerifyOutcome.UNAVAILABLE
        assert deal.verified_at == self.NOW

    def test_a_flag_its_query_still_sees_is_not_checked(
        self, session: Session, fake: FakeEbay
    ) -> None:
        """Search only returns live listings; confirming one would waste a call."""
        seen_at = self.NOW - timedelta(minutes=10)
        seed_deal(session, last_seen_at=seen_at, query_last_scanned_at=seen_at)
        assert self.run(session, fake) == (0, 0)
        assert fake.get_item_calls == []

    def test_a_dismissed_flag_is_not_checked(self, session: Session, fake: FakeEbay) -> None:
        deal = self.unseen(session)
        deal.dismissed_at = self.NOW - timedelta(hours=1)
        session.flush()
        assert self.run(session, fake) == (0, 0)

    def test_a_recent_check_is_not_repeated(self, session: Session, fake: FakeEbay) -> None:
        deal = self.unseen(session)
        deal.verified_at = self.NOW - RECHECK_AFTER + timedelta(minutes=5)
        deal.verify_outcome = VerifyOutcome.AVAILABLE
        session.flush()
        assert self.run(session, fake) == (0, 0)

    def test_an_old_available_check_is_repeated(
        self, session: Session, fake: FakeEbay
    ) -> None:
        """A verification is a snapshot, not a lease."""
        deal = self.unseen(session)
        deal.verified_at = self.NOW - RECHECK_AFTER - timedelta(minutes=5)
        deal.verify_outcome = VerifyOutcome.AVAILABLE
        session.flush()
        assert self.run(session, fake) == (1, 1)

    def test_a_flag_already_verified_unavailable_is_not_rechecked(
        self, session: Session, fake: FakeEbay
    ) -> None:
        """Already out of the open feed on that evidence; a check after the
        listing's last sighting is not outranked by anything newer."""
        deal = seed_deal(
            session,
            last_seen_at=self.NOW - timedelta(days=3),
            query_last_scanned_at=self.NOW - timedelta(minutes=10),
        )
        deal.verified_at = self.NOW - timedelta(days=2)
        deal.verify_outcome = VerifyOutcome.UNAVAILABLE
        session.flush()
        assert self.run(session, fake) == (0, 0)

    def test_a_sighting_after_an_unavailable_check_makes_it_due_again(
        self, session: Session, fake: FakeEbay
    ) -> None:
        deal = self.unseen(session)
        deal.verified_at = self.NOW - timedelta(days=3)
        deal.verify_outcome = VerifyOutcome.UNAVAILABLE
        session.flush()
        assert self.run(session, fake) == (1, 1)

    def test_a_flag_retired_by_a_disappearance_is_not_checked(
        self, session: Session, fake: FakeEbay
    ) -> None:
        deal = self.unseen(session)
        query_id = session.scalars(select(Query.id).where(Query.name == "q-v1|1|0")).one()
        session.add(
            ListingDisappearance(
                listing_item_id=deal.listing_id,
                query_id=query_id,
                cohort_key=deal.cohort_key,
                last_seen_at=self.NOW - timedelta(hours=6),
                detected_at=self.NOW - timedelta(hours=5),
                last_price=38.0,
                currency="USD",
            )
        )
        session.flush()
        assert self.run(session, fake) == (0, 0)

    def test_requests_are_answered_first_and_auto_limit_bounds_the_rest(
        self, session: Session, fake: FakeEbay
    ) -> None:
        self.unseen(session, "v1|a|0", score=9.0)
        self.unseen(session, "v1|b|0", score=8.0)
        seed_deal(session, "v1|asked|0", verify_requested_at=self.NOW - timedelta(hours=1))
        assert self.run(session, fake, auto_limit=1) == (2, 1)
        # The request first, then the highest-scored unseen flag.
        assert fake.get_item_calls == ["v1|asked|0", "v1|a|0"]

    def test_no_unasked_check_runs_ahead_of_a_request_left_queued(
        self, session: Session, fake: FakeEbay
    ) -> None:
        fake.rate_limit_remaining = 50  # below the operator reserve
        self.unseen(session)
        seed_deal(session, "v1|asked|0", verify_requested_at=self.NOW)
        assert self.run(session, fake) == (0, 0)
        assert fake.get_item_calls == []

    def test_the_scanner_pass_checks_unseen_flags(
        self, session: Session, fake: FakeEbay
    ) -> None:
        moment = datetime.now(UTC)
        seed_deal(
            session,
            last_seen_at=moment - timedelta(hours=6),
            query_last_scanned_at=moment - timedelta(minutes=10),
        )
        # Not due, so the pass has nothing to scan and goes straight to checks.
        for query in session.scalars(select(Query)):
            query.cadence_minutes = 600
        session.flush()
        with client_for(fake) as client:
            result = run_tick(session, client)
        assert result.scanned == 0
        assert (result.verified, result.verified_unasked) == (1, 1)


class TestParseItemAvailability:
    """The getItem body has no listing-status field; these are the signals it has.

    Shapes taken from the live API: an ended listing keeps answering 200 for
    weeks, and one that ended unsold still estimates ``IN_STOCK`` — so the end
    date is read first and outranks the stock estimate.
    """

    NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)

    def parse(self, body: object) -> ItemAvailability | None:
        return parse_item_availability(body, now=self.NOW)

    def test_in_stock_with_no_end_date_is_available(self) -> None:
        assert self.parse(item_detail()) is ItemAvailability.AVAILABLE

    def test_limited_stock_is_available(self) -> None:
        assert self.parse(item_detail(stock="LIMITED_STOCK")) is ItemAvailability.AVAILABLE

    def test_a_future_end_date_does_not_retire_a_live_listing(self) -> None:
        body = item_detail(end_date="2026-10-27T12:00:00.000Z")
        assert self.parse(body) is ItemAvailability.AVAILABLE

    def test_out_of_stock_is_unavailable(self) -> None:
        assert self.parse(item_detail(stock="OUT_OF_STOCK")) is ItemAvailability.UNAVAILABLE

    def test_a_past_end_date_outranks_in_stock(self) -> None:
        """Observed: a listing that ended unsold still says IN_STOCK."""
        body = item_detail(stock="IN_STOCK", end_date="2026-09-21T21:17:51.000Z")
        assert self.parse(body) is ItemAvailability.UNAVAILABLE

    def test_a_past_end_date_alone_is_unavailable(self) -> None:
        body = item_detail(stock=None, end_date="2026-09-21T21:17:51.000Z")
        assert self.parse(body) is ItemAvailability.UNAVAILABLE

    def test_an_unrecognized_stock_status_is_unknown(self) -> None:
        """eBay can grow statuses; guessing what a new one means would turn a
        parse failure into a market claim."""
        assert self.parse(item_detail(stock="BACKORDERED_MAYBE")) is None

    def test_no_signal_at_all_is_unknown(self) -> None:
        assert self.parse(item_detail(stock=None)) is None

    def test_an_unparseable_end_date_is_not_evidence(self) -> None:
        assert self.parse(item_detail(stock=None, end_date="last tuesday")) is None

    def test_the_invented_listing_status_shape_is_unknown(self) -> None:
        """The shape the first version of this parser read does not exist on
        the real API; a response carrying only it says nothing."""
        assert self.parse({"listing": {"status": "ACTIVE"}}) is None


class TestGetItemMapping:
    """The client turns one getItem response into one honest outcome.

    The fake's item responses carry a ``seller`` node on purpose: the real
    getItem returns it, and the client's posture is to read the end date and
    stock estimate and discard everything else at this boundary — same as the
    search path drops the username. Nothing returned here can carry seller data
    because the return type is one enum member.
    """

    def test_an_active_listing_is_available(self, fake: FakeEbay) -> None:
        fake.item_details["v1|1|0"] = item_detail()
        with client_for(fake) as client:
            assert client.get_item("v1|1|0") is ItemAvailability.AVAILABLE

    def test_an_ended_listing_is_unavailable(self, fake: FakeEbay) -> None:
        fake.item_details["v1|1|0"] = item_detail(end_date="2026-01-01T00:00:00.000Z")
        with client_for(fake) as client:
            assert client.get_item("v1|1|0") is ItemAvailability.UNAVAILABLE

    def test_a_sold_out_listing_is_unavailable(self, fake: FakeEbay) -> None:
        fake.item_details["v1|1|0"] = item_detail(stock="OUT_OF_STOCK")
        with client_for(fake) as client:
            assert client.get_item("v1|1|0") is ItemAvailability.UNAVAILABLE

    def test_an_id_the_api_no_longer_returns_is_unavailable(
        self, fake: FakeEbay
    ) -> None:
        with client_for(fake) as client:
            assert client.get_item("v1|404|0") is ItemAvailability.UNAVAILABLE

    def test_a_server_error_is_unknown_not_unavailable(self, fake: FakeEbay) -> None:
        """eBay has transient 404/5xx incidents; a failure is not evidence that
        a listing ended."""
        fake.item_endpoint_fails.add("v1|1|0")
        with client_for(fake) as client:
            assert client.get_item("v1|1|0") is ItemAvailability.UNKNOWN

    def test_a_response_without_a_signal_is_unknown(self, fake: FakeEbay) -> None:
        fake.item_details["v1|1|0"] = item_detail(stock=None)
        with client_for(fake) as client:
            assert client.get_item("v1|1|0") is ItemAvailability.UNKNOWN

    def test_each_call_costs_exactly_one_unit(self, fake: FakeEbay) -> None:
        fake.item_details["v1|1|0"] = item_detail()
        with client_for(fake) as client:
            assert client.calls_made == 0
            client.get_item("v1|1|0")
            client.get_item("v1|404|0")
            assert client.calls_made == 2
            assert fake.get_item_calls == ["v1|1|0", "v1|404|0"]


class TestEnumStorageVocabulary:
    """The new enums persist their documented values, not their member names.

    The migrations backfill and declare lowercase ('gb', 'memory',
    'available'); the ORM must write the same vocabulary or every row the
    migration wrote becomes unreadable — a defect the live deploy of the
    categories round caught and this test now pins.
    """

    def test_the_new_enums_store_values_not_names(self, session: Session) -> None:
        from sqlalchemy import text

        from touchstone.db.models import Deal as DealRow
        from touchstone.db.models import PriceUnit as PU
        from touchstone.db.models import SpecCategory as SC
        from touchstone.db.models import VerifyOutcome as VO

        query = make_query(session, name="vocab-q")
        scan = make_scan(session, query)
        listing = make_listing(session, "v1|vocab|0", "32GB 2Rx4 PC4-2400T ECC REG")
        session.add(
            ItemSpec(
                category=SC.MEMORY,
                title_hash=listing.title_hash,
                normalized_title="t",
                total_gb=32,
                method=ExtractionMethod.REGEX,
                confidence=0.9,
            )
        )
        session.add(
            DealRow(
                listing_id=listing.item_id,
                scan_id=scan.id,
                cohort_key="k",
                total_cost=10.0,
                per_unit=1.2,
                unit=PU.GB,
                verify_outcome=VO.AVAILABLE,
                verified_at=datetime.now(UTC),
                cohort_p10=1.6,
                cohort_n=5,
                score=1.0,
            )
        )
        session.flush()
        raw_unit = session.execute(
            text("select unit, verify_outcome from deal where listing_id = 'v1|vocab|0'")
        ).one()
        raw_category = session.execute(
            text("select category from item_spec where title_hash = :h"),
            {"h": listing.title_hash},
        ).scalar_one()
        assert raw_unit == ("gb", "available")
        assert raw_category == "memory"
