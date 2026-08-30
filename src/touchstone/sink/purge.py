"""Deleting a seller's data on an eBay account-deletion notification.

eBay's guidance, and what it means here
---------------------------------------
From the Marketplace User Account Deletion guide, the ``data`` object carries three
identifiers:

* ``username`` — "the publicly known eBay user ID". **"Select developers will not
  receive username data for U.S. users through this field. Instead, an immutable
  user ID will be returned in its place."** (effective 2025-09-26)
* ``userId`` — "the immutable identifier of the eBay user".
* ``eiasToken`` — "the eBay user's EIAS token; another identifier used for an eBay
  user". Stable even when the user changes their eBay user ID.

That detail decides the matching strategy, and it is easy to get wrong. The username
field is not *emptied* for affected users — it is *populated with a different kind of
identifier*. So a naive ``WHERE seller_username = notification.username`` does not
fail loudly with a null; it quietly compares a user id against stored usernames,
matches nothing, and looks exactly like "we do not hold this user."

A false "we hold nothing" is the worst possible outcome here: it acks a deletion we
never performed, which is the same failure the predecessor sink shipped. So we match
**every identifier the notification carries against every identifier column we
store**, rather than pairing them by name. It costs one indexed OR.

That closes the *pairing* error. It does not close everything, and the residual is
worth stating plainly:

===========================================  ==========================
Browse gave us          eBay's notification sends   Result
===========================================  ==========================
a user id               a user id                   matches
a real username         a real username             matches
a real username         a user id (only)            **NO MATCH**
===========================================  ==========================

The third row is unfixable from here: Browse's ``seller`` object exposes only
``username``, never ``userId`` or ``eiasToken``, so if eBay's two identifier spaces
diverge for the same person we have nothing to join on. Such an event is recorded as
unmatched and is indistinguishable, one at a time, from genuinely holding no data.
Only the rate separates them — which is the entire reason ``unmatched_rate`` exists
and why a rising one is a real signal rather than noise.

If nothing matches, acking is correct, and eBay's guide is explicit about the shape
of the obligation: "It is the responsibility of each developer to remove all user
data associated with the eBay user specified in the notification." Holding no data
for that user satisfies it. The guide accepts ``200``, ``201``, ``202``, or ``204``;
unacknowledged notifications are resent, and after ~24h of silence the callback is
marked down with a 30-day window before non-compliance. Withholding an ack because
we found nothing would break the endpoint to no purpose.

The outcome is still recorded as ``unmatched``, because a single unmatched event and
a systematic linkage failure look identical up close. Only the *rate* separates them
— see ``unmatched_rate``.

Volume to design for: eBay says to be prepared for "up to 1500 notifications on any
given day", event-driven and bursty, with many days at zero.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from touchstone.db.models import DeletionReceipt, Listing, ListingObservation

log = logging.getLogger("touchstone.purge")


@dataclass(frozen=True)
class DeletionIdentifiers:
    """The three identifiers from a notification's ``data`` object."""

    username: str | None = None
    user_id: str | None = None
    eias_token: str | None = None

    @classmethod
    def from_notification(cls, payload: dict[str, Any]) -> DeletionIdentifiers:
        data = (payload.get("notification") or {}).get("data") or {}
        return cls(
            username=(data.get("username") or "").strip() or None,
            user_id=(data.get("userId") or "").strip() or None,
            eias_token=(data.get("eiasToken") or "").strip() or None,
        )

    def values(self) -> set[str]:
        """Every non-empty identifier, deduplicated.

        Deduplication matters: for affected U.S. users eBay puts the same immutable
        user id in both ``username`` and ``userId``, and there is no reason to
        search for it twice.
        """
        return {v for v in (self.username, self.user_id, self.eias_token) if v}


@dataclass(frozen=True)
class PurgeOutcome:
    notification_id: str
    listings_deleted: int
    observations_deleted: int
    unmatched: bool
    already_done: bool = False


def _matching_listings(identifiers: set[str]) -> Any:
    """Any stored identifier column equal to any identifier the notification sent.

    Deliberately a cross-product rather than username→username: see the module
    docstring. eBay may send a user id in the username field.
    """
    return or_(
        Listing.seller_username.in_(identifiers),
        Listing.seller_user_id.in_(identifiers),
        Listing.seller_eias_token.in_(identifiers),
    )


def purge_seller(
    session: Session,
    notification_id: str,
    identifiers: DeletionIdentifiers,
) -> PurgeOutcome:
    """Irreversibly delete every listing attributable to these identifiers.

    Idempotent by receipt: a notification already carrying a completed receipt is
    not re-run. The receipt is written only after the delete, so an interrupted
    purge is retried rather than mistaken for a finished one — the same rule that
    makes the sink's retry loop honest.
    """
    existing = session.get(DeletionReceipt, notification_id)
    if existing is not None and existing.completed_at is not None:
        log.info("notification %s already purged; not re-running", notification_id)
        return PurgeOutcome(
            notification_id=notification_id,
            listings_deleted=existing.listings_deleted,
            observations_deleted=existing.observations_deleted,
            unmatched=existing.unmatched,
            already_done=True,
        )

    wanted = identifiers.values()
    if not wanted:
        # A notification with no identifiers at all is malformed. Recording it as
        # unmatched would claim we checked; we did not, and cannot.
        raise ValueError(f"notification {notification_id} carried no identifiers")

    predicate = _matching_listings(wanted)

    item_ids = list(session.scalars(select(Listing.item_id).where(predicate)))
    observation_count = 0
    if item_ids:
        observation_count = int(
            session.scalar(
                select(func.count())
                .select_from(ListingObservation)
                .where(ListingObservation.listing_id.in_(item_ids))
            )
            or 0
        )
        # Observations cascade from the listing; deleting the parent is what makes
        # this irreversible in one step rather than leaving orphaned price rows
        # keyed to a deleted seller.
        session.execute(delete(Listing).where(Listing.item_id.in_(item_ids)))
        session.flush()

    receipt = existing or DeletionReceipt(notification_id=notification_id)
    receipt.username = identifiers.username
    receipt.user_id = identifiers.user_id
    receipt.eias_token = identifiers.eias_token
    receipt.listings_deleted = len(item_ids)
    receipt.observations_deleted = observation_count
    receipt.unmatched = not item_ids
    receipt.completed_at = datetime.now(UTC)
    session.add(receipt)
    session.flush()

    log.info(
        "purge %s: %d listings, %d observations%s",
        notification_id,
        len(item_ids),
        observation_count,
        " (no data held for this user)" if not item_ids else "",
    )
    return PurgeOutcome(
        notification_id=notification_id,
        listings_deleted=len(item_ids),
        observations_deleted=observation_count,
        unmatched=not item_ids,
    )


def unmatched_rate(session: Session, window: int = 100) -> float:
    """Share of recent purges that matched nothing.

    A single unmatched notification is unremarkable — we track a narrow slice of
    eBay. A *rising* rate is the only available signal that our stored identifier
    space has diverged from what eBay sends, which is the failure this design
    cannot otherwise detect.
    """
    stmt = (
        select(DeletionReceipt.unmatched)
        .where(DeletionReceipt.completed_at.is_not(None))
        .order_by(DeletionReceipt.received_at.desc())
        .limit(window)
    )
    rows = list(session.scalars(stmt))
    if not rows:
        return 0.0
    return sum(1 for r in rows if r) / len(rows)
