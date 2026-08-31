"""The web face as a working front end: queries, deals, corrections, watchlist."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from tests.conftest import WebHarness
from tests.factories import (
    make_aggregate,
    make_listing,
    make_observation,
    make_query,
    make_scan,
)
from touchstone.db.models import Deal, ExtractionMethod, ItemSpec, Query, Watch
from touchstone.extract.normalize import title_hash

RDIMM = "32GB 2Rx4 PC4-2400T-R DDR4 ECC REG RDIMM Server Memory"


class TestQueries:
    def test_a_query_can_be_created_and_edited(self, harness: WebHarness) -> None:
        created = harness.client.post(
            "/queries",
            data={
                "name": "ddr4-32",
                "q": "32gb ddr4 ecc rdimm",
                "cadence_minutes": "60",
                "max_pages": "4",
                "min_seller_feedback": "1",
                "enabled": "on",
            },
            follow_redirects=False,
        )
        assert created.status_code == 303

        query = harness.session.scalars(select(Query).where(Query.name == "ddr4-32")).one()
        assert query.max_pages == 4

        harness.client.post(
            f"/queries/{query.id}",
            data={
                "name": "ddr4-32",
                "q": "32gb ddr4 ecc rdimm",
                "cadence_minutes": "120",
                "max_pages": "6",
                "min_seller_feedback": "1",
                "enabled": "on",
            },
            follow_redirects=False,
        )
        harness.session.expire_all()
        assert harness.session.get(Query, query.id).cadence_minutes == 120  # type: ignore[union-attr]

    def test_an_unaffordable_query_is_refused_rather_than_warned_about(
        self, harness: WebHarness
    ) -> None:
        response = harness.client.post(
            "/queries",
            data={
                "name": "greedy",
                "q": "ram",
                "cadence_minutes": "5",
                "max_pages": "500",
                "min_seller_feedback": "1",
            },
        )
        assert response.status_code == 400
        assert "Pages per scan must be between" in response.text
        assert harness.session.scalars(select(Query).where(Query.name == "greedy")).first() is None

    def test_the_projected_daily_cost_is_shown_before_saving(self, harness: WebHarness) -> None:
        session = harness.session
        make_query(session, name="hourly", cadence_minutes=60, max_pages=5)
        session.commit()
        body = harness.client.get("/queries").text
        # 24 scans a day x 5 pages = 120 calls, worst case.
        assert "120" in body
        assert "Projected calls/day" in body

    def test_scan_now_records_a_request_and_calls_nothing(self, harness: WebHarness) -> None:
        session = harness.session
        query = make_query(session, name="on-demand")
        session.commit()

        harness.client.post(f"/queries/{query.id}/scan-request", follow_redirects=False)
        session.expire_all()
        refreshed = session.get(Query, query.id)
        assert refreshed is not None
        assert refreshed.scan_requested_at is not None
        # No scan was run: the scanner owns that, on its own schedule.
        assert refreshed.last_scanned_at is None

        harness.client.post(f"/queries/{query.id}/scan-request/cancel", follow_redirects=False)
        session.expire_all()
        assert session.get(Query, query.id).scan_requested_at is None  # type: ignore[union-attr]

    def test_toggling_enabled(self, harness: WebHarness) -> None:
        session = harness.session
        query = make_query(session, name="toggle-me", enabled=True)
        session.commit()
        harness.client.post(f"/queries/{query.id}/enabled", follow_redirects=False)
        session.expire_all()
        assert session.get(Query, query.id).enabled is False  # type: ignore[union-attr]

    def test_changing_the_feedback_floor_says_so_out_loud(self, harness: WebHarness) -> None:
        session = harness.session
        query = make_query(session, name="floor-warn", min_seller_feedback=1)
        session.commit()
        follow = harness.client.post(
            f"/queries/{query.id}",
            data={
                "name": "floor-warn",
                "q": "ddr4 ecc rdimm",
                "cadence_minutes": "60",
                "max_pages": "5",
                "min_seller_feedback": "25",
                "enabled": "on",
            },
        )
        assert "different set of sellers" in follow.text

    def test_a_duplicate_name_is_reported_not_crashed(self, harness: WebHarness) -> None:
        session = harness.session
        make_query(session, name="taken")
        session.commit()
        response = harness.client.post(
            "/queries",
            data={
                "name": "taken",
                "q": "ram",
                "cadence_minutes": "60",
                "max_pages": "5",
                "min_seller_feedback": "1",
            },
        )
        assert response.status_code == 400
        assert "already exists" in response.text


class TestDeals:
    def _seed_deal(self, harness: WebHarness) -> Deal:
        session = harness.session
        query = make_query(session, name="deals-q")
        scan = make_scan(session, query)
        cohort = "gen=DDR4|ff=RDIMM|ecc=y|reg=y|cap=32|mt=2400|rank=2Rx4|cond=3000"
        make_aggregate(session, scan, cohort_key=cohort, n=14, per_gb_median=2.4, per_gb_p10=1.8)
        listing = make_listing(session, "v1|555|0", RDIMM)
        session.add(
            ItemSpec(
                title_hash=listing.title_hash,
                normalized_title=RDIMM.lower(),
                capacity_per_module_gb=32,
                module_count=1,
                total_gb=32,
                method=ExtractionMethod.REGEX,
                confidence=0.9,
            )
        )
        deal = Deal(
            listing_id=listing.item_id,
            scan_id=scan.id,
            cohort_key=cohort,
            total_cost=38.0,
            per_gb=1.1875,
            cohort_p10=1.8,
            cohort_n=14,
            score=1.02,
        )
        session.add(deal)
        session.commit()
        return deal

    def test_the_feed_shows_cohort_context_and_the_parse_behind_it(
        self, harness: WebHarness
    ) -> None:
        self._seed_deal(harness)
        body = harness.client.get("/deals").text
        assert "ts-band" in body, "the cohort price band is the signature element"
        assert "Capacity parse" in body
        assert "pattern" in body
        assert "1.188" in body
        assert "spread-unit" in body

    def test_dismiss_and_restore(self, harness: WebHarness) -> None:
        deal = self._seed_deal(harness)
        harness.client.post(f"/deals/{deal.id}/dismiss", follow_redirects=False)
        harness.session.expire_all()
        assert harness.session.get(Deal, deal.id).dismissed_at is not None  # type: ignore[union-attr]
        assert "ts-band" not in harness.client.get("/deals").text

        harness.client.post(f"/deals/{deal.id}/restore", follow_redirects=False)
        harness.session.expire_all()
        assert harness.session.get(Deal, deal.id).dismissed_at is None  # type: ignore[union-attr]


class TestSpecCorrection:
    def test_a_title_that_was_never_extracted_can_still_be_corrected(
        self, harness: WebHarness
    ) -> None:
        session = harness.session
        listing = make_listing(session, "v1|900|0", RDIMM)
        session.commit()

        assert "Correct" in harness.client.get("/specs").text

        response = harness.client.post(
            f"/specs/{listing.title_hash}",
            data={
                "capacity_per_module_gb": "32",
                "module_count": "4",
                "total_gb": "128",
                "ddr_gen": "DDR4",
                "speed_mt": "2400",
                "ecc": "yes",
                "registered": "yes",
                "corrected_by": "operator",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303

        session.expire_all()
        spec = session.get(ItemSpec, listing.title_hash)
        assert spec is not None
        assert spec.total_gb == 128
        assert spec.method is ExtractionMethod.MANUAL
        assert float(spec.confidence or 0) == 1.0
        assert spec.corrected_by == "operator"

    def test_an_implausible_correction_is_refused_and_stored_nowhere(
        self, harness: WebHarness
    ) -> None:
        session = harness.session
        listing = make_listing(session, "v1|901|0", RDIMM)
        session.commit()

        response = harness.client.post(
            f"/specs/{listing.title_hash}",
            data={
                "capacity_per_module_gb": "32",
                "module_count": "4",
                "total_gb": "999",
                "corrected_by": "operator",
            },
        )
        assert response.status_code == 400
        assert "range check" in response.text
        session.expire_all()
        assert session.get(ItemSpec, listing.title_hash) is None

    def test_a_correction_must_be_attributed(self, harness: WebHarness) -> None:
        session = harness.session
        listing = make_listing(session, "v1|902|0", RDIMM)
        session.commit()
        response = harness.client.post(
            f"/specs/{listing.title_hash}",
            data={"total_gb": "32", "corrected_by": "  "},
        )
        assert response.status_code == 400
        assert session.get(ItemSpec, listing.title_hash) is None

    def test_the_worklist_puts_the_most_shared_title_first(self, harness: WebHarness) -> None:
        session = harness.session
        common = "16GB DDR4 SERVER RAM MIXED BRAND"
        rare = "8GB SOMETHING UNUSUAL"
        for index in range(5):
            make_listing(session, f"v1|c{index}|0", common)
        make_listing(session, "v1|r0|0", rare)
        session.commit()

        body = harness.client.get("/specs").text
        assert body.index(common) < body.index(rare)


class TestWatchlist:
    def test_pin_view_and_unpin(self, harness: WebHarness) -> None:
        session = harness.session
        query = make_query(session, name="watch-q")
        scan = make_scan(session, query)
        listing = make_listing(session, "v1|777|0", RDIMM)
        make_observation(session, listing, scan, price=44.0, shipping_cost=6.0)
        session.commit()

        harness.client.post(
            "/watch", data={"listing_id": listing.item_id, "note": "for the R730"},
            follow_redirects=False,
        )
        session.expire_all()
        assert session.scalars(select(Watch)).first() is not None

        detail = harness.client.get(f"/watch/{listing.item_id}")
        assert detail.status_code == 200
        assert "44.00" in detail.text
        assert "50.00" in detail.text

        harness.client.post(f"/watch/{listing.item_id}/unpin", follow_redirects=False)
        session.expire_all()
        assert session.scalars(select(Watch)).first() is None

    def test_unknown_shipping_is_never_rendered_as_free(self, harness: WebHarness) -> None:
        session = harness.session
        query = make_query(session, name="freight")
        scan = make_scan(session, query)
        listing = make_listing(session, "v1|778|0", RDIMM)
        make_observation(session, listing, scan, price=120.0, shipping_cost=None)
        session.add(Watch(listing_id=listing.item_id))
        session.commit()

        body = harness.client.get(f"/watch/{listing.item_id}").text
        # Both the shipping cell and the delivered cell must say so; a zero in
        # either would understate the delivered price and every $/GB from it.
        assert body.count("unknown") >= 2
        assert ">0.00<" not in body
        assert "120.00" in body

    def test_pinning_an_unobserved_listing_is_refused(self, harness: WebHarness) -> None:
        harness.client.post("/watch", data={"listing_id": "v1|nope|0"}, follow_redirects=False)
        assert harness.session.scalars(select(Watch)).first() is None


class TestMisc:
    def test_the_dashboard_renders_with_no_data_at_all(self, harness: WebHarness) -> None:
        response = harness.client.get("/")
        assert response.status_code == 200
        assert "asking price" in response.text

    def test_an_unknown_query_is_a_404(self, harness: WebHarness) -> None:
        assert harness.client.get("/queries/99999/trend").status_code == 404

    def test_an_out_of_range_window_is_refused(self, harness: WebHarness) -> None:
        session = harness.session
        query = make_query(session, name="window")
        session.commit()
        response = harness.client.get(f"/queries/{query.id}/trend?days=4000")
        assert response.status_code == 400

    def test_security_headers_and_a_strict_csp(self, harness: WebHarness) -> None:
        headers = harness.client.get("/").headers
        assert headers["X-Frame-Options"] == "DENY"
        assert headers["X-Content-Type-Options"] == "nosniff"
        csp = headers["Content-Security-Policy"]
        # No CDN anywhere in the family; the theme toggle is an external self script.
        assert "script-src 'self'" in csp
        assert "unsafe-inline" not in csp

    def test_the_title_hash_helper_matches_the_extractor(self) -> None:
        """Guards the factories: a divergent hash would make the spec tests vacuous."""
        assert title_hash(RDIMM) == title_hash(RDIMM.upper())


class TestDegenerateBand:
    def test_a_spreadless_cohort_says_so_rather_than_drawing_a_hairline(
        self, harness: WebHarness
    ) -> None:
        """Found on live data: most cohorts of identical modules have p10 == median."""
        session = harness.session
        query = make_query(session, name="no-spread")
        scan = make_scan(session, query)
        cohort = "gen=DDR4|ff=RDIMM|ecc=y|reg=y|cap=32|mt=2666|rank=?|cond=3000"
        make_aggregate(session, scan, cohort_key=cohort, n=40, per_gb_median=5.3, per_gb_p10=5.3)
        listing = make_listing(session, "v1|4040|0", RDIMM)
        session.add(
            ItemSpec(
                title_hash=listing.title_hash,
                normalized_title=RDIMM.lower(),
                total_gb=32,
                method=ExtractionMethod.REGEX,
                confidence=0.9,
            )
        )
        session.add(
            Deal(
                listing_id=listing.item_id,
                scan_id=scan.id,
                cohort_key=cohort,
                total_cost=142.0,
                per_gb=4.44,
                cohort_p10=5.3,
                cohort_n=40,
                score=3.27,
            )
        )
        session.commit()

        body = harness.client.get("/deals").text
        assert "no spread" in body
        assert "fallback of 5% of the price level" in body


class TestHealth:
    def test_liveness_answers_without_touching_the_database(
        self, harness: WebHarness
    ) -> None:
        response = harness.client.get("/livez")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_readiness_reports_the_database(self, harness: WebHarness) -> None:
        response = harness.client.get("/readyz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "database": "ok"}

    def test_readiness_turns_503_when_the_database_will_not_answer(
        self, harness: WebHarness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pod that cannot reach Postgres should leave the load balancer.

        Injected rather than simulated: the check must fail on a real exception from
        the session, which is the only shape this failure actually takes.
        """
        from touchstone.web.routes import health

        def refuse(self: object, *args: object, **kwargs: object) -> None:
            raise OperationalError("SELECT 1", {}, Exception("connection refused"))

        monkeypatch.setattr(Session, "execute", refuse)
        response = harness.client.get("/readyz")
        assert response.status_code == 503
        assert response.json()["database"] == "unreachable"
        assert health.router is not None

    def test_liveness_survives_a_dead_database(
        self, harness: WebHarness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The point of splitting them: a Postgres blip must not restart the pod."""

        def refuse(self: object, *args: object, **kwargs: object) -> None:
            raise OperationalError("SELECT 1", {}, Exception("connection refused"))

        monkeypatch.setattr(Session, "execute", refuse)
        assert harness.client.get("/livez").status_code == 200
