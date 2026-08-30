"""The deletion endpoint.

The compliance claim is now structural rather than behavioural: there is no seller
data to erase, so the strongest tests here are the ones that check the *absence* of
a place to store it. A behavioural test can only show that a purge ran; these show
there is nothing a purge could have missed.
"""

from __future__ import annotations

import json
import threading
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import Engine, delete, func, inspect, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.dml import Insert

from tests.fake_ebay import item
from touchstone.db.models import Base, DeletionReceipt, Listing, Query, Scan, ScanStatus
from touchstone.ebay.client import ParsedListing, parse_item_summary
from touchstone.sink.purge import handle_deletion, notification_id_of, receipts

SAMPLE: dict[str, Any] = {
    "metadata": {
        "topic": "MARKETPLACE_ACCOUNT_DELETION",
        "schemaVersion": "1.0",
        "deprecated": False,
    },
    "notification": {
        "notificationId": "11111111-2222-3333-4444-555555555555",
        "eventDate": "2026-08-30T20:43:59.462Z",
        "publishDate": "2026-08-30T20:43:59.679Z",
        "publishAttemptCount": 1,
        "data": {
            "username": "some_seller",
            "userId": "1234567890",
            "eiasToken": "nY+sHZ2PrBmdj6wVnY+sEZ2PrA2dj6wFk4GhDpKEpQmdj6x9nY+seQ==",
        },
    },
}


# Tables holding one row per listing (or per listing-derived fact). Nothing here may
# carry a value derived from an individual seller — that is what would make a row
# attributable to a person.
PER_LISTING_TABLES = {
    "listing",
    "listing_observation",
    "listing_disappearance",
    "scan_aggregate",
    "item_spec",
    "deal",
    "watch",
}

# Names that are an eBay user identifier outright, wherever they appear.
IDENTIFIER_NAMES = {
    "seller_username",
    "seller_user_id",
    "seller_eias_token",
    "username",
    "user_id",
    "eias_token",
}

SELLER_WORDS = ("seller", "username", "eias")


def _identifier_columns(table_name: str, column_names: list[str]) -> list[str]:
    """Columns that could make a row attributable to an individual seller.

    Deliberately not a blanket ban on the word "seller". `query.min_seller_feedback`
    and `scan.excluded_low_feedback` are a configured threshold and a counter — they
    describe the *filter*, not any particular seller, and live on config/metadata
    tables rather than on a listing. The invariant is about attribution, not
    vocabulary, so the check is scoped to where attribution could happen.
    """
    offenders = []
    for name in column_names:
        lowered = name.lower()
        attributable = table_name in PER_LISTING_TABLES and any(
            w in lowered for w in SELLER_WORDS
        )
        if lowered in IDENTIFIER_NAMES or attributable:
            offenders.append(f"{table_name}.{name}")
    return offenders


class TestNoPlaceToStoreASeller:
    """The structural guarantee. If these fail, the whole design is undone."""

    def test_no_table_has_a_seller_column(self) -> None:
        offenders: list[str] = []
        for table in Base.metadata.tables.values():
            offenders += _identifier_columns(table.name, [c.name for c in table.columns])
        assert offenders == [], (
            "a column exists that could attribute a row to an individual eBay "
            f"seller: {offenders}. Storing one reintroduces the purge, the replay "
            "after restore, and the register of deleted users."
        )

    def test_the_live_schema_agrees(self, engine: Engine) -> None:
        """Guards the guard: the model metadata and the real database could drift."""
        inspector = inspect(engine)
        offenders: list[str] = []
        for table_name in inspector.get_table_names():
            offenders += _identifier_columns(
                table_name, [c["name"] for c in inspector.get_columns(table_name)]
            )
        assert offenders == []

    def test_the_check_would_catch_a_reintroduced_identifier(self) -> None:
        """Guards the guard's guard. The scoping above narrowed what counts as a
        violation, so prove the narrowing did not defang it."""
        assert _identifier_columns("listing", ["seller_username"]) == [
            "listing.seller_username"
        ]
        assert _identifier_columns("listing", ["seller_feedback_score"]) == [
            "listing.seller_feedback_score"
        ]
        assert _identifier_columns("deletion_receipt", ["username"]) == [
            "deletion_receipt.username"
        ]
        # ...and that a threshold on a config table still passes.
        assert _identifier_columns("query", ["min_seller_feedback"]) == []

    def test_the_client_does_not_even_parse_the_seller(self) -> None:
        """Dropped at the API boundary, so an identifier never enters the process.

        eBay's response definitely contains it — this asserts we decline to read it,
        not that eBay withheld it.
        """
        raw = item("v1|1|0", price=100.0, seller="a_real_username")
        seller = raw["seller"]
        assert seller["username"] == "a_real_username"

        parsed = parse_item_summary(raw)
        assert parsed is not None
        assert not hasattr(parsed, "seller_username")
        assert "a_real_username" not in repr(parsed)

    def test_parsed_listing_has_no_seller_field(self) -> None:
        assert not any("seller" in f for f in ParsedListing.__dataclass_fields__)


