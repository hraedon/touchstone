"""Honoring operator-queued availability checks for flagged deals.

The web tier holds no eBay credentials and can never spend budget, so a
verification request from the UI is a column on the deal row, and this worker —
run by the scanner after its scans, or by ``touchstone verify`` — answers it
with exactly one Browse ``getItem`` call.

The outcome is recorded as an observation: what the API reported, when. An
``unknown`` is a failed check, not a verdict; the request is cleared so a
transient failure cannot wedge the queue, and the operator can simply ask
again.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from touchstone.db.models import Deal, VerifyOutcome
from touchstone.ebay.budget import BudgetGuard
from touchstone.ebay.client import EbayClient

log = logging.getLogger("touchstone.verify")

# A pass is bounded so a burst of queued requests cannot quietly spend the
# day's remaining allowance; the rest wait for the next pass.
DEFAULT_LIMIT = 10


@dataclass(frozen=True)
class VerifyResult:
    verified: int
    left_queued: int
    api_calls: int


def run_verification(
    session: Session,
    client: EbayClient,
    guard: BudgetGuard,
    *,
    limit: int = DEFAULT_LIMIT,
    now: datetime | None = None,
) -> VerifyResult:
    """Verify queued deals, oldest request first, within the remaining allowance.

    A request is never dropped: if the allowance will not cover its call, it
    stays queued for a later pass. ``get_item`` cannot raise, so outcomes are
    recorded as they happen and only a crash mid-pass can lose the unverified
    remainder — the caller commits what landed.
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
    for deal in queued:
        if guard.check(1) < 1:
            log.warning(
                "allowance will not cover a verification of %s; the request "
                "stays queued for a later pass",
                deal.listing_id,
            )
            break
        availability = client.get_item(deal.listing_id)
        deal.verified_at = moment
        # Both enums carry the same wire values; the conversion is explicit so
        # a drift between them is a test failure here, not a silent mismatch.
        deal.verify_outcome = VerifyOutcome(availability.value)
        deal.verify_requested_at = None
        guard.record(1)
        verified += 1
        log.info("verified %s: %s", deal.listing_id, availability.value)
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
    return VerifyResult(verified=verified, left_queued=left_queued, api_calls=verified)
