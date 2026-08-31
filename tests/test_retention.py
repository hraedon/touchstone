"""Retention pruning, and the property that makes it safe.

The whole reason touchstone can delete old listings at all is that per-cohort
statistics were materialized when the scan ran and are never recomputed. So the
central test here is not "did it delete the right rows" — it is "did the history
move", asserted field by field against a snapshot taken before the prune.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from touchstone.db.models import (
    Deal,
    Listing,
    ListingDisappearance,
    ListingObservation,
    Query,
    Scan,
    ScanAggregate,
    ScanStatus,
    Watch,
)
from touchstone.scan import retention

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
OLD = NOW - timedelta(days=400)
RECENT = NOW - timedelta(days=2)
COHORT = "gen=DDR4|ff=RDIMM|ecc=y|reg=y|cap=32|mt=2400|rank=2Rx4|cond=3000"


@pytest.fixture
def seeded(session: Session) -> Query:
    """Two scans a year apart, with aggregates, a disappearance, a watch and a deal."""
    query = Query(name="retention", q="ecc ddr4")
    session.add(query)
    session.flush()

    for index, (moment, item_id) in enumerate(
        [(OLD, "v1|old|0"), (RECENT, "v1|recent|0")]
    ):
        scan = Scan(
            query_id=query.id,
            started_at=moment,
            finished_at=moment,
            status=ScanStatus.COMPLETE,
            result_count=1,
        )
        session.add(scan)
        session.flush()
        listing = Listing(
            item_id=item_id,
            title="32GB 2Rx4 PC4-2400T-R DDR4 ECC REG",
            title_hash=f"hash{index}",
            condition_id="3000",
            first_seen_at=moment,
            last_seen_at=moment,
        )
        session.add(listing)
        session.add(
            ListingObservation(
                listing_id=item_id,
                scan_id=scan.id,
                observed_at=moment,
                price=100.0 + index,
                shipping_cost=5.0,
                total_cost=105.0 + index,
                currency="USD",
            )
        )
        session.add(
            ScanAggregate(
                scan_id=scan.id,
                query_id=query.id,
                observed_at=moment,
                cohort_key=COHORT,
                n=17 + index,
                currency="USD",
                price_min=90.0,
                price_p10=95.0,
                price_p25=100.0,
                price_median=110.0 + index,
                price_mean=112.0,
                per_gb_min=2.8,
                per_gb_p10=2.9,
                per_gb_p25=3.1,
                per_gb_median=3.44,
                per_gb_mean=3.5,
            )
        )

    session.add(
        ListingDisappearance(
            listing_item_id="v1|old|0",
            query_id=query.id,
            cohort_key=COHORT,
            last_seen_at=OLD,
            detected_at=OLD + timedelta(hours=1),
            last_price=100.0,
            last_total_cost=105.0,
            currency="USD",
        )
    )
    session.commit()
    return query


def _snapshot(session: Session) -> list[tuple[object, ...]]:
    return [
        (
            row.scan_id,
            row.cohort_key,
            row.n,
            float(row.price_min),
            float(row.price_p10),
            float(row.price_p25),
            float(row.price_median),
            float(row.price_mean),
            None if row.per_gb_median is None else float(row.per_gb_median),
        )
        for row in session.scalars(select(ScanAggregate).order_by(ScanAggregate.id))
    ]


class TestTheHistoryDoesNotMove:
    def test_pruning_leaves_every_aggregate_byte_identical(
        self, session: Session, seeded: Query
    ) -> None:
        """The load-bearing property. If this fails, pruning rewrites history."""
        before = _snapshot(session)
        assert before, "the fixture must produce aggregates or this test is vacuous"

        retention.prune(session, days=365, dry_run=False, now=NOW)
        session.commit()

        assert _snapshot(session) == before

    def test_pruning_leaves_disappearances_alone(
        self, session: Session, seeded: Query
    ) -> None:
        """They are not foreign-keyed to listing precisely so they survive this."""
        before = session.scalars(select(ListingDisappearance)).all()
        assert len(before) == 1
        retention.prune(session, days=365, dry_run=False, now=NOW)
        session.commit()
        after = session.scalars(select(ListingDisappearance)).all()
        assert len(after) == 1
        assert float(after[0].last_price) == 100.0

    def test_no_scan_row_is_ever_deleted(self, session: Session, seeded: Query) -> None:
        """scan_aggregate.scan_id cascades. Deleting a scan takes the history with it."""
        before = {scan.id for scan in session.scalars(select(Scan))}
        retention.prune(session, days=365, dry_run=False, now=NOW)
        session.commit()
        assert {scan.id for scan in session.scalars(select(Scan))} == before

    def test_the_module_contains_no_write_to_the_scan_table(self) -> None:
        """A grep, because the consequence of getting this wrong is silent."""
        import pathlib

        source = (
            pathlib.Path(retention.__file__).read_text(encoding="utf-8").split('"""', 2)[-1]
        )
        assert "delete(Scan)" not in source
        assert "delete(ScanAggregate)" not in source
        assert "delete(ListingDisappearance)" not in source


