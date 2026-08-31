"""Retention: dropping old observations without rewriting history.

This command is only safe because of the load-bearing decision recorded in
``docs/measurement-model.md``: per-cohort statistics are materialized when a scan
runs and are **never** recomputed. Because of that, deleting the listings a statistic
was computed from cannot change the statistic. If aggregates were derived on demand,
this file could not exist — every prune would silently rewrite every historical chart
and there would be no way to tell that from a market that moved.

So the rule has teeth here, and the tests assert the consequence rather than the
intention: prune, then check the aggregates are identical.

What is never touched, and why
------------------------------
``scan_aggregate``
    The history itself. It deliberately has no foreign key to ``listing``.

``listing_disappearance``
    Likewise: ``listing_item_id`` is a plain column, not a foreign key, so that a
    disappearance survives the listing it refers to.

``scan``
    ``scan_aggregate.scan_id`` and ``deal.scan_id`` are ``ON DELETE CASCADE``.
    Deleting a scan row would take the aggregates with it, quietly. Nothing in this
    module may write to that table, and a test checks the module for it.

Listings are also kept regardless of age when they are pinned to the watchlist or
carry an undismissed deal. Both hold cascading foreign keys to ``listing``, so
pruning one would unpin a listing or erase a flag nobody had looked at yet.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from touchstone.db.models import Deal, Listing, ListingObservation, Watch

log = logging.getLogger("touchstone.retention")

# Long by default. Observations are small, the operator has no experience of this
# database's growth yet, and the cost of keeping too much is disk while the cost of
# keeping too little is unrecoverable.
DEFAULT_RETENTION_DAYS = 365

# Refuse to run with a horizon so short it would delete the recent past. There is no
# legitimate reason to prune a week of observations, and a mistyped `--days 3` is a
# very plausible way to lose the only copy.
MINIMUM_RETENTION_DAYS = 30


@dataclass(frozen=True)
class PrunePlan:
    """What a prune would do, or did. The same shape either way, so a dry run and a
    real run are directly comparable."""

    cutoff: datetime
    observations: int
    listings: int
    protected_watched: int
    protected_flagged: int
    dry_run: bool

    def summary(self) -> str:
        verb = "would remove" if self.dry_run else "removed"
        return (
            f"{verb} {self.observations} observation(s) and {self.listings} listing(s) "
            f"older than {self.cutoff.isoformat()}; kept {self.protected_watched} "
            f"watched and {self.protected_flagged} flagged listing(s) regardless of "
            f"age. Aggregates and disappearances are never pruned."
        )


def _protected_listing_ids(session: Session) -> set[str]:
    watched = set(session.scalars(select(Watch.listing_id)))
    flagged = set(
        session.scalars(select(Deal.listing_id).where(Deal.dismissed_at.is_(None)))
    )
    return watched | flagged


def prune(
    session: Session,
    *,
    days: int = DEFAULT_RETENTION_DAYS,
    dry_run: bool = True,
    now: datetime | None = None,
) -> PrunePlan:
    """Drop observations older than ``days``, then listings left with none.

    Defaults to a dry run. A destructive default on a command that takes a horizon as
    an argument is how a mistyped number becomes an incident.
    """
    if days < MINIMUM_RETENTION_DAYS:
        raise ValueError(
            f"retention horizon must be at least {MINIMUM_RETENTION_DAYS} days, got {days}"
        )

    cutoff = (now or datetime.now(UTC)) - timedelta(days=days)
    protected = _protected_listing_ids(session)

    # A protected listing keeps its whole history, not just its row. The watchlist
    # draws that history; pruning it would leave a pinned listing with a blank chart,
    # which is a worse outcome than the disk it saves.
    stale = ListingObservation.observed_at < cutoff
    if protected:
        stale = stale & ListingObservation.listing_id.notin_(protected)

    observation_count = int(
        session.scalar(select(func.count(ListingObservation.id)).where(stale)) or 0
    )

    # Listings that will have nothing left: every observation of them is stale.
    surviving_listing_ids = select(ListingObservation.listing_id).where(~stale)
    orphaned = select(Listing.item_id).where(Listing.item_id.notin_(surviving_listing_ids))
    if protected:
        orphaned = orphaned.where(Listing.item_id.notin_(protected))
    orphan_ids = list(session.scalars(orphaned))

    watched_count = len(set(session.scalars(select(Watch.listing_id))))
    flagged_count = len(
        set(session.scalars(select(Deal.listing_id).where(Deal.dismissed_at.is_(None))))
    )

    plan = PrunePlan(
        cutoff=cutoff,
        observations=observation_count,
        listings=len(orphan_ids),
        protected_watched=watched_count,
        protected_flagged=flagged_count,
        dry_run=dry_run,
    )
    if dry_run:
        log.info("%s", plan.summary())
        return plan

    session.execute(delete(ListingObservation).where(stale))
    if orphan_ids:
        session.execute(delete(Listing).where(Listing.item_id.in_(orphan_ids)))
    session.flush()
    log.info("%s", plan.summary())
    return plan
