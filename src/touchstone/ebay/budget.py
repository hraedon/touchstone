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
import time
from collections.abc import Callable
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

# How long one authoritative read from eBay stands before it is asked for again.
#
# This is not a performance tweak. `state()` used to call getRateLimits on *every*
# invocation, and a scheduler pass calls `state()` at least twice per query — once to
# decide whether to continue, once inside run_scan's `check()`. A pass over forty
# queries therefore made eighty Developer Analytics calls, against a separate
# allowance, none of which were recorded anywhere. Observed in production: two
# getRateLimits calls to scan a single query.
#
# Within the window the figure stays honest by subtracting the calls we know we have
# spent since the anchor was taken. That can only over-count our own spend, never
# under-count it, so the guard stays conservative in the direction that matters.
AUTHORITATIVE_TTL_SECONDS = 60.0


class BudgetExhausted(RuntimeError):
    """Not enough remaining quota to run the requested work."""


def today_utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


@dataclass(frozen=True)
class _Anchor:
    """The last figure eBay gave us, and when."""

    limit: int
    remaining: int
    at: float


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

    def __init__(
        self,
        session: Session,
        client: EbayClient | None = None,
        *,
        authoritative_ttl_seconds: float = AUTHORITATIVE_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._session = session
        self._client = client
        self._ttl = authoritative_ttl_seconds
        self._clock = clock
        self._anchor: _Anchor | None = None
        # Calls spent since the anchor was taken. Held in memory as well as in the
        # ledger so a rolled-back transaction cannot make the rest of this pass
        # believe it has allowance it has already spent.
        self._spent_since_anchor = 0
        # Spend written to the ledger inside a transaction that has not been
        # committed yet. See `committed()` and `replay_uncommitted()`.
        self._uncommitted = 0

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

        anchor = self._anchor
        if anchor is not None and self._clock() - anchor.at < self._ttl:
            # eBay's figure, minus what we have spent since it was taken.
            return BudgetState(
                remaining=max(0, anchor.remaining - self._spent_since_anchor),
                limit=anchor.limit,
                authoritative=True,
            )

        if self._client is not None:
            limits = self._client.rate_limit()
            if limits is not None:
                self._anchor = _Anchor(
                    limit=limits.limit, remaining=limits.remaining, at=self._clock()
                )
                self._spent_since_anchor = 0
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
        """Add spent calls to the ledger, and to this guard's own accounting."""
        if calls <= 0:
            return
        row = self._ledger()
        row.calls_used += calls
        self._session.flush()
        self._spent_since_anchor += calls
        self._uncommitted += calls

    def committed(self) -> None:
        """Tell the guard the ledger write above reached the database."""
        self._uncommitted = 0

    def replay_uncommitted(self) -> int:
        """Re-write spend whose transaction was rolled back, and report how much.

        A failed scan still spent its calls. The ledger write recording that spend
        lives in the same transaction as the failure, so rolling that transaction
        back to recover the session also discards the accounting — leaving the next
        process to believe it has allowance that is already gone. This puts it back.
        """
        pending, self._uncommitted = self._uncommitted, 0
        if pending <= 0:
            return 0
        row = self._ledger()
        row.calls_used += pending
        self._session.flush()
        return pending


def recent_budgets(session: Session, days: int = 7) -> list[RateBudget]:
    stmt = select(RateBudget).order_by(RateBudget.day.desc()).limit(days)
    return list(session.scalars(stmt))
