"""The public deletion callback, exercised through FastAPI's HTTP boundary."""

from __future__ import annotations

import json
from contextlib import nullcontext

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tests.test_sink_crypto import (
    OFFICIAL_KID,
    OFFICIAL_PAYLOAD,
    OFFICIAL_PUBLIC_KEY,
    OFFICIAL_SIGNATURE,
)
from touchstone.db.models import DeletionReceipt
from touchstone.ebay.client import Credentials
from touchstone.sink.app import SinkSettings, create_app
from touchstone.sink.crypto import NotificationVerifier, load_public_key


class _OfficialKeySource:
    def get_public_key(self, kid: str) -> ec.EllipticCurvePublicKey:
        assert kid == OFFICIAL_KID
        return load_public_key(OFFICIAL_PUBLIC_KEY)


class _AlwaysValidVerifier(NotificationVerifier):
    def verify(self, payload: object, signature_header: str) -> bool:
        return True


def _client(
    session: Session,
    *,
    verifier: NotificationVerifier | None = None,
) -> TestClient:
    settings = SinkSettings(
        dsn="postgresql+psycopg://unused-in-test",
        verification_token="v" * 32,
        endpoint_url="https://ebdel.hraedon.com/",
        ebay_credentials=Credentials("client", "secret"),
    )
    app = create_app(
        settings=settings,
        verifier=verifier or NotificationVerifier(_OfficialKeySource()),
        session_provider=lambda: nullcontext(session),
    )
    return TestClient(app)


def test_endpoint_url_is_pinned_to_the_public_callback() -> None:
    with pytest.raises(ValueError, match="exactly"):
        SinkSettings(
            dsn="postgresql+psycopg://unused-in-test",
            verification_token="v" * 32,
            endpoint_url="https://wrong.example/",
            ebay_credentials=Credentials("client", "secret"),
        )


def test_challenge_is_json_without_a_bom(session: Session) -> None:
    with _client(session) as client:
        response = client.get("/", params={"challenge_code": "123"})

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert not response.content.startswith(b"\xef\xbb\xbf")
    assert response.json() == {
        "challengeResponse": (
            "a902e32868f940c011176b5cb439d680306ff88b69eb098da7fb5abe7c2150d0"
        )
    }


def test_healthz_is_available_internally(session: Session) -> None:
    with _client(session) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_signed_notification_round_trips_and_redelivery_is_idempotent(
    session: Session,
) -> None:
    with _client(session) as client:
        first = client.post(
            "/",
            json=OFFICIAL_PAYLOAD,
            headers={"X-EBAY-SIGNATURE": OFFICIAL_SIGNATURE},
        )
        second = client.post(
            "/",
            json=OFFICIAL_PAYLOAD,
            headers={"X-EBAY-SIGNATURE": OFFICIAL_SIGNATURE},
        )

    assert first.status_code == 200
    assert second.status_code == 200
    notification_id = OFFICIAL_PAYLOAD["notification"]["notificationId"]
    receipt = session.get(DeletionReceipt, notification_id)
    assert receipt is not None
    assert receipt.acknowledged_at is not None
    assert session.scalar(select(func.count()).select_from(DeletionReceipt)) == 1


def test_tampered_notification_returns_412_and_writes_nothing(session: Session) -> None:
    tampered = json.loads(json.dumps(OFFICIAL_PAYLOAD))
    tampered["notification"]["data"]["username"] = "someone_else"

    with _client(session) as client:
        response = client.post(
            "/",
            json=tampered,
            headers={"X-EBAY-SIGNATURE": OFFICIAL_SIGNATURE},
        )

    assert response.status_code == 412
    assert session.scalar(select(func.count()).select_from(DeletionReceipt)) == 0


def test_missing_signature_returns_412_and_writes_nothing(session: Session) -> None:
    with _client(session) as client:
        response = client.post("/", json=OFFICIAL_PAYLOAD)

    assert response.status_code == 412
    assert session.scalar(select(func.count()).select_from(DeletionReceipt)) == 0


def test_oversized_notification_is_rejected_before_verification(session: Session) -> None:
    with _client(session) as client:
        response = client.post(
            "/",
            content=b"{" + (b'\"padding\":\"' + b"x" * 65_536) + b'\"}',
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 413
    assert session.scalar(select(func.count()).select_from(DeletionReceipt)) == 0


def test_signed_notification_for_another_topic_is_not_recorded(session: Session) -> None:
    payload = json.loads(json.dumps(OFFICIAL_PAYLOAD))
    payload["metadata"]["topic"] = "OTHER_SIGNED_TOPIC"
    verifier = _AlwaysValidVerifier(_OfficialKeySource())

    with _client(session, verifier=verifier) as client:
        response = client.post(
            "/",
            json=payload,
            headers={"X-EBAY-SIGNATURE": "signature-accepted-by-test-double"},
        )

    assert response.status_code == 412
    assert session.scalar(select(func.count()).select_from(DeletionReceipt)) == 0
