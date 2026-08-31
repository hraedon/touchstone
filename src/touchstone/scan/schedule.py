"""Deciding what to scan, and when to stop.

``run_scan`` executes one query. This picks which ones, in what order, and refuses to
keep going once the daily allowance is gone. It is deliberately a separate module
from the scanner: the scanner's job is to be correct about one query, and this one's
job is to be fair and frugal across many.

Three rules here exist because the obvious alternative misbehaves:

* **Explicit requests first, then most-overdue first.** Round-robin would let a
  60-minute query and a daily query take turns, which is not what either asked for.
* **Stop the pass when nothing can be spent**, rather than letting every remaining
  query record its own ``SKIPPED_BUDGET`` row. One refusal is a fact; forty identical
  refusals bury it.
* **One pass at a time, cluster-wide.** A CronJob overlapping an operator's manual
  scan would double-spend the allowance and could put two writers on one query.
  Kubernetes' ``concurrencyPolicy`` stops the job colliding with itself; only a lock
  in the database stops it colliding with a person.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from touchstone.db.models import Query, ScanStatus
from touchstone.ebay.budget import BudgetGuard
from touchstone.ebay.client import EbayClient
from touchstone.scan.runner import ScanSkipped, run_scan

log = logging.getLogger("touchstone.schedule")

# Arbitrary but fixed: a Postgres advisory lock key is a namespace agreed by
# convention, and this is touchstone's scanner. Changing it silently disables the
# mutual exclusion, so it lives here with a name rather than inline as a number.
SCANNER_LOCK_KEY = 0x70756C73  # "puls"

# Re-exported so a test can patch the name this module actually calls. Patching
# ``touchstone.scan.runner.run_scan`` would not affect the reference bound here.
__all__ = ["SCANNER_LOCK_KEY", "TickResult", "due_queries", "run_scan", "run_tick", "scanner_lock"]


def due_queries(session: Session, *, now: datetime | None = None) -> list[Query]:
    """Enabled queries that should be scanned, in the order they should be taken.

    Ordering is: explicitly requested first, then by how far past cadence they are.
    A query never scanned is maximally overdue, which is what makes a new query run
    on the next pass rather than waiting out a cadence it has never served.
    """
    moment = now or datetime.now(UTC)
    candidates = session.scalars(select(Query).where(Query.enabled.is_(True))).all()

    def overdue_by(query: Query) -> float:
        if query.last_scanned_at is None:
            return float("inf")
        elapsed = moment - _aware(query.last_scanned_at)
        return (elapsed - timedelta(minutes=query.cadence_minutes)).total_seconds()

    due = [
        query
        for query in candidates
        if query.scan_requested_at is not None or overdue_by(query) >= 0
    ]
    due.sort(
        key=lambda query: (
            0 if query.scan_requested_at is not None else 1,
            -overdue_by(query),
            query.id,
        )
    )
    return due


def _aware(moment: datetime) -> datetime:
    """Postgres returns tz-aware datetimes; a test fixture may not."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


@contextmanager
def scanner_lock(session: Session) -> Iterator[bool]:
    """Hold the cluster-wide scanner lock, or report that someone else has it.

    Session-scoped rather than transaction-scoped, because a pass spans many
    transactions. Released explicitly on the way out; a lost connection releases it
    too, which is the behaviour we want if a pod is killed mid-pass.
    """
    acquired = bool(
        session.execute(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": SCANNER_LOCK_KEY}
        ).scalar()
    )
    try:
        yield acquired
    finally:
        if acquired:
            session.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": SCANNER_LOCK_KEY}
            )


@dataclass
class TickResult:
    """What one scheduler pass did. Every field is something an operator asks about."""

    considered: int = 0
    scanned: int = 0
    skipped_budget: int = 0
    failed: int = 0
    api_calls: int = 0
    listings: int = 0
    deals: int = 0
    stopped_on_budget: bool = False
    lock_held_elsewhere: bool = False
    scan_ids: list[int] = field(default_factory=list)

    def summary(self) -> str:
        if self.lock_held_elsewhere:
            return "another scanner pass holds the lock; nothing attempted"
        parts = [
            f"{self.considered} due",
            f"{self.scanned} scanned",
            f"{self.listings} listings",
            f"{self.deals} deals",
            f"{self.api_calls} API calls",
        ]
        if self.failed:
            parts.append(f"{self.failed} FAILED")
        if self.skipped_budget:
            parts.append(f"{self.skipped_budget} skipped (budget)")
        if self.stopped_on_budget:
            parts.append("stopped: allowance exhausted")
        return ", ".join(parts)


def run_tick(
    session: Session,
    client: EbayClient,
    *,
    now: datetime | None = None,
    limit: int | None = None,
) -> TickResult:
    """Scan everything currently due, within the remaining allowance.

    Commits after each query so a failure late in the pass does not discard the
    scans that already succeeded — each scan is a complete, independent fact.
    """
    result = TickResult()
    queries = due_queries(session, now=now)
    result.considered = len(queries)
    if limit is not None:
        queries = queries[:limit]

    guard = BudgetGuard(session, client)
    for query in queries:
        # Re-read the allowance each time: the previous query just spent some of it.
        if guard.state().usable <= 0:
            result.stopped_on_budget = True
            log.warning(
                "stopping the pass: no usable allowance remains. %d due queries were "
                "not attempted, and are not recorded as refusals.",
                len(queries) - result.scanned - result.failed - result.skipped_budget,
            )
            session.commit()
            break

        try:
            outcome = run_scan(session, client, query, budget=guard)
        except ScanSkipped as exc:
            # run_scan has already recorded the refusal against a scan row.
            session.commit()
            result.skipped_budget += 1
            log.warning("query %s skipped: %s", query.name, exc)
            continue
        except Exception:
            session.rollback()
            result.failed += 1
            log.exception("query %s failed; the pass continues", query.name)
            continue

        session.commit()
        result.scan_ids.append(outcome.scan_id)
        result.api_calls += outcome.api_calls
        if outcome.status is ScanStatus.COMPLETE:
            result.scanned += 1
            result.listings += outcome.observed
            result.deals += outcome.deals
        else:
            result.failed += 1

    return result
