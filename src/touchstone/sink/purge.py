"""Handling an eBay Marketplace Account Deletion notification.

There is nothing to delete, and that is the design rather than an oversight.

How this got here
-----------------
An earlier version stored ``seller_username`` on every listing so a notification
could be matched against it. That produced a chain of increasingly awkward problems:

* eBay's ``data.username`` carries an *immutable user id* instead of a username for
  U.S. users of affected developers, so matching had to be a cross-product of every
  identifier against every column — and could still fail when the two identifier
  spaces diverged, with no way to tell that from holding no data;
* a purge had to be replayed after any database restore, or a nightly backup would
  quietly resurrect erased data;
* and that replay needed a durable list of which users had been erased — a permanent
  register of the people who had asked to be forgotten, maintained in order to prove
  they had been forgotten.

Every one of those followed from storing the seller. So we do not store the seller.
The API client drops ``seller.username`` before it reaches the database layer, and no
table has a column for it.

What is left
------------
A listing with no seller attribution is a product offer, not personal data about a
person — the same reasoning that lets ``ScanAggregate`` stand independently, applied
one level down. A deletion notification therefore has nothing to match and nothing to
erase, which is a stronger position than matching correctly would have been: the
claim is checkable from the schema rather than from an audit log we wrote about
ourselves.

We still subscribe and still acknowledge. touchstone persists eBay data (listings,
titles, prices), so the "not persisting eBay data" exemption would be a false
attestation. eBay accepts ``200``/``201``/``202``/``204``; unacknowledged
notifications are resent, and the callback is marked down after ~24h of silence.

The receipt records that a notification arrived and was answered. It stores no
identifiers, because retaining them to prove we retain nothing would defeat the
point.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from touchstone.db.models import DeletionReceipt

log = logging.getLogger("touchstone.purge")


@dataclass(frozen=True)
class DeletionOutcome:
    notification_id: str
    already_seen: bool = False


def notification_id_of(payload: dict[str, Any]) -> str:
    notification = payload.get("notification")
    if not isinstance(notification, dict):
        raise ValueError("notification payload has no notification object")
    raw = notification.get("notificationId")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("notification payload has no notificationId")
    return raw.strip()


def handle_deletion(session: Session, notification_id: str) -> DeletionOutcome:
    """Record that a deletion notification was received and answered.

    Deletes nothing, because there is no seller data to delete. Idempotent: eBay
    resends until acknowledged, and a redelivery must not create a second receipt.
    """
    acknowledged_at = datetime.now(UTC)
    inserted = session.scalar(
        insert(DeletionReceipt)
        .values(notification_id=notification_id, acknowledged_at=acknowledged_at)
        .on_conflict_do_nothing(index_elements=[DeletionReceipt.notification_id])
        .returning(DeletionReceipt.notification_id)
    )
    if inserted is None:
        # A historical/pending receipt still needs completing. The predicate keeps
        # this atomic too: only one concurrent delivery can transition it.
        completed_pending = session.scalar(
            update(DeletionReceipt)
            .where(
                DeletionReceipt.notification_id == notification_id,
                DeletionReceipt.acknowledged_at.is_(None),
            )
            .values(acknowledged_at=acknowledged_at)
            .returning(DeletionReceipt.notification_id)
        )
        if completed_pending is None:
            return DeletionOutcome(notification_id=notification_id, already_seen=True)

    log.info(
        "deletion notification %s acknowledged; no seller data is stored, so "
        "nothing required erasure",
        notification_id,
    )
    return DeletionOutcome(notification_id=notification_id)


def receipts(session: Session, limit: int = 50) -> list[DeletionReceipt]:
    stmt = select(DeletionReceipt).order_by(DeletionReceipt.received_at.desc()).limit(limit)
    return list(session.scalars(stmt))