class TestWhatGetsRemoved:
    def test_stale_observations_and_their_orphaned_listings_go(
        self, session: Session, seeded: Query
    ) -> None:
        retention.prune(session, days=365, dry_run=False, now=NOW)
        session.commit()

        remaining = session.scalars(select(ListingObservation)).all()
        assert [row.listing_id for row in remaining] == ["v1|recent|0"]
        assert {row.item_id for row in session.scalars(select(Listing))} == {"v1|recent|0"}

    def test_a_dry_run_reports_the_same_plan_and_deletes_nothing(
        self, session: Session, seeded: Query
    ) -> None:
        plan = retention.prune(session, days=365, dry_run=True, now=NOW)
        session.commit()

        assert plan.observations == 1
        assert plan.listings == 1
        assert plan.dry_run is True
        assert "would remove" in plan.summary()
        assert len(session.scalars(select(ListingObservation)).all()) == 2
        assert len(session.scalars(select(Listing)).all()) == 2

    def test_a_horizon_that_covers_everything_removes_nothing(
        self, session: Session, seeded: Query
    ) -> None:
        """Mutation guard: the deletions above must be caused by the horizon."""
        plan = retention.prune(session, days=10_000, dry_run=False, now=NOW)
        session.commit()
        assert plan.observations == 0
        assert len(session.scalars(select(ListingObservation)).all()) == 2

    def test_a_reckless_horizon_is_refused(self, session: Session) -> None:
        with pytest.raises(ValueError, match="at least"):
            retention.prune(session, days=3, dry_run=False, now=NOW)


class TestProtectedListings:
    def test_a_watched_listing_keeps_its_row_and_its_whole_history(
        self, session: Session, seeded: Query
    ) -> None:
        """A pinned listing with a blank chart is worse than the disk it saves."""
        session.add(Watch(listing_id="v1|old|0", note="for the R730"))
        session.commit()

        retention.prune(session, days=365, dry_run=False, now=NOW)
        session.commit()

        assert session.get(Listing, "v1|old|0") is not None
        observations = session.scalars(
            select(ListingObservation).where(ListingObservation.listing_id == "v1|old|0")
        ).all()
        assert len(observations) == 1, "the watchlist draws this history"
        assert session.scalars(select(Watch)).first() is not None

    def test_an_undismissed_deal_protects_its_listing(
        self, session: Session, seeded: Query
    ) -> None:
        scan = session.scalars(select(Scan).order_by(Scan.started_at)).first()
        assert scan is not None
        session.add(
            Deal(
                listing_id="v1|old|0",
                scan_id=scan.id,
                cohort_key=COHORT,
                total_cost=105.0,
                per_gb=3.28,
                cohort_p10=3.5,
                cohort_n=17,
                score=1.4,
            )
        )
        session.commit()

        retention.prune(session, days=365, dry_run=False, now=NOW)
        session.commit()

        assert session.get(Listing, "v1|old|0") is not None
        assert session.scalars(select(Deal)).first() is not None

    def test_a_dismissed_deal_does_not_protect_it(
        self, session: Session, seeded: Query
    ) -> None:
        """Mutation guard on the protection: it must be the *undismissed* predicate."""
        scan = session.scalars(select(Scan).order_by(Scan.started_at)).first()
        assert scan is not None
        session.add(
            Deal(
                listing_id="v1|old|0",
                scan_id=scan.id,
                cohort_key=COHORT,
                total_cost=105.0,
                per_gb=3.28,
                cohort_p10=3.5,
                cohort_n=17,
                score=1.4,
                dismissed_at=NOW,
            )
        )
        session.commit()

        retention.prune(session, days=365, dry_run=False, now=NOW)
        session.commit()

        assert session.get(Listing, "v1|old|0") is None


