"""The scheduler: what is due, in what order, and when to stop.

``run_scan`` is already covered. What is tested here is the selection and the
stopping, because both fail quietly — a starved query just looks like a market that
went still, and an unbounded pass just looks like a busy day until the allowance is
gone.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.fake_ebay import FakeEbay, Generation, item
from touchstone.db.models import Query, RateBudget, Scan, ScanStatus
from touchstone.ebay.budget import today_utc
from touchstone.ebay.client import Credentials, EbayClient
from touchstone.scan import schedule

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


def make_query(
    session: Session,
    name: str,
    *,
    cadence_minutes: int = 60,
    last_scanned_at: datetime | None = None,
    scan_requested_at: datetime | None = None,
    enabled: bool = True,
    max_pages: int = 1,
) -> Query:
    query = Query(
        name=name,
        q="ecc ddr4",
        cadence_minutes=cadence_minutes,
        last_scanned_at=last_scanned_at,
        scan_requested_at=scan_requested_at,
        enabled=enabled,
        max_pages=max_pages,
    )
    session.add(query)
    # Committed, not just flushed: the scheduler rolls back on a failing query, and
    # a merely-flushed fixture would vanish with it — which would test the fixture,
    # not the scheduler.
    session.commit()
    return query


class TestDueSelection:
    def test_a_query_inside_its_cadence_is_not_due(self, session: Session) -> None:
        make_query(session, "fresh", cadence_minutes=60,
                   last_scanned_at=NOW - timedelta(minutes=30))
        assert schedule.due_queries(session, now=NOW) == []

    def test_a_query_past_its_cadence_is_due(self, session: Session) -> None:
        query = make_query(session, "stale", cadence_minutes=60,
                           last_scanned_at=NOW - timedelta(minutes=61))
        assert [q.id for q in schedule.due_queries(session, now=NOW)] == [query.id]

    def test_exactly_at_cadence_is_due(self, session: Session) -> None:
        """The boundary matters: a strict > would drift the cadence by a whole pass."""
        query = make_query(session, "exact", cadence_minutes=60,
                           last_scanned_at=NOW - timedelta(minutes=60))
        assert [q.id for q in schedule.due_queries(session, now=NOW)] == [query.id]

    def test_a_never_scanned_query_is_due_immediately(self, session: Session) -> None:
        query = make_query(session, "new", cadence_minutes=1440, last_scanned_at=None)
        assert [q.id for q in schedule.due_queries(session, now=NOW)] == [query.id]

    def test_a_disabled_query_is_never_due(self, session: Session) -> None:
        make_query(session, "off", enabled=False, last_scanned_at=None)
        assert schedule.due_queries(session, now=NOW) == []

    def test_a_disabled_query_is_not_rescued_by_a_scan_request(
        self, session: Session
    ) -> None:
        """Disabling is the operator's off switch and outranks a stale request."""
        make_query(session, "off-but-asked", enabled=False, scan_requested_at=NOW)
        assert schedule.due_queries(session, now=NOW) == []

    def test_an_explicit_request_makes_a_fresh_query_due(self, session: Session) -> None:
        query = make_query(session, "asked", cadence_minutes=1440,
                           last_scanned_at=NOW - timedelta(minutes=1),
                           scan_requested_at=NOW)
        assert [q.id for q in schedule.due_queries(session, now=NOW)] == [query.id]

    def test_requests_come_first_then_most_overdue(self, session: Session) -> None:
        slightly = make_query(session, "slightly-late", cadence_minutes=60,
                              last_scanned_at=NOW - timedelta(minutes=70))
        very = make_query(session, "very-late", cadence_minutes=60,
                          last_scanned_at=NOW - timedelta(hours=9))
        asked = make_query(session, "asked", cadence_minutes=60,
                           last_scanned_at=NOW - timedelta(minutes=1),
                           scan_requested_at=NOW)
        order = [q.id for q in schedule.due_queries(session, now=NOW)]
        assert order == [asked.id, very.id, slightly.id]

    def test_a_naive_timestamp_does_not_crash_the_comparison(
        self, session: Session
    ) -> None:
        """Guards a real trap: Postgres returns aware datetimes, fixtures may not."""
        query = make_query(session, "naive", cadence_minutes=60)
        query.last_scanned_at = datetime(2026, 8, 30, 12, 0)
        session.flush()
        assert [q.id for q in schedule.due_queries(session, now=NOW)] == [query.id]


