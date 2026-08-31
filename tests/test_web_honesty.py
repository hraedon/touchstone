"""The honesty constraints, asserted rather than reviewed.

Plan 003's deliverable is not "a UI" — it is a UI that cannot casually undo a careful
measurement model with a chart title. So the constraints are tests, and they run
against rendered HTML, which is the only place a label actually reaches a reader.
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy import delete

from tests.conftest import WebHarness
from tests.factories import (
    make_aggregate,
    make_listing,
    make_observation,
    make_query,
    make_scan,
)
from touchstone.db.models import Listing, ListingObservation, ScanStatus

# Phrases that would turn an asking-price index into a claim touchstone cannot make.
# "worth" and "value" are here because they are how the claim gets made accidentally:
# nobody writes "market price" on purpose, they write "what it's worth".
FORBIDDEN = (
    "market price",
    "market value",
    "sold for",
    "sold price",
    "worth",
    "fair value",
    "true value",
    "actual price paid",
)


def _text(html: str) -> str:
    """Rendered text, lowercased, with tags removed so an attribute cannot hide a claim."""
    return re.sub(r"<[^>]+>", " ", html).lower()


def _seed_trend(harness: WebHarness) -> int:
    session = harness.session
    query = make_query(session)
    for hour in range(4):
        scan = make_scan(session, query, offset_hours=hour * 24)
        make_aggregate(session, scan, n=12, price_median=60.0 + hour, per_gb_median=1.9)
    session.commit()
    return query.id


class TestForbiddenLabels:
    def test_no_page_claims_to_know_a_market_price(self, harness: WebHarness) -> None:
        query_id = _seed_trend(harness)
        pages = [
            "/",
            "/queries",
            "/queries/new",
            f"/queries/{query_id}/edit",
            f"/queries/{query_id}/trend",
            f"/queries/{query_id}/trend?metric=price",
            "/deals",
            "/specs",
            "/watch",
        ]
        offences: list[str] = []
        for path in pages:
            response = harness.client.get(path)
            assert response.status_code == 200, path
            body = _text(response.text)
            offences.extend(
                f"{path} contains {phrase!r}" for phrase in FORBIDDEN if phrase in body
            )
        assert offences == [], "\n".join(offences)

    def test_the_guard_can_actually_fail(self) -> None:
        """Guards the guard.

        A grep that never matches proves nothing about the pages; this shows the
        matcher itself works on text shaped like the templates.
        """
        assert any(
            phrase in _text("<p>the <b>market price</b> is $40</p>") for phrase in FORBIDDEN
        )

    def test_the_trend_page_says_asking(self, harness: WebHarness) -> None:
        query_id = _seed_trend(harness)
        body = _text(harness.client.get(f"/queries/{query_id}/trend").text)
        assert "asking" in body
        assert "not transaction prices" in body


class TestSuppression:
    @pytest.mark.parametrize(("n", "suppressed"), [(1, True), (4, True), (5, False), (12, False)])
    def test_thin_cohorts_are_suppressed_at_the_documented_boundary(
        self, harness: WebHarness, n: int, suppressed: bool
    ) -> None:
        session = harness.session
        query = make_query(session, name=f"thin-{n}")
        scan = make_scan(session, query)
        make_aggregate(session, scan, n=n, price_median=61.0, per_gb_median=1.91)
        session.commit()

        body = harness.client.get(f"/queries/{query.id}/trend").text
        assert f"n={n}" in _text(body) or str(n) in body
        if suppressed:
            assert "withheld" in body
            # The number itself must not reach the page.
            assert "61.00" not in body
            assert "1.910" not in body
        else:
            assert "61.00" in body

    def test_the_count_survives_suppression(self, harness: WebHarness) -> None:
        """The count is a fact even when the statistics are not reportable."""
        session = harness.session
        query = make_query(session, name="count-kept")
        scan = make_scan(session, query)
        make_aggregate(session, scan, n=2, price_median=99.0)
        session.commit()
        body = harness.client.get(f"/queries/{query.id}/trend").text
        assert "withheld" in body
        assert ">2<" in body or "n=2" in _text(body)


class TestDiscontinuities:
    def test_a_feedback_floor_change_is_drawn(self, harness: WebHarness) -> None:
        session = harness.session
        query = make_query(session, name="floor-change")
        for hour, floor in ((0, 1), (24, 1), (48, 50), (72, 50)):
            scan = make_scan(session, query, offset_hours=hour, min_seller_feedback=floor)
            make_aggregate(session, scan, n=11)
        session.commit()

        body = harness.client.get(f"/queries/{query.id}/trend").text
        assert "Seller-feedback floor changed" in body
        assert "floor 1 to 50" in body
        assert "class=\"rule\"" in body

    def test_an_unchanged_floor_draws_nothing(self, harness: WebHarness) -> None:
        """Mutation guard: the marker must be caused by the change, not by the page."""
        session = harness.session
        query = make_query(session, name="floor-stable")
        for hour in (0, 24, 48):
            scan = make_scan(session, query, offset_hours=hour, min_seller_feedback=1)
            make_aggregate(session, scan, n=11)
        session.commit()

        body = harness.client.get(f"/queries/{query.id}/trend").text
        assert "Seller-feedback floor changed" not in body

    def test_a_failed_scan_does_not_manufacture_a_break(self, harness: WebHarness) -> None:
        session = harness.session
        query = make_query(session, name="floor-failed-scan")
        make_scan(session, query, offset_hours=0, min_seller_feedback=1)
        make_scan(
            session,
            query,
            offset_hours=12,
            min_seller_feedback=0,
            status=ScanStatus.FAILED,
            result_count=0,
        )
        make_scan(session, query, offset_hours=24, min_seller_feedback=1)
        session.commit()

        body = harness.client.get(f"/queries/{query.id}/trend").text
        assert "Seller-feedback floor changed" not in body

    def test_the_cohort_key_generation_change_is_drawn(self, harness: WebHarness) -> None:
        session = harness.session
        query = make_query(session, name="key-change")
        old = make_scan(session, query, offset_hours=0)
        make_aggregate(session, old, cohort_key=f"q={query.id}|cond=3000", n=20)
        new = make_scan(session, query, offset_hours=48)
        make_aggregate(session, new, n=20)
        session.commit()

        body = harness.client.get(f"/queries/{query.id}/trend").text
        assert "Cohort definition changed" in body
        assert "pre-capacity cohort" in body

    def test_current_keys_alone_draw_no_generation_break(self, harness: WebHarness) -> None:
        session = harness.session
        query = make_query(session, name="key-stable")
        for hour in (0, 48):
            scan = make_scan(session, query, offset_hours=hour)
            make_aggregate(session, scan, n=20)
        session.commit()

        body = harness.client.get(f"/queries/{query.id}/trend").text
        assert "Cohort definition changed" not in body


class TestCappedScans:
    def test_a_capped_scan_is_marked(self, harness: WebHarness) -> None:
        session = harness.session
        query = make_query(session, name="capped")
        scan = make_scan(session, query, capped=True)
        make_aggregate(session, scan, n=30)
        session.commit()

        body = harness.client.get(f"/queries/{query.id}/trend").text
        assert "capped" in body
        assert "moving window" in body
        assert "dot--flagged" in body

    def test_an_uncapped_scan_is_not_marked(self, harness: WebHarness) -> None:
        session = harness.session
        query = make_query(session, name="uncapped")
        scan = make_scan(session, query, capped=False)
        make_aggregate(session, scan, n=30)
        session.commit()

        body = harness.client.get(f"/queries/{query.id}/trend").text
        assert "dot--flagged" not in body


class TestDisappearanceSeparation:
    def test_the_disappearance_panel_is_separate_and_never_called_sold(
        self, harness: WebHarness
    ) -> None:
        query_id = _seed_trend(harness)
        body = harness.client.get(f"/queries/{query_id}/trend").text
        assert "ts-panel--inference" in body
        assert "an inference, not a sales record" in body
        # It may say a listing *may have* sold; it may never report a sale.
        assert "sold for" not in body.lower()
        assert "sold price" not in body.lower()


class TestAggregatesAreNeverRecomputed:
    def test_the_trend_survives_the_listings_being_deleted(self, harness: WebHarness) -> None:
        """The load-bearing property, checked from the outside.

        If any part of the web layer derived a statistic from ``listing`` rows, this
        chart would change when they were pruned — silently rewriting history. It
        must not move by a digit.
        """
        session = harness.session
        query = make_query(session, name="prune-proof")
        scan = make_scan(session, query)
        make_aggregate(session, scan, n=30, price_median=77.5, per_gb_median=2.25)
        listing = make_listing(session, "v1|1|0", "32GB PC4-2400T-R DDR4 ECC RDIMM")
        make_observation(session, listing, scan)
        session.commit()

        before = harness.client.get(f"/queries/{query.id}/trend").text

        session.execute(delete(ListingObservation))
        session.execute(delete(Listing))
        session.commit()

        after = harness.client.get(f"/queries/{query.id}/trend").text
        assert "77.50" in before
        assert "77.50" in after
        assert "2.250" in after
