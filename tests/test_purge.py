"""The purge is a compliance claim, so it is proven rather than asserted."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from touchstone.db.models import (
    DeletionReceipt,
    Listing,
    ListingObservation,
    Query,
    Scan,
    ScanAggregate,
    ScanStatus,
)
from touchstone.sink.purge import (
    DeletionIdentifiers,
    purge_seller,
    unmatched_rate,
)


def seed(
    session: Session,
    *,
    item_id: str,
    username: str | None = None,
    user_id: str | None = None,
    eias: str | None = None,
    observations: int = 2,
) -> Listing:
    query = session.scalars(select(Query).where(Query.name == "purge-fixture")).first()
    if query is None:
        query = Query(name="purge-fixture", q="ecc ddr4")
        session.add(query)
        session.flush()
    scan = Scan(query_id=query.id, status=ScanStatus.COMPLETE)
    session.add(scan)
    session.flush()

    listing = Listing(
        item_id=item_id,
        title="32GB 2Rx4 PC4-2400T ECC REG",
        title_hash="h" * 64,
        seller_username=username,
        seller_user_id=user_id,
        seller_eias_token=eias,
        condition_id="3000",
    )
    session.add(listing)
    session.flush()
    for _ in range(observations):
        session.add(
            ListingObservation(
                listing_id=item_id,
                scan_id=scan.id,
                price=100.0,
                total_cost=100.0,
                currency="USD",
            )
        )
        # A fresh scan per observation keeps the (listing, scan) uniqueness honest.
        scan = Scan(query_id=query.id, status=ScanStatus.COMPLETE)
        session.add(scan)
        session.flush()
    session.flush()
    return listing


def test_purge_deletes_the_listing_and_its_observations(session: Session) -> None:
    seed(session, item_id="v1|1|0", username="doomed", observations=2)

    outcome = purge_seller(
        session, "n-1", DeletionIdentifiers(username="doomed", user_id="U1", eias_token="E1")
    )

    assert outcome.listings_deleted == 1
    assert outcome.observations_deleted == 2
    assert outcome.unmatched is False
    assert session.get(Listing, "v1|1|0") is None
    assert session.scalars(select(ListingObservation)).all() == []


def test_userid_in_the_username_field_still_matches(session: Session) -> None:
    """The case eBay's 2025-09-26 change introduces.

    For affected U.S. users the ``username`` field carries the immutable user id,
    not a username. Pairing username->seller_username would match nothing and look
    exactly like 'we hold no data for this user' — acking a deletion never done.
    """
    # Browse gave us the user id in seller.username, so that is what we stored.
    seed(session, item_id="v1|2|0", username="1234567890")

    outcome = purge_seller(
        session,
        "n-2",
        # eBay sends the same immutable id in BOTH fields for these users.
        DeletionIdentifiers(username="1234567890", user_id="1234567890", eias_token="E2"),
    )

    assert outcome.unmatched is False
    assert outcome.listings_deleted == 1


def test_match_is_a_cross_product_not_paired_by_field(session: Session) -> None:
    """A stored username may correspond to the notification's userId, or vice
    versa. Any identifier must match any column."""
    seed(session, item_id="v1|3|0", username=None, user_id="U3")
    seed(session, item_id="v1|4|0", username=None, eias="E4")

    # The notification carries these values in the *username* slot only.
    first = purge_seller(session, "n-3", DeletionIdentifiers(username="U3"))
    second = purge_seller(session, "n-4", DeletionIdentifiers(username="E4"))

    assert first.listings_deleted == 1
    assert second.listings_deleted == 1


def test_unknown_user_is_recorded_unmatched_and_is_a_successful_purge(
    session: Session,
) -> None:
    """touchstone samples a narrow slice of eBay and will hold nothing for most
    deleting users. There is nothing to delete, so the obligation is satisfied and
    the sink may ack — withholding one would mark the endpoint down for no reason."""
    seed(session, item_id="v1|5|0", username="someone_else")

    outcome = purge_seller(session, "n-5", DeletionIdentifiers(username="never_seen"))

    assert outcome.unmatched is True
    assert outcome.listings_deleted == 0
    # The untouched seller is still here — a purge must not over-delete.
    assert session.get(Listing, "v1|5|0") is not None

    receipt = session.get(DeletionReceipt, "n-5")
    assert receipt is not None
    assert receipt.unmatched is True
    assert receipt.completed_at is not None


def test_only_the_named_seller_is_deleted(session: Session) -> None:
    seed(session, item_id="v1|6|0", username="doomed")
    seed(session, item_id="v1|7|0", username="bystander")

    purge_seller(session, "n-6", DeletionIdentifiers(username="doomed"))

    assert session.get(Listing, "v1|6|0") is None
    assert session.get(Listing, "v1|7|0") is not None


def test_purge_is_idempotent_by_receipt(session: Session) -> None:
    seed(session, item_id="v1|8|0", username="doomed", observations=1)

    first = purge_seller(session, "n-7", DeletionIdentifiers(username="doomed"))
    second = purge_seller(session, "n-7", DeletionIdentifiers(username="doomed"))

    assert first.already_done is False
    assert second.already_done is True
    assert second.listings_deleted == first.listings_deleted


def test_notification_with_no_identifiers_is_rejected_not_recorded_as_unmatched(
    session: Session,
) -> None:
    """Recording it unmatched would claim we searched. We could not."""
    with pytest.raises(ValueError):
        purge_seller(session, "n-8", DeletionIdentifiers())
    assert session.get(DeletionReceipt, "n-8") is None


def test_identifiers_parsed_from_a_notification_payload() -> None:
    payload = {
        "metadata": {"topic": "MARKETPLACE_ACCOUNT_DELETION"},
        "notification": {
            "notificationId": "abc",
            "data": {"username": "bob", "userId": "U9", "eiasToken": "  "},
        },
    }
    ids = DeletionIdentifiers.from_notification(payload)
    assert ids.username == "bob"
    assert ids.user_id == "U9"
    # Whitespace-only is absent, not an identifier to search for.
    assert ids.eias_token is None
    assert ids.values() == {"bob", "U9"}


def test_duplicate_identifiers_are_searched_once() -> None:
    ids = DeletionIdentifiers(username="X", user_id="X", eias_token=None)
    assert ids.values() == {"X"}


def test_purge_does_not_touch_scan_aggregates(session: Session) -> None:
    """The whole reason aggregates are materialized: a deletion must not rewrite
    recorded history."""
    listing = seed(session, item_id="v1|9|0", username="doomed", observations=1)
    scan_id = session.scalars(select(ListingObservation.scan_id)).first()
    assert scan_id is not None
    query_id = session.scalars(select(Query.id).where(Query.name == "purge-fixture")).one()
    session.add(
        ScanAggregate(
            scan_id=scan_id,
            query_id=query_id,
            observed_at=listing.first_seen_at,
            cohort_key="q=1|cond=3000",
            n=7,
            currency="USD",
            price_min=10,
            price_p10=20,
            price_p25=30,
            price_median=40,
            price_mean=50,
        )
    )
    session.flush()
    before = [(a.n, float(a.price_median)) for a in session.scalars(select(ScanAggregate))]

    purge_seller(session, "n-9", DeletionIdentifiers(username="doomed"))

    after = [(a.n, float(a.price_median)) for a in session.scalars(select(ScanAggregate))]
    assert after == before


def test_unmatched_rate_reports_the_share(session: Session) -> None:
    seed(session, item_id="v1|10|0", username="held")
    purge_seller(session, "r-1", DeletionIdentifiers(username="held"))
    purge_seller(session, "r-2", DeletionIdentifiers(username="absent-a"))
    purge_seller(session, "r-3", DeletionIdentifiers(username="absent-b"))

    # 2 of 3 matched nothing. A single unmatched event is unremarkable; the rate is
    # the only signal that our identifier space has diverged from eBay's.
    assert unmatched_rate(session) == pytest.approx(2 / 3)


def test_unmatched_rate_is_zero_with_no_history(session: Session) -> None:
    assert unmatched_rate(session) == 0.0


def test_known_gap_username_stored_against_userid_notification(session: Session) -> None:
    """The residual linkage failure the cross-product match cannot fix.

    Browse's seller object exposes only `username`. If it gave us a real username
    while eBay's notification carries only an immutable user id, there is nothing to
    join on and we keep data we were told to erase.

    This test pins the gap as KNOWN rather than pretending it is closed. It is the
    justification for tracking `unmatched_rate`: one such event is indistinguishable
    from genuinely holding no data, and only the rate reveals it.
    """
    seed(session, item_id="v1|11|0", username="a_real_username")

    outcome = purge_seller(
        session,
        "gap-1",
        DeletionIdentifiers(username="1234567890", user_id="1234567890", eias_token="E"),
    )

    # Documents current behavior. If this ever starts matching, Browse began
    # returning a joinable identifier and the gap has genuinely closed.
    assert outcome.unmatched is True
    assert session.get(Listing, "v1|11|0") is not None
