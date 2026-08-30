"""Interoperability tests for eBay notification cryptography.

The fixed signature and key below come from eBay's official Go Event Notification
SDK test vector.  That matters: a signer and verifier written together can agree on
the same serialization bug and still produce a comfortably green suite.

Fixture values are Copyright 2022 eBay Inc., used under Apache-2.0 from upstream
commit ``698b30b34b0ee2b1a54f0e31e94a1f1249d2d1b3``. See THIRD_PARTY_NOTICES.md.
"""

from __future__ import annotations

import base64
import json
import threading
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from touchstone.ebay.client import Credentials
from touchstone.sink.crypto import (
    NotificationVerifier,
    PublicKeyClient,
    PublicKeyError,
    SignatureFormatError,
    canonical_notification,
    challenge_response,
    decode_signature_header,
    load_public_key,
    verify_notification_signature,
)

OFFICIAL_PUBLIC_KEY = (
    "-----BEGIN PUBLIC KEY-----"
    "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEZhhxXKtR+TOvtDbgTPCkSof02qgB"
    "B7IsYOyf76ilExJ/upAa/vKIKheOoCyOpcLmi4t0b4uepb7LLjmMr90FUg=="
    "-----END PUBLIC KEY-----"
)
OFFICIAL_SIGNATURE = (
    "eyJhbGciOiJlY2RzYSIsImtpZCI6Ijk5MzYyNjFhLTdkN2ItNDYyMS1hMGYxLTk2"
    "Y2NiNDI4YWY0OSIsInNpZ25hdHVyZSI6Ik1FWUNJUUNmeGZJV3V4bVdjSUJRSjljNS"
    "9YN2lHREpxczJSQ0dzQkVhQWppbnlycmZBSWhBSVY2d0djVGlCdVY1S0pVaWYyaG9r"
    "eXJMK1E5c3NIa2FkK214Mm5FRTI1dyIsImRpZ2VzdCI6IlNIQTEifQ=="
)
OFFICIAL_KID = "9936261a-7d7b-4621-a0f1-96ccb428af49"
OFFICIAL_PAYLOAD: dict[str, Any] = {
    "metadata": {
        "topic": "MARKETPLACE_ACCOUNT_DELETION",
        "schemaVersion": "1.0",
        "deprecated": False,
    },
    "notification": {
        "notificationId": (
            "49feeaeb-4982-42d9-a377-9645b8479411_"
            "33f7e043-fed8-442b-9d44-791923bd9a6d"
        ),
        "eventDate": "2021-03-19T20:43:59.462Z",
        "publishDate": "2021-03-19T20:43:59.679Z",
        "publishAttemptCount": 1,
        "data": {
            "username": "test_user",
            "userId": "ma8vp1jySJC",
            "eiasToken": "nY+sHZ2PrBmdj6wVnY+sEZ2PrA2dj6wJnY+gAZGEpwmdj6x9nY+seQ==",
        },
    },
}


def test_challenge_matches_ebay_sdk_vector() -> None:
    """Official SDK fixture inputs, with the expected digest pinned independently."""
    value = "71745723-d031-455c-bfa5-f90d11b4f20a"
    assert challenge_response(value, value, "http://www.testendpoint.com/webhook") == (
        "048de9ffd0e35021fefc0388d5c52e0f475324f582f3778ca81a53354ccbbc97"
    )


def test_canonical_notification_matches_go_struct_order_and_escaping() -> None:
    # Deliberately supply keys out of order. Go marshals struct declaration order,
    # not the order in which JSON fields happened to arrive.
    payload: dict[str, Any] = {
        "notification": {
            "data": {
                "eiasToken": "token\u2028tail",
                "userId": "user-id",
                "username": "seller<&>",
            },
            "publishAttemptCount": 1,
            "publishDate": "2026-08-30T20:43:59.679Z",
            "eventDate": "2026-08-30T20:43:59.462Z",
            "notificationId": "notification-id",
        },
        "metadata": {
            "deprecated": False,
            "schemaVersion": "1.0",
            "topic": "MARKETPLACE_ACCOUNT_DELETION",
        },
        "ignoredByTheGoStruct": True,
    }

    assert canonical_notification(payload) == (
        b'{"metadata":{"topic":"MARKETPLACE_ACCOUNT_DELETION",'
        b'"schemaVersion":"1.0","deprecated":false},"notification":{'
        b'"notificationId":"notification-id",'
        b'"eventDate":"2026-08-30T20:43:59.462Z",'
        b'"publishDate":"2026-08-30T20:43:59.679Z",'
        b'"publishAttemptCount":1,"data":{'
        b'"username":"seller\\u003c\\u0026\\u003e","userId":"user-id",'
        b'"eiasToken":"token\\u2028tail"}}}'
    )


def test_official_ebay_signature_vector_verifies() -> None:
    public_key = load_public_key(OFFICIAL_PUBLIC_KEY)
    canonical = canonical_notification(OFFICIAL_PAYLOAD)
    signature = decode_signature_header(OFFICIAL_SIGNATURE).signature

    assert verify_notification_signature(public_key, signature, canonical)

    tampered = json.loads(json.dumps(OFFICIAL_PAYLOAD))
    tampered["notification"]["data"]["username"] = "someone_else"
    assert not verify_notification_signature(
        public_key,
        signature,
        canonical_notification(tampered),
    )


