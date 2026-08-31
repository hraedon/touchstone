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
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from tests.fake_ebay import FakeEbay, Generation, item
from touchstone.db.models import Query, RateBudget, Scan, ScanStatus
from touchstone.ebay.budget import BudgetGuard, _Anchor, today_utc
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


def test_the_summary_names_the_lock_case() -> None:
    result = schedule.TickResult(lock_held_elsewhere=True)
    assert "holds the lock" in result.summary()


class TestTheQuotaReadIsNotItselfExpensive:
    """`state()` used to call eBay's getRateLimits every single time.

    A pass calls it at least twice per query — once to decide whether to continue,
    once inside run_scan's `check()` — so scanning forty queries made eighty
    Developer Analytics calls, against a separate allowance, none of them recorded
    anywhere. It was visible in the first production run: two getRateLimits requests
    to scan one query.
    """

    def test_a_pass_over_several_queries_reads_the_quota_once(
        self, session: Session
    ) -> None:
        for index in range(4):
            make_query(session, f"q{index}", last_scanned_at=None)
        fake = FakeEbay(
            generations=[Generation(items=[item("v1|1|0", price=100.0)])],
            rate_limit_remaining=5000,
        )
        url = fake.start()
        try:
            with EbayClient(credentials=Credentials("id", "secret"), base_url=url) as client:
                result = schedule.run_tick(session, client, now=NOW)
        finally:
            fake.stop()

        assert result.scanned == 4
        assert fake.rate_limit_calls == 1, (
            f"one authoritative read should serve the whole pass; made "
            f"{fake.rate_limit_calls}"
        )

    def test_the_cached_figure_still_subtracts_what_the_pass_has_spent(
        self, session: Session
    ) -> None:
        """Caching must not make the guard optimistic between reads."""
        clock = iter([0.0] * 50)
        guard = BudgetGuard(session, client=None, clock=lambda: next(clock))
        session.add(RateBudget(day=today_utc(), calls_used=0, calls_limit=100))
        session.flush()

        guard._anchor = _Anchor(limit=100, remaining=100, at=0.0)
        assert guard.state().remaining == 100
        guard.record(30)
        assert guard.state().remaining == 70, "spend since the anchor must be subtracted"

    def test_the_anchor_expires(self, session: Session) -> None:
        """Mutation guard: a cache that never refreshes is a stale figure forever."""
        # The freshness check short-circuits on the first call (no anchor yet), so
        # the ticks are: [anchor stamp, freshness check, new anchor stamp].
        ticks = iter([0.0, 999.0, 999.0, 999.0])
        fake = FakeEbay(
            generations=[Generation(items=[])], rate_limit_remaining=4000
        )
        url = fake.start()
        try:
            with EbayClient(credentials=Credentials("id", "secret"), base_url=url) as client:
                guard = BudgetGuard(session, client, clock=lambda: next(ticks))
                guard.state()
                guard.state()
        finally:
            fake.stop()
        assert fake.rate_limit_calls == 2