class TestTick:
    def _fake(self, remaining: int = 5000) -> FakeEbay:
        return FakeEbay(
            generations=[
                Generation(items=[item("v1|1|0", price=100.0), item("v1|2|0", price=110.0)])
            ],
            rate_limit_remaining=remaining,
        )

    def _tick(self, session: Session, url: str, **kwargs: object) -> schedule.TickResult:
        with EbayClient(credentials=Credentials("id", "secret"), base_url=url) as client:
            return schedule.run_tick(session, client, now=NOW, **kwargs)  # type: ignore[arg-type]

    def test_only_due_queries_are_scanned(self, session: Session) -> None:
        due = make_query(session, "due", last_scanned_at=None)
        make_query(session, "not-due", last_scanned_at=NOW - timedelta(minutes=1))
        fake = self._fake()
        url = fake.start()
        try:
            result = self._tick(session, url)
        finally:
            fake.stop()

        assert result.considered == 1
        assert result.scanned == 1
        scans = session.scalars(select(Scan)).all()
        assert [scan.query_id for scan in scans] == [due.id]

    def test_a_scan_clears_the_request_and_stamps_the_query(self, session: Session) -> None:
        query = make_query(session, "asked", scan_requested_at=NOW, last_scanned_at=None)
        fake = self._fake()
        url = fake.start()
        try:
            self._tick(session, url)
        finally:
            fake.stop()

        session.refresh(query)
        assert query.scan_requested_at is None
        assert query.last_scanned_at is not None

    def test_an_exhausted_allowance_stops_the_pass_without_a_refusal_per_query(
        self, session: Session
    ) -> None:
        """Forty identical SKIPPED_BUDGET rows would bury the one that matters."""
        for index in range(4):
            make_query(session, f"q{index}", last_scanned_at=None)
        session.add(
            RateBudget(day=today_utc(), calls_used=5000, calls_limit=5000)
        )
        session.flush()

        fake = self._fake(remaining=0)
        url = fake.start()
        try:
            result = self._tick(session, url)
        finally:
            fake.stop()

        assert result.considered == 4
        assert result.scanned == 0
        assert result.stopped_on_budget is True
        assert session.scalars(select(Scan)).all() == []

    def test_the_limit_caps_one_pass(self, session: Session) -> None:
        for index in range(3):
            make_query(session, f"q{index}", last_scanned_at=None)
        fake = self._fake()
        url = fake.start()
        try:
            result = self._tick(session, url, limit=2)
        finally:
            fake.stop()

        assert result.considered == 3
        assert result.scanned == 2

    def test_a_failing_query_does_not_stop_the_others(
        self, session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One bad query must cost one scan, not the whole pass.

        The failure is injected rather than simulated: an exception escaping
        ``run_scan`` is the real shape of the hazard, and anything gentler would
        pass against a scheduler that had no error handling at all.
        """
        make_query(session, "aaa-broken", last_scanned_at=None)
        make_query(session, "bbb-working", last_scanned_at=None)

        real = schedule.run_scan
        calls: list[str] = []

        def flaky(session_: Session, client: object, query: Query, **kwargs: object):  # type: ignore[no-untyped-def]
            calls.append(query.name)
            if query.name == "aaa-broken":
                raise RuntimeError("eBay returned something impossible")
            return real(session_, client, query, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(schedule, "run_scan", flaky)

        fake = self._fake()
        url = fake.start()
        try:
            result = self._tick(session, url)
        finally:
            fake.stop()

        assert calls == ["aaa-broken", "bbb-working"], "the pass must reach the second query"
        assert result.failed == 1
        assert result.scanned == 1
        # The surviving scan committed; the failed one left no half-written row.
        statuses = [scan.status for scan in session.scalars(select(Scan))]
        assert statuses == [ScanStatus.COMPLETE]

    def test_nothing_due_is_a_clean_no_op(self, session: Session) -> None:
        make_query(session, "fresh", last_scanned_at=NOW - timedelta(minutes=1))
        fake = self._fake()
        url = fake.start()
        try:
            result = self._tick(session, url)
        finally:
            fake.stop()
        assert result == schedule.TickResult(considered=0)
        assert "0 due" in result.summary()


class TestScannerLock:
    def test_a_second_holder_is_refused_and_the_first_keeps_it(
        self, dsn: str
    ) -> None:
        """Two connections, because an advisory lock is per-session.

        This is the whole point of the lock: a CronJob pass and an operator's manual
        scan are different connections, and Kubernetes' concurrencyPolicy cannot see
        the second one.
        """
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session as RawSession

        first_engine = create_engine(dsn, future=True)
        second_engine = create_engine(dsn, future=True)
        try:
            with RawSession(first_engine) as first, RawSession(second_engine) as second:
                with schedule.scanner_lock(first) as held:
                    assert held is True
                    with schedule.scanner_lock(second) as also_held:
                        assert also_held is False
                # Released: a fresh attempt now succeeds.
                with schedule.scanner_lock(second) as after:
                    assert after is True
        finally:
            first_engine.dispose()
            second_engine.dispose()

    def test_the_lock_is_released_even_when_the_body_raises(self, dsn: str) -> None:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session as RawSession

        engine_a = create_engine(dsn, future=True)
        engine_b = create_engine(dsn, future=True)
        try:
            with RawSession(engine_a) as first, RawSession(engine_b) as second:
                with pytest.raises(RuntimeError), schedule.scanner_lock(first) as held:
                    assert held is True
                    raise RuntimeError("boom")
                with schedule.scanner_lock(second) as after:
                    assert after is True, "a crashed pass must not wedge the scanner"
        finally:
            engine_a.dispose()
            engine_b.dispose()


def test_the_summary_names_the_lock_case() -> None:
    result = schedule.TickResult(lock_held_elsewhere=True)
    assert "holds the lock" in result.summary()
