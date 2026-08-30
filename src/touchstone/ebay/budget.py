"""API call budget.

The daily Browse allowance (5,000, application-wide) is the binding constraint on
how often and how deeply touchstone can scan, so it is tracked explicitly rather
than hoped about.

The rule that matters: **an unreadable quota is not an unlimited quota.** When
eBay's getRateLimits call fails, this falls back to the local ledger, which is
conservative by construction. It never falls back to "proceed". A budget check whose
failure mode is silent permission is exactly the kind of check that looks green right
up until the application is throttled off the API for the rest of the day.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from touchstone.db.models import RateBudget, utcnow
from touchstone.ebay.client import EbayClient

log = logging.getLogger("touchstone.budget")

DEFAULT_DAILY_LIMIT = 5000

# Headroom so an operator's manual scan is never the call that runs the application
# into the wall. Note this does NOT cover the deletion sink's getPublicKey call —
# that is the Notification API, a separate allowance — but that key is cached for an
# hour precisely because eBay warns an uncached fetch per notification can exceed
# call limits during a burst.
RESERVE = 100


class BudgetExhausted(RuntimeError):
    """Not enough remaining quota to run the requested work."""


def today_utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


@dataclass
class BudgetState:
    remaining: int
    limit: int
    authoritative: bool

    @property
    def usable(self) -> int:
        """Remaining quota minus the safety reserve, floored at zero."""
        return max(0, self.remaining - RESERVE)


class BudgetGuard:
    """Reads quota from eBay when it can, from the local ledger when it cannot."""

    def __init__(self, session: Session, client: EbayClient | None = None) -> None:
        self._session = session
        self._client = client

    def _ledger(self, day: str | None = None) -> RateBudget:
        day = day or today_utc()
        row = self._session.get(RateBudget, day)
        if row is None:
            row = RateBudget(day=day, calls_used=0, calls_limit=DEFAULT_DAILY_LIMIT)
            self._session.add(row)
            self._session.flush()
        return row

    def state(self) -> BudgetState:
        """Current budget. Authoritative when eBay answered, ledger-based otherwise."""
        row = self._ledger()

        if self._client is not None:
            limits = self._client.rate_limit()
            if limits is not None:
                row.calls_limit = limits.limit
                row.last_authoritative_read = utcnow()
                row.last_authoritative_remaining = limits.remaining
                # Re-anchor the ledger to eBay's view so drift cannot accumulate.
                row.calls_used = max(0, limits.limit - limits.remaining)
                self._session.flush()
                return BudgetState(
                    remaining=limits.remaining, limit=limits.limit, authoritative=True
                )
            log.warning(
                "quota unreadable; falling back to the local ledger "
                "(used=%d limit=%d). Not proceeding as if unlimited.",
                row.calls_used,
                row.calls_limit,
            )

        return BudgetState(
            remaining=max(0, row.calls_limit - row.calls_used),
            limit=row.calls_limit,
            authoritative=False,
        )

    def check(self, wanted: int) -> int:
        """Return how many calls may be spent, up to `wanted`.

        Returns 0 when nothing may be spent. Callers must respect a 0 rather than
        pressing on.
        """
        if wanted <= 0:
            return 0
        return min(wanted, self.state().usable)

    def require(self, wanted: int) -> None:
        """Raise BudgetExhausted unless the full amount is available."""
        allowed = self.check(wanted)
        if allowed < wanted:
            state = self.state()
            raise BudgetExhausted(
                f"need {wanted} calls, {allowed} available "
                f"(remaining={state.remaining} limit={state.limit} "
                f"authoritative={state.authoritative})"
            )

    def record(self, calls: int) -> None:
        """Add spent calls to the ledger."""
        if calls <= 0:
            return
        row = self._ledger()
        row.calls_used += calls
        self._session.flush()


def recent_budgets(session: Session, days: int = 7) -> list[RateBudget]:
    stmt = select(RateBudget).order_by(RateBudget.day.desc()).limit(days)
    return list(session.scalars(stmt))