class TestAcknowledgement:
    def test_notification_is_recorded_and_acknowledged(self, session: Session) -> None:
        outcome = handle_deletion(session, notification_id_of(SAMPLE))

        assert outcome.already_seen is False
        receipt = session.get(DeletionReceipt, SAMPLE["notification"]["notificationId"])
        assert receipt is not None
        assert receipt.acknowledged_at is not None

    def test_the_receipt_stores_no_identifiers(self, session: Session) -> None:
        """Retaining them to prove we retain nothing would defeat the point."""
        handle_deletion(session, notification_id_of(SAMPLE))
        receipt = receipts(session)[0]

        blob = json.dumps(
            {
                c.name: str(getattr(receipt, c.name))
                for c in DeletionReceipt.__table__.columns
            }
        )
        data: dict[str, str] = SAMPLE["notification"]["data"]
        for value in data.values():
            assert value not in blob

    def test_redelivery_does_not_create_a_second_receipt(self, session: Session) -> None:
        """eBay resends until acknowledged."""
        first = handle_deletion(session, notification_id_of(SAMPLE))
        second = handle_deletion(session, notification_id_of(SAMPLE))

        assert first.already_seen is False
        assert second.already_seen is True
        assert len(receipts(session)) == 1

    def test_concurrent_redelivery_does_not_race_the_receipt_insert(
        self, engine: Engine
    ) -> None:
        """A rolling deploy briefly has two sink pods able to receive the same ID."""
        notification_id = "concurrent-redelivery-fixture"
        with Session(engine) as cleanup:
            cleanup.execute(
                delete(DeletionReceipt).where(
                    DeletionReceipt.notification_id == notification_id
                )
            )
            cleanup.commit()

        start = threading.Barrier(3)
        both_inserts_ready = threading.Barrier(2)
        errors: list[Exception] = []
        outcomes: list[bool] = []
        original_scalar = Session.scalar

        def synchronized_scalar(
            current: Session,
            statement: Any,
            *args: Any,
            **kwargs: Any,
        ) -> Any:
            if isinstance(statement, Insert):
                both_inserts_ready.wait(timeout=5)
            return original_scalar(current, statement, *args, **kwargs)

        def deliver() -> None:
            try:
                start.wait(timeout=5)
                with Session(engine) as concurrent_session:
                    outcome = handle_deletion(concurrent_session, notification_id)
                    concurrent_session.commit()
                    outcomes.append(outcome.already_seen)
            except Exception as exc:  # the assertion below reports either thread
                errors.append(exc)

        with patch.object(Session, "scalar", synchronized_scalar):
            workers = [threading.Thread(target=deliver) for _ in range(2)]
            for worker in workers:
                worker.start()
            start.wait(timeout=5)
            for worker in workers:
                worker.join(timeout=10)

        assert errors == []
        assert sorted(outcomes) == [False, True]
        with Session(engine) as check:
            count = check.scalar(
                select(func.count()).select_from(DeletionReceipt).where(
                    DeletionReceipt.notification_id == notification_id
                )
            )
            assert count == 1
            check.execute(
                delete(DeletionReceipt).where(
                    DeletionReceipt.notification_id == notification_id
                )
            )
            check.commit()

    def test_nothing_is_deleted(self, session: Session) -> None:
        """The listings are market data about offers, not personal data about the
        user in the notification, so a deletion leaves them alone."""
        query = Query(name="purge-fixture", q="ecc ddr4")
        session.add(query)
        session.flush()
        session.add(Scan(query_id=query.id, status=ScanStatus.COMPLETE))
        session.add(
            Listing(item_id="v1|1|0", title="32GB ECC RDIMM", title_hash="h" * 64)
        )
        session.flush()

        handle_deletion(session, notification_id_of(SAMPLE))

        assert session.get(Listing, "v1|1|0") is not None
        assert len(session.scalars(select(Listing)).all()) == 1


class TestNotificationParsing:
    def test_id_extracted(self) -> None:
        assert notification_id_of(SAMPLE) == "11111111-2222-3333-4444-555555555555"

    def test_missing_notification_object_rejected(self) -> None:
        with pytest.raises(ValueError):
            notification_id_of({"metadata": {}})

    def test_missing_id_rejected(self) -> None:
        with pytest.raises(ValueError):
            notification_id_of({"notification": {"data": {}}})

    def test_blank_id_rejected(self) -> None:
        with pytest.raises(ValueError):
            notification_id_of({"notification": {"notificationId": "   "}})