def test_inline_public_key_is_normalized_and_must_be_p256() -> None:
    key = load_public_key(OFFICIAL_PUBLIC_KEY)
    assert isinstance(key, ec.EllipticCurvePublicKey)
    assert isinstance(key.curve, ec.SECP256R1)


@pytest.mark.parametrize(
    "raw",
    [
        "not base64",
        base64.b64encode(b"not json").decode(),
        base64.b64encode(b'{}').decode(),
        base64.b64encode(b'{"kid":"key","signature":""}').decode(),
    ],
)
def test_malformed_signature_headers_are_rejected(raw: str) -> None:
    with pytest.raises(SignatureFormatError):
        decode_signature_header(raw)


def test_public_key_client_uses_oauth_quotes_kid_and_caches_for_one_hour() -> None:
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/identity/v1/oauth2/token":
            assert request.headers["authorization"] == "Basic Y2xpZW50OnNlY3JldA=="
            return httpx.Response(200, json={"access_token": "app-token", "expires_in": 7200})
        assert request.url.raw_path.endswith(b"/key%2Fwith%20space")
        assert request.headers["authorization"] == "Bearer app-token"
        return httpx.Response(
            200,
            json={"key": OFFICIAL_PUBLIC_KEY, "algorithm": "ECDSA", "digest": "SHA1"},
        )

    now = [100.0]
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        keys = PublicKeyClient(
            Credentials("client", "secret"),
            http,
            clock=lambda: now[0],
        )
        first = keys.get_public_key("key/with space")
        second = keys.get_public_key("key/with space")
        assert first is second

        now[0] += 3601
        keys.get_public_key("key/with space")

    token_calls = [r for r in calls if r.url.path == "/identity/v1/oauth2/token"]
    key_calls = [r for r in calls if "/public_key/" in r.url.path]
    assert len(token_calls) == 1
    assert len(key_calls) == 2


def test_public_key_client_negative_caches_a_failed_key_lookup() -> None:
    key_calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal key_calls
        if request.url.path == "/identity/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "token", "expires_in": 7200})
        key_calls += 1
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        keys = PublicKeyClient(Credentials("client", "secret"), http)
        with pytest.raises(PublicKeyError):
            keys.get_public_key("unknown-key")
        with pytest.raises(PublicKeyError):
            keys.get_public_key("unknown-key")

    assert key_calls == 1


def test_public_key_client_bounds_unique_failed_key_lookups() -> None:
    key_calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal key_calls
        if request.url.path == "/identity/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "token", "expires_in": 7200})
        key_calls += 1
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        keys = PublicKeyClient(
            Credentials("client", "secret"),
            http,
            max_key_fetches_per_window=2,
        )
        for kid in ("unknown-one", "unknown-two", "unknown-three"):
            with pytest.raises(PublicKeyError):
                keys.get_public_key(kid)

    assert key_calls == 2


def test_public_key_client_does_not_queue_workers_behind_key_io() -> None:
    key_request_started = threading.Event()
    release_key_request = threading.Event()

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/identity/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "token", "expires_in": 7200})
        key_request_started.set()
        assert release_key_request.wait(timeout=5)
        return httpx.Response(
            200,
            json={"key": OFFICIAL_PUBLIC_KEY, "algorithm": "ECDSA", "digest": "SHA1"},
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        keys = PublicKeyClient(Credentials("client", "secret"), http)
        first_errors: list[Exception] = []
        second_errors: list[Exception] = []
        second_finished = threading.Event()

        def first_lookup() -> None:
            try:
                keys.get_public_key("first-key")
            except Exception as exc:
                first_errors.append(exc)

        def second_lookup() -> None:
            try:
                keys.get_public_key("second-key")
            except Exception as exc:
                second_errors.append(exc)
            finally:
                second_finished.set()

        first = threading.Thread(target=first_lookup)
        second = threading.Thread(target=second_lookup)
        first.start()
        assert key_request_started.wait(timeout=5)
        second.start()
        finished_without_waiting_for_network = second_finished.wait(timeout=0.5)
        release_key_request.set()
        first.join(timeout=5)
        second.join(timeout=5)

    assert finished_without_waiting_for_network
    assert first_errors == []
    assert len(second_errors) == 1
    assert isinstance(second_errors[0], PublicKeyError)


class _OfficialKeySource:
    def get_public_key(self, kid: str) -> ec.EllipticCurvePublicKey:
        assert kid == OFFICIAL_KID
        return load_public_key(OFFICIAL_PUBLIC_KEY)


def test_notification_verifier_decodes_header_fetches_key_and_verifies() -> None:
    verifier = NotificationVerifier(_OfficialKeySource())
    assert verifier.verify(OFFICIAL_PAYLOAD, OFFICIAL_SIGNATURE)
