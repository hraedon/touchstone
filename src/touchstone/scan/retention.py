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

Why the protections are subqueries and not a Python set
-------------------------------------------------------
An earlier version read the protected ids into a set, and the orphan ids into a
list, and only then issued the deletes. Between those two moments a scanner pass —
which runs every fifteen minutes, and would routinely overlap a weekly prune — can
re-observe a listing that was about to be deleted, or flag a new deal on it. The
delete would still name it, and the cascade would take the brand-new observation or
the unexamined flag with it: exactly the silent history-rewrite this module claims
cannot happen.

So every predicate is evaluated by the database at the moment of the delete, inside
one transaction, and the caller holds the same writer lock the scanner takes. The
reported plan can still be a moment stale; the deletion cannot.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import ColumnElement, delete, func, select
from sqlalchemy.orm import InstrumentedAttribute, Session

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


def _is_protected(listing_id: InstrumentedAttribute[str]) -> ColumnElement[bool]:
    """Whether this listing is pinned or carries a flag nobody has dismissed.

    Returned as a SQL expression rather than a set of ids so that it is evaluated by
    the database at the moment of the delete. A set read earlier goes stale the
    instant a scanner pass flags a new deal or an operator pins a listing.
    """
    watched = select(Watch.id).where(Watch.listing_id == listing_id).exists()
    flagged = (
        select(Deal.id)
        .where(Deal.listing_id == listing_id, Deal.dismissed_at.is_(None))
        .exists()
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

    # A protected listing keeps its whole history, not just its row. The watchlist
    # draws that history; pruning it would leave a pinned listing with a blank chart,
    # which is a worse outcome than the disk it saves.
    unprotected = ~_is_protected(ListingObservation.listing_id)
    stale = (ListingObservation.observed_at < cutoff) & unprotected

    observation_count = int(
        session.scalar(select(func.count(ListingObservation.id)).where(stale)) or 0
    )

    # A listing is orphaned when nothing that is being kept refers to it. Expressed
    # against the state *after* the observation delete, so the plan and the delete
    # describe the same thing.
    has_surviving_observation = (
        select(ListingObservation.id)
        .where(ListingObservation.listing_id == Listing.item_id, ~stale)
        .exists()
    )
    orphaned = ~has_surviving_observation & ~_is_protected(Listing.item_id)
    listing_count = int(
        session.scalar(select(func.count(Listing.item_id)).where(orphaned)) or 0
    )

    watched_count = int(
        session.scalar(select(func.count(func.distinct(Watch.listing_id)))) or 0
    )
    flagged_count = int(
        session.scalar(
            select(func.count(func.distinct(Deal.listing_id))).where(
                Deal.dismissed_at.is_(None)
            )
        )
        or 0
    )

    plan = PrunePlan(
        cutoff=cutoff,
        observations=observation_count,
        listings=listing_count,
        protected_watched=watched_count,
        protected_flagged=flagged_count,
        dry_run=dry_run,
    )
    if dry_run:
        log.info("%s", plan.summary())
        return plan

    session.execute(delete(ListingObservation).where(stale))
    # Re-derived here rather than reusing a list of ids read a moment ago: after the
    # delete above, "has no observations left" is a question only the database can
    # answer correctly, and answering it in Python opens the window this module's
    # docstring is about.
    session.execute(
        delete(Listing).where(
            ~select(ListingObservation.id)
            .where(ListingObservation.listing_id == Listing.item_id)
            .exists(),
            ~_is_protected(Listing.item_id),
        )
    )
    session.flush()
    log.info("%s", plan.summary())
    return plan
