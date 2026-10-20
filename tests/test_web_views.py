"""The read models, tested directly where behaviour is easier to pin than in HTML."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

from tests.factories import (
    BASE_TIME,
    make_aggregate,
    make_listing,
    make_query,
    make_scan,
)
from touchstone.db.models import (
    Deal,
    Listing,
    ListingDisappearance,
    Query,
    RateBudget,
    ScanStatus,
    VerifyOutcome,
)
from touchstone.scan.aggregate import MIN_COHORT_N
from touchstone.web import views

RDIMM_TITLE = "32GB 2Rx4 PC4-2400T-R DDR4 ECC REG RDIMM Server Memory"


class TestLegacyCohortKeys:
    @pytest.mark.parametrize(
        ("key", "legacy"),
        [
            ("q=7|cond=3000", True),
            ("q=1|cond=unknown", True),
            ("gen=DDR4|ff=RDIMM|ecc=y|reg=y|cap=32|mt=2400|rank=2Rx4|cond=3000", False),
            ("unspecced|cond=3000", False),
        ],
    )
    def test_only_the_plan_001_shape_counts_as_legacy(self, key: str, legacy: bool) -> None:
        assert views.is_legacy_cohort_key(key) is legacy

    def test_an_unrecognized_future_key_is_not_relabelled_as_ancient(self) -> None:
        """A negative test for the current format would misfile a future one."""
        assert views.is_legacy_cohort_key("v3|gen=DDR5|cond=1000") is False


class TestSuppressionBoundary:
    @pytest.mark.parametrize("n", range(0, MIN_COHORT_N))
    def test_below_the_floor_is_suppressed(self, session: Session, n: int) -> None:
        query = make_query(session, name=f"n-{n}")
        scan = make_scan(session, query)
        make_aggregate(session, scan, n=n)
        session.flush()
        trend = views.query_trend(session, query.id)
        assert trend is not None
        assert trend.series[0].points[0].suppressed is True
        assert trend.series[0].shown_points == ()

    def test_at_the_floor_it_is_reportable(self, session: Session) -> None:
        query = make_query(session, name="at-floor")
        scan = make_scan(session, query)
        make_aggregate(session, scan, n=MIN_COHORT_N)
        session.flush()
        trend = views.query_trend(session, query.id)
        assert trend is not None
        assert trend.series[0].points[0].suppressed is False


class TestDiscontinuityDetection:
    def _rows(self, floors: list[int], statuses: list[ScanStatus] | None = None
              ) -> list[views.ScanRow]:
        statuses = statuses or [ScanStatus.COMPLETE] * len(floors)
        from datetime import timedelta

        return [
            views.ScanRow(
                id=index,
                started_at=BASE_TIME + timedelta(hours=index),
                status=status,
                result_count=10,
                excluded_low_feedback=0,
                min_seller_feedback=floor,
                api_calls=1,
                capped=False,
                error=None,
            )
            for index, (floor, status) in enumerate(zip(floors, statuses, strict=True))
        ]

    def test_one_marker_per_change(self) -> None:
        markers = views.feedback_floor_discontinuities(self._rows([1, 1, 10, 10, 0]))
        assert [marker.note for marker in markers] == ["floor 1 to 10", "floor 10 to 0"]

    def test_the_first_scan_is_never_a_change(self) -> None:
        assert views.feedback_floor_discontinuities(self._rows([5])) == []

    def test_incomplete_scans_are_ignored(self) -> None:
        markers = views.feedback_floor_discontinuities(
            self._rows(
                [1, 99, 1],
                [ScanStatus.COMPLETE, ScanStatus.SKIPPED_BUDGET, ScanStatus.COMPLETE],
            )
        )
        assert markers == []

    def test_every_discontinuity_kind_has_a_label_and_a_reason(self) -> None:
        """assert_never guards the dispatch; this proves both arms are reachable."""
        for kind in views.DiscontinuityKind:
            assert views.discontinuity_label(kind)
            assert views.discontinuity_detail(kind)


class TestBudgetView:
    def test_the_web_face_never_reports_an_authoritative_figure(self, session: Session) -> None:
        """A page view must not spend a call to find out, so it never claims to know."""
        session.add(
            RateBudget(
                day=views.budget_view(session).day,
                calls_used=100,
                calls_limit=5000,
                last_authoritative_read=BASE_TIME,
                last_authoritative_remaining=4900,
            )
        )
        session.flush()
        budget = views.budget_view(session)
        assert budget.authoritative is False
        assert budget.remaining == 4900
        assert budget.usable == 4900 - budget.reserve
        assert budget.last_authoritative_read == BASE_TIME

    def test_no_ledger_row_yet_reports_zero_used_not_unlimited(self, session: Session) -> None:
        budget = views.budget_view(session)
        assert budget.calls_used == 0
        assert budget.calls_limit > 0
        assert budget.authoritative is False


class TestProjectedCost:
    @pytest.mark.parametrize(
        ("cadence", "pages", "enabled", "expected"),
        [
            (60, 5, True, 120),
            (1440, 5, True, 5),
            (60, 5, False, 0),
            (60, 0, True, 24),
        ],
    )
    def test_worst_case_daily_calls(
        self, session: Session, cadence: int, pages: int, enabled: bool, expected: int
    ) -> None:
        make_query(
            session,
            name=f"cost-{cadence}-{pages}-{enabled}",
            cadence_minutes=cadence,
            max_pages=pages,
            enabled=enabled,
        )
        session.flush()
        row = next(r for r in views.query_rows(session) if r.query.cadence_minutes == cadence)
        assert row.projected_daily_calls == expected


class TestSeriesOrdering:
    def test_the_best_sampled_cohort_comes_first(self, session: Session) -> None:
        query = make_query(session, name="ordering")
        scan = make_scan(session, query)
        make_aggregate(session, scan, cohort_key="a|cond=3000", n=3)
        make_aggregate(session, scan, cohort_key="b|cond=3000", n=40)
        session.flush()
        trend = views.query_trend(session, query.id)
        assert trend is not None
        assert [series.cohort_key for series in trend.series] == ["b|cond=3000", "a|cond=3000"]


class TestDealFeedAvailability:
    """The open feed retires deals whose listing left the market, at read time.

    The state is derived from recorded rows only — the listing's last sighting
    and the disappearance series — and written nowhere, which is what makes a
    later sighting revoke it by itself.
    """

    def seed(self, session: Session) -> tuple[Query, Listing, Deal]:
        query = make_query(session, name="avail-q")
        scan = make_scan(session, query)
        listing = make_listing(session, "v1|900|0", RDIMM_TITLE)
        listing.last_seen_at = BASE_TIME + timedelta(hours=1)
        deal = Deal(
            listing_id=listing.item_id,
            scan_id=scan.id,
            cohort_key="gen=DDR4|cap=32|cond=3000",
            total_cost=38.0,
            per_unit=1.1875,
            cohort_p10=1.8,
            cohort_n=14,
            score=1.02,
        )
        session.add(deal)
        session.flush()
        return query, listing, deal

    def disappear(
        self, session: Session, query: Query, listing: Listing, *, at_hours: float
    ) -> None:
        session.add(
            ListingDisappearance(
                listing_item_id=listing.item_id,
                query_id=query.id,
                cohort_key="gen=DDR4|cap=32|cond=3000",
                last_seen_at=listing.last_seen_at,
                detected_at=BASE_TIME + timedelta(hours=at_hours),
                last_price=38.0,
                currency="USD",
            )
        )
        session.flush()

    def test_a_deal_whose_listing_is_still_seen_is_open(self, session: Session) -> None:
        _, _, deal = self.seed(session)
        feed = views.deal_feed(session)
        assert [view.deal.id for view in feed] == [deal.id]
        assert feed[0].availability == "open"

    def test_a_deal_whose_listing_left_the_market_leaves_the_open_feed(
        self, session: Session
    ) -> None:
        query, listing, _ = self.seed(session)
        self.disappear(session, query, listing, at_hours=2)

        assert views.deal_feed(session) == []
        included = views.deal_feed(session, include_dismissed=True)
        assert [view.availability for view in included] == ["left_market"]
        assert included[0].left_market_at == BASE_TIME + timedelta(hours=2)

    def test_a_later_sighting_reopens_the_deal(self, session: Session) -> None:
        query, listing, _ = self.seed(session)
        self.disappear(session, query, listing, at_hours=2)
        listing.last_seen_at = BASE_TIME + timedelta(hours=3)
        session.flush()

        assert [view.availability for view in views.deal_feed(session)] == ["open"]

    def test_a_verified_unavailable_deal_leaves_the_open_feed(
        self, session: Session
    ) -> None:
        _, _, deal = self.seed(session)
        deal.verified_at = BASE_TIME + timedelta(hours=2)
        deal.verify_outcome = VerifyOutcome.UNAVAILABLE
        session.flush()

        assert views.deal_feed(session) == []
        included = views.deal_feed(session, include_dismissed=True)
        assert [view.availability for view in included] == ["verified_unavailable"]

    def test_an_available_verification_outranks_an_older_disappearance(
        self, session: Session
    ) -> None:
        query, listing, deal = self.seed(session)
        self.disappear(session, query, listing, at_hours=2)
        deal.verified_at = BASE_TIME + timedelta(hours=3)
        deal.verify_outcome = VerifyOutcome.AVAILABLE
        session.flush()

        assert [view.availability for view in views.deal_feed(session)] == ["open"]


class TestDealFeedFiltering:
    """The feed trims after retirement and can be narrowed to one query."""

    def flag(
        self, session: Session, query: Query, item_id: str, *, score: float
    ) -> Deal:
        scan = make_scan(session, query)
        listing = make_listing(session, item_id, f"{RDIMM_TITLE} {item_id}")
        listing.last_seen_at = BASE_TIME + timedelta(hours=1)
        deal = Deal(
            listing_id=listing.item_id,
            scan_id=scan.id,
            cohort_key="gen=DDR4|cap=32|cond=3000",
            total_cost=38.0,
            per_unit=1.1875,
            cohort_p10=1.8,
            cohort_n=14,
            score=score,
        )
        session.add(deal)
        session.flush()
        return deal

    def test_retired_flags_do_not_use_up_the_limit(self, session: Session) -> None:
        """Trimming before retirement let higher-scored retired flags fill every
        slot, leaving the open feed empty with open flags sitting below them."""
        query = make_query(session, name="limit-q")
        for item_id, score in (("v1|r1|0", 9.0), ("v1|r2|0", 8.0)):
            retired = self.flag(session, query, item_id, score=score)
            retired.verified_at = BASE_TIME + timedelta(hours=2)
            retired.verify_outcome = VerifyOutcome.UNAVAILABLE
        open_deal = self.flag(session, query, "v1|open|0", score=1.5)
        session.flush()

        feed = views.deal_feed(session, limit=1)
        assert [view.deal.id for view in feed] == [open_deal.id]

    def test_a_query_filter_shows_only_that_querys_flags(self, session: Session) -> None:
        memory = make_query(session, name="memory-q")
        nics = make_query(session, name="nic-q")
        self.flag(session, memory, "v1|m1|0", score=5.0)
        self.flag(session, memory, "v1|m2|0", score=4.0)
        nic = self.flag(session, nics, "v1|n1|0", score=1.1)

        feed = views.deal_feed(session, query_id=nics.id)
        assert [view.deal.id for view in feed] == [nic.id]
        assert feed[0].query is not None and feed[0].query.name == "nic-q"

    def test_facets_count_each_querys_open_flags(self, session: Session) -> None:
        memory = make_query(session, name="memory-q")
        nics = make_query(session, name="nic-q")
        self.flag(session, memory, "v1|m1|0", score=5.0)
        gone = self.flag(session, memory, "v1|m2|0", score=4.0)
        gone.verified_at = BASE_TIME + timedelta(hours=2)
        gone.verify_outcome = VerifyOutcome.UNAVAILABLE
        self.flag(session, nics, "v1|n1|0", score=1.1)
        session.flush()

        facets = views.deal_facets(views.deal_feed(session, limit=None))
        assert [(facet.query.name, facet.count) for facet in facets] == [
            ("memory-q", 1),
            ("nic-q", 1),
        ]
