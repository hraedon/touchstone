"""On-request availability verification for flagged deals.

One Browse ``getItem`` call per queued request, executed by the scanner, mapped
to three honest outcomes — ``available``, ``unavailable`` (the API no longer
returns the listing, which is what it reports for ended ones), and ``unknown``
(a failure or an ambiguous response, which is not evidence of anything).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.factories import make_listing, make_query, make_scan
from tests.fake_ebay import FakeEbay, Generation
from touchstone.db.models import Deal, Query, VerifyOutcome
from touchstone.ebay.budget import BudgetGuard
from touchstone.ebay.client import Credentials, EbayClient, ItemAvailability
from touchstone.scan.schedule import run_tick
from touchstone.scan.verify import run_verification


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
) -> Deal:
    query = make_query(session, name=f"q-{item_id}")
    scan = make_scan(session, query)
    make_listing(session, item_id=item_id, title="32GB 2Rx4 PC4-2400T ECC REG")
    deal = Deal(
        listing_id=item_id,
        scan_id=scan.id,
        cohort_key="gen=DDR4|cap=32",
        total_cost=38.0,
        per_gb=1.1875,
        cohort_p10=1.8,
        cohort_n=14,
        score=1.02,
        verify_requested_at=verify_requested_at,
    )
    session.add(deal)
    session.flush()
    return deal


class TestRunVerification:
    def test_a_queued_request_is_honored_and_cleared(
        self, session: Session, fake: FakeEbay
    ) -> None:
        fake.item_status["v1|1|0"] = "ACTIVE"
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
        fake.item_status["v1|1|0"] = "ACTIVE"
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
        fake.item_status["v1|1|0"] = "ACTIVE"
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


class TestGetItemMapping:
    """The client turns one getItem response into one honest outcome.

    The fake's item responses carry a ``seller`` node on purpose: the real
    getItem returns it, and the client's posture is to read the listing status
    and discard everything else at this boundary — same as the search path
    drops the username. Nothing returned here can carry seller data because the
    return type is one enum member.
    """

    def test_an_active_listing_is_available(self, fake: FakeEbay) -> None:
        fake.item_status["v1|1|0"] = "ACTIVE"
        with client_for(fake) as client:
            assert client.get_item("v1|1|0") is ItemAvailability.AVAILABLE

    def test_an_ended_listing_is_unavailable(self, fake: FakeEbay) -> None:
        fake.item_status["v1|1|0"] = "ENDED"
        with client_for(fake) as client:
            assert client.get_item("v1|1|0") is ItemAvailability.UNAVAILABLE

    def test_a_completed_listing_is_unavailable(self, fake: FakeEbay) -> None:
        fake.item_status["v1|1|0"] = "COMPLETED"
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

    def test_an_unrecognized_status_is_unknown(self, fake: FakeEbay) -> None:
        """eBay can grow statuses; guessing what a new one means would turn a
        parse failure into a market claim."""
        fake.item_status["v1|1|0"] = "SUSPENDED_FOR_REASONABILITY_CHECK"
        with client_for(fake) as client:
            assert client.get_item("v1|1|0") is ItemAvailability.UNKNOWN

    def test_a_response_without_a_status_is_unknown(self, fake: FakeEbay) -> None:
        fake.item_status["v1|1|0"] = ""
        with client_for(fake) as client:
            assert client.get_item("v1|1|0") is ItemAvailability.UNKNOWN

    def test_each_call_costs_exactly_one_unit(self, fake: FakeEbay) -> None:
        fake.item_status["v1|1|0"] = "ACTIVE"
        with client_for(fake) as client:
            assert client.calls_made == 0
            client.get_item("v1|1|0")
            client.get_item("v1|404|0")
            assert client.calls_made == 2
            assert fake.get_item_calls == ["v1|1|0", "v1|404|0"]