def test_the_cascade_this_module_is_avoiding_is_real(session: Session) -> None:
    """Guards the guard.

    Everything above rests on scan_aggregate cascading from scan. If that foreign key
    ever loses ON DELETE CASCADE, the reason this module never touches `scan` stops
    being true and the comments become folklore.
    """
    [fk] = [
        fk
        for fk in inspect(session.get_bind()).get_foreign_keys("scan_aggregate")
        if fk["referred_table"] == "scan"
    ]
    assert fk["options"].get("ondelete") == "CASCADE"


class TestConcurrentChangesDuringAPrune:
    """The hazard a code review named: prune runs weekly, the scanner every fifteen
    minutes, so a prune routinely overlaps several scans.

    An earlier version read the protected ids and the orphan ids into Python before
    deleting. Anything that became protected, or was re-observed, in the window
    between reading and deleting was still deleted — taking a brand-new observation
    or an unexamined flag with it through the cascade.

    The window is *inside* one ``prune()`` call, so these tests inject the change
    there rather than between two calls: patching ``PrunePlan`` gives a deterministic
    hook at exactly the moment the plan is finalised and before any DELETE runs. A
    test that changed the database between two separate calls would pass against the
    broken code, which is how this hazard would have survived review.
    """

    @staticmethod
    def _inject(monkeypatch: pytest.MonkeyPatch, action: object) -> None:
        real = retention.PrunePlan
        fired = {"done": False}

        def hook(**kwargs: object) -> object:
            if not fired["done"]:
                fired["done"] = True
                action()  # type: ignore[operator]
            return real(**kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(retention, "PrunePlan", hook)

    def test_a_listing_re_observed_mid_prune_is_not_deleted(
        self, session: Session, seeded: Query, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert retention.prune(session, days=365, dry_run=True, now=NOW).listings == 1, (
            "the fixture must offer something to delete, or this test proves nothing"
        )

        def a_scanner_pass_lands() -> None:
            scan = session.scalars(select(Scan).order_by(Scan.started_at.desc())).first()
            assert scan is not None
            session.add(
                ListingObservation(
                    listing_id="v1|old|0",
                    scan_id=scan.id,
                    observed_at=NOW,
                    price=95.0,
                    shipping_cost=5.0,
                    total_cost=100.0,
                    currency="USD",
                )
            )
            session.flush()

        self._inject(monkeypatch, a_scanner_pass_lands)
        retention.prune(session, days=365, dry_run=False, now=NOW)
        session.commit()

        assert session.get(Listing, "v1|old|0") is not None, (
            "a listing re-observed during the prune must survive it"
        )
        fresh = session.scalars(
            select(ListingObservation).where(ListingObservation.observed_at == NOW)
        ).all()
        assert len(fresh) == 1, "and the new observation must survive with it"

    def test_a_listing_pinned_mid_prune_is_not_deleted(
        self, session: Session, seeded: Query, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def an_operator_pins_it() -> None:
            session.add(Watch(listing_id="v1|old|0"))
            session.flush()

        self._inject(monkeypatch, an_operator_pins_it)
        retention.prune(session, days=365, dry_run=False, now=NOW)
        session.commit()

        assert session.get(Listing, "v1|old|0") is not None
        assert session.scalars(select(Watch)).first() is not None, (
            "the cascade must not erase the pin that was just created"
        )

    def test_a_deal_flagged_mid_prune_protects_its_listing(
        self, session: Session, seeded: Query, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def a_scan_flags_it() -> None:
            scan = session.scalars(select(Scan).order_by(Scan.started_at)).first()
            assert scan is not None
            session.add(
                Deal(
                    listing_id="v1|old|0",
                    scan_id=scan.id,
                    cohort_key=COHORT,
                    total_cost=105.0,
                    per_gb=3.28,
                    cohort_p10=3.5,
                    cohort_n=17,
                    score=1.4,
                )
            )
            session.flush()

        self._inject(monkeypatch, a_scan_flags_it)
        retention.prune(session, days=365, dry_run=False, now=NOW)
        session.commit()

        assert session.get(Listing, "v1|old|0") is not None
        assert session.scalars(select(Deal)).first() is not None
