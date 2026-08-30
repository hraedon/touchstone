"""Budget guard.

The property that matters most here is negative: an unreadable quota must never
become an unlimited quota.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from tests.fake_ebay import FakeEbay, Generation
from touchstone.db.models import RateBudget
from touchstone.ebay.budget import (
    DEFAULT_DAILY_LIMIT,
    RESERVE,
    BudgetExhausted,
    BudgetGuard,
    today_utc,
)
from touchstone.ebay.client import Credentials, EbayClient


@pytest.fixture
def fake() -> FakeEbay:
    return FakeEbay(generations=[Generation(items=[])])


def client_for(fake: FakeEbay) -> EbayClient:
    return EbayClient(
        credentials=Credentials("id", "secret"),
        base_url=fake.start(),
    )


def test_authoritative_read_is_used_and_reanchors_the_ledger(
    session: Session, fake: FakeEbay
) -> None:
    fake.rate_limit_remaining = 4200
    fake.rate_limit_total = 5000
    with client_for(fake) as client:
        guard = BudgetGuard(session, client)
        state = guard.state()

    assert state.authoritative is True
    assert state.remaining == 4200
    row = session.get(RateBudget, today_utc())
    assert row is not None
    # The ledger is re-anchored to eBay's view so local drift cannot accumulate.
    assert row.calls_used == 800
    assert row.last_authoritative_remaining == 4200
    fake.stop()


def test_unreadable_quota_falls_back_to_the_ledger_not_to_unlimited(
    session: Session, fake: FakeEbay
) -> None:
    """The whole point of this module. A failed getRateLimits must degrade to the
    conservative local count, never to permission to proceed."""
    fake.rate_limit_fails = True
    session.add(RateBudget(day=today_utc(), calls_used=4900, calls_limit=5000))
    session.flush()

    with client_for(fake) as client:
        guard = BudgetGuard(session, client)
        state = guard.state()
        # check() re-reads through the client, so it must run before the client
        # is closed.
        usable_now = guard.check(1)

    assert state.authoritative is False
    assert state.remaining == 100
    # 100 remaining minus the reserve leaves nothing usable.
    assert state.usable == 0
    assert usable_now == 0
    fake.stop()


def test_check_is_clamped_by_the_reserve(session: Session) -> None:
    session.add(
        RateBudget(
            day=today_utc(),
            calls_used=DEFAULT_DAILY_LIMIT - RESERVE - 3,
            calls_limit=DEFAULT_DAILY_LIMIT,
        )
    )
    session.flush()
    guard = BudgetGuard(session, client=None)
    assert guard.check(10) == 3


def test_require_raises_when_short(session: Session) -> None:
    session.add(
        RateBudget(
            day=today_utc(),
            calls_used=DEFAULT_DAILY_LIMIT,
            calls_limit=DEFAULT_DAILY_LIMIT,
        )
    )
    session.flush()
    guard = BudgetGuard(session, client=None)
    with pytest.raises(BudgetExhausted):
        guard.require(1)


def test_record_accumulates(session: Session) -> None:
    guard = BudgetGuard(session, client=None)
    guard.record(3)
    guard.record(4)
    row = session.get(RateBudget, today_utc())
    assert row is not None
    assert row.calls_used == 7


def test_record_ignores_non_positive(session: Session) -> None:
    guard = BudgetGuard(session, client=None)
    guard.record(5)
    guard.record(0)
    guard.record(-2)
    row = session.get(RateBudget, today_utc())
    assert row is not None
    assert row.calls_used == 5