class TestSpendSurvivesAFailedScan:
    def test_a_failed_scan_still_costs_its_calls_in_the_ledger(
        self, session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """run_scan records the spend in the same transaction as the failure.

        Rolling that back to recover the session would discard the accounting and
        leave the next pass believing it has allowance that is already gone.
        """
        make_query(session, "doomed", last_scanned_at=None)

        def fail_after_spending(
            session_: Session, client: object, query: Query, *, budget: BudgetGuard
        ) -> None:
            budget.record(7)
            raise RuntimeError("eBay returned something impossible")

        monkeypatch.setattr(schedule, "run_scan", fail_after_spending)

        fake = FakeEbay(
            generations=[Generation(items=[])], rate_limit_remaining=None, rate_limit_fails=True
        )
        url = fake.start()
        try:
            with EbayClient(credentials=Credentials("id", "secret"), base_url=url) as client:
                result = schedule.run_tick(session, client, now=NOW)
        finally:
            fake.stop()

        assert result.failed == 1
        row = session.get(RateBudget, today_utc())
        assert row is not None
        assert row.calls_used == 7, "the calls a failed scan spent are still spent"


class TestWriterLock:
    def test_a_second_holder_is_refused_and_the_first_keeps_it(self, dsn: str) -> None:
        """Two engines, because an advisory lock belongs to one Postgres backend."""
        from sqlalchemy import create_engine

        first = create_engine(dsn, future=True)
        second = create_engine(dsn, future=True)
        try:
            with schedule.writer_lock(first) as held:
                assert held is True
                with schedule.writer_lock(second) as also_held:
                    assert also_held is False
            with schedule.writer_lock(second) as after:
                assert after is True
        finally:
            first.dispose()
            second.dispose()

    def test_the_lock_is_released_even_when_the_body_raises(self, dsn: str) -> None:
        from sqlalchemy import create_engine

        first = create_engine(dsn, future=True)
        second = create_engine(dsn, future=True)
        try:
            with pytest.raises(RuntimeError), schedule.writer_lock(first) as held:
                assert held is True
                raise RuntimeError("boom")
            with schedule.writer_lock(second) as after:
                assert after is True, "a crashed pass must not wedge the writer"
        finally:
            first.dispose()
            second.dispose()

    def test_the_lock_survives_commits_on_a_pooled_session(self, dsn: str) -> None:
        """The reason the lock takes its own connection.

        A pass commits many times, and each commit returns the ORM session's
        connection to the pool. If the lock rode that connection, the unlock could
        land on a different backend than the lock.
        """
        from sqlalchemy import create_engine, text
        from sqlalchemy.orm import close_all_sessions, sessionmaker

        engine = create_engine(dsn, future=True)
        other = create_engine(dsn, future=True)
        factory = sessionmaker(bind=engine, future=True)
        try:
            with schedule.writer_lock(engine) as held:
                assert held is True
                for _ in range(3):
                    with factory() as session:
                        session.execute(text("SELECT 1"))
                        session.commit()
                with schedule.writer_lock(other) as intruder:
                    assert intruder is False, "the lock must still be held"
            with schedule.writer_lock(other) as after:
                assert after is True
        finally:
            close_all_sessions()
            engine.dispose()
            other.dispose()


class TestTheRecoveryPathCannotAbortThePass:
    """The fix for lost spend introduced its own way to stop a pass.

    Committing the failed scan can itself raise — typically when the failure *was* a
    database error — and the replay that follows can raise too. If either escapes,
    a single bad query takes the rest of the pass with it, which is precisely what
    the error handling exists to prevent.
    """

    def test_a_database_that_refuses_every_write_still_lets_the_pass_continue(
        self, session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        make_query(session, "aaa-doomed", last_scanned_at=None)
        make_query(session, "bbb-fine", last_scanned_at=None)

        reached: list[str] = []
        real_scan = schedule.run_scan

        def scan(session_: Session, client: object, query: Query, **kw: object):  # type: ignore[no-untyped-def]
            reached.append(query.name)
            if query.name == "aaa-doomed":
                raise RuntimeError("the connection went away mid-scan")
            return real_scan(session_, client, query, **kw)  # type: ignore[arg-type]

        commits = {"n": 0}
        real_commit = Session.commit

        def flaky_commit(self: Session) -> None:
            commits["n"] += 1
            # Refuse the commit of the failure *and* the replay that follows it.
            if commits["n"] <= 2:
                raise OperationalError("COMMIT", {}, Exception("write refused"))
            real_commit(self)

        monkeypatch.setattr(schedule, "run_scan", scan)
        monkeypatch.setattr(Session, "commit", flaky_commit)

        fake = FakeEbay(generations=[Generation(items=[item("v1|1|0", price=100.0)])])
        url = fake.start()
        try:
            with EbayClient(credentials=Credentials("id", "secret"), base_url=url) as client:
                result = schedule.run_tick(session, client, now=NOW)
        finally:
            fake.stop()

        assert reached == ["aaa-doomed", "bbb-fine"], (
            "the pass must reach the second query even when nothing can be written "
            "about the first"
        )
        assert result.failed == 1
        assert result.scanned == 1
