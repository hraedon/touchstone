"""Availability checks for flagged deals: queued by the operator, or due on their own.

The web tier holds no eBay credentials and can never spend budget, so a
verification request from the UI is a column on the deal row, and this worker —
run by the scanner after its scans, or by ``touchstone verify`` — answers it
with exactly one Browse ``getItem`` call.

The disappearance series cannot retire every stale flag. A capped query (more
than eBay's 10,000-item paging ceiling) records no disappearances at all, by
design, so a flag from one would sit in the open feed forever after its listing
ended. The second half of a pass therefore checks, unasked, the open flags whose
listing its own query's latest scan did not see — the only flags whose standing
nothing else will ever update. Each is re-checked at most once per
``RECHECK_AFTER``, and only after any operator requests have been answered.

The outcome is recorded as an observation: what the API reported, when. An
``unknown`` is a failed check, not a verdict; the request is cleared so a
transient failure cannot wedge the queue, and the operator can simply ask
again.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session

from touchstone.db.models import (
    Deal,
    Listing,
    ListingDisappearance,
    Query,
    Scan,
    VerifyOutcome,
)
from touchstone.ebay.budget import BudgetGuard
from touchstone.ebay.client import EbayClient

log = logging.getLogger("touchstone.verify")

# A pass is bounded so a burst of queued requests cannot quietly spend the
# day's remaining allowance; the rest wait for the next pass.
DEFAULT_LIMIT = 10

# Unasked checks per pass. At the scanner's 15-minute cadence this clears a
# backlog of a hundred stale flags within a few hours, and once it is clear the
# steady state is one call per unseen flag per RECHECK_AFTER.
DEFAULT_AUTO_LIMIT = 10

# A verification is a snapshot, not a lease: an unseen flag that checked out
# available is asked about again after this long.
RECHECK_AFTER = timedelta(hours=24)


@dataclass(frozen=True)
class VerifyResult:
    verified: int
    left_queued: int
    api_calls: int
    automatic: int = 0


def due_for_automatic_check(
    session: Session, *, now: datetime, limit: int
) -> list[Deal]:
    """Open flags whose listing went unseen, and that no recent check has answered.

    Every condition narrows to flags the feed cannot otherwise settle:

    * not dismissed, not already queued by the operator;
    * the listing is absent from its query's latest scan — a listing still being
      returned by search is live, and spending a call to confirm that is waste;
    * no disappearance newer than its last sighting — that flag is already out
      of the open feed on recorded evidence;
    * not already verified unavailable since its last sighting — likewise;
    * no verification within ``RECHECK_AFTER``.
    """
    retired_by_disappearance = exists().where(
        ListingDisappearance.listing_item_id == Deal.listing_id,
        ListingDisappearance.detected_at > Listing.last_seen_at,
    )
    stmt = (
        select(Deal)
        .join(Listing, Listing.item_id == Deal.listing_id)
        .join(Scan, Scan.id == Deal.scan_id)
        .join(Query, Query.id == Scan.query_id)
        .where(
            Deal.dismissed_at.is_(None),
            Deal.verify_requested_at.is_(None),
            Query.last_scanned_at.is_not(None),
            Listing.last_seen_at < Query.last_scanned_at,
            ~retired_by_disappearance,
            # Spelled as a disjunction, not NOT(outcome AND stamp): a never-checked
            # deal has NULLs there, and NOT(NULL) excludes the row in SQL.
            or_(
                Deal.verify_outcome.is_(None),
                Deal.verify_outcome != VerifyOutcome.UNAVAILABLE,
                Deal.verified_at < Listing.last_seen_at,
            ),
            or_(Deal.verified_at.is_(None), Deal.verified_at < now - RECHECK_AFTER),
        )
        # Never-checked first, then the flags that sit highest in the feed: the
        # top of the page is what the operator reads.
        .order_by(Deal.verified_at.is_not(None), Deal.score.desc(), Deal.id)
        .limit(limit)
    )
    return list(session.scalars(stmt))


def _check(
    deal: Deal, client: EbayClient, guard: BudgetGuard, moment: datetime, why: str
) -> bool:
    """One getItem call for one deal; False when the allowance will not cover it."""
    if guard.check(1) < 1:
        log.warning(
            "allowance will not cover a verification of %s; it waits for a later pass",
            deal.listing_id,
        )
        return False
    availability = client.get_item(deal.listing_id)
    deal.verified_at = moment
    # Both enums carry the same wire values; the conversion is explicit so
    # a drift between them is a test failure here, not a silent mismatch.
    deal.verify_outcome = VerifyOutcome(availability.value)
    deal.verify_requested_at = None
    guard.record(1)
    log.info("verified %s (%s): %s", deal.listing_id, why, availability.value)
    return True


def run_verification(
    session: Session,
    client: EbayClient,
    guard: BudgetGuard,
    *,
    limit: int = DEFAULT_LIMIT,
    auto_limit: int = DEFAULT_AUTO_LIMIT,
    now: datetime | None = None,
) -> VerifyResult:
    """Verify queued deals oldest request first, then unseen flags that are due.

    A request is never dropped: if the allowance will not cover its call, it
    stays queued for a later pass, and no automatic check runs ahead of it.
    ``get_item`` cannot raise, so outcomes are recorded as they happen and only
    a crash mid-pass can lose the unverified remainder — the caller commits
    what landed.
    """
    moment = now or datetime.now(UTC)
    stmt = (
        select(Deal)
        .where(Deal.verify_requested_at.is_not(None))
        .order_by(Deal.verify_requested_at)
        .limit(limit)
    )
    queued = list(session.scalars(stmt))
    verified = 0
    allowance_ran_out = False
    for deal in queued:
        if not _check(deal, client, guard, moment, "requested"):
            allowance_ran_out = True
            break
        verified += 1

    automatic = 0
    if not allowance_ran_out and auto_limit > 0:
        session.flush()
        for deal in due_for_automatic_check(session, now=moment, limit=auto_limit):
            if not _check(deal, client, guard, moment, "unseen since its query's last scan"):
                break
            automatic += 1
    session.flush()
    # Counted from the table, not from the batch: a limit trims what this pass
    # attempts, not what the operator is still waiting on.
    left_queued = int(
        session.scalar(
            select(func.count())
            .select_from(Deal)
            .where(Deal.verify_requested_at.is_not(None))
        )
        or 0
    )
    return VerifyResult(
        verified=verified + automatic,
        left_queued=left_queued,
        api_calls=verified + automatic,
        automatic=automatic,
    )
