"""eBay Marketplace Account Deletion notification cryptography.

eBay signs a canonical re-serialization of the notification, not the request bytes.
The canonical form is the compact JSON produced by the field order of eBay's Go SDK
structs, including Go's HTML escaping.  Changing that shape is a wire-protocol change.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from touchstone.ebay.client import PRODUCTION, Credentials, EbayError, TokenCache

PUBLIC_KEY_PATH = "/commerce/notification/v1/public_key"
PUBLIC_KEY_CACHE_SECONDS = 3600.0
MAX_CACHED_KEYS = 100
FAILED_KEY_CACHE_SECONDS = 300.0
MAX_CACHED_FAILED_KEYS = 100
KEY_FETCH_WINDOW_SECONDS = 300.0
MAX_KEY_FETCHES_PER_WINDOW = 10

_PEM_BEGIN = "-----BEGIN PUBLIC KEY-----"
_PEM_END = "-----END PUBLIC KEY-----"

log = logging.getLogger("touchstone.sink.crypto")


class SignatureFormatError(ValueError):
    """A notification or its signature header cannot have been emitted by eBay."""


class PublicKeyError(RuntimeError):
    """The public key required to verify a notification could not be obtained."""


@dataclass(frozen=True)
class SignatureHeader:
    kid: str
    signature: str


def challenge_response(
    challenge_code: str,
    verification_token: str,
    endpoint_url: str,
) -> str:
    """Return SHA-256(challenge + token + endpoint) as lowercase hexadecimal."""
    digest = hashes.Hash(hashes.SHA256())
    digest.update(challenge_code.encode("utf-8"))
    digest.update(verification_token.encode("utf-8"))
    digest.update(endpoint_url.encode("utf-8"))
    return digest.finalize().hex()


def go_json_marshal(value: object) -> bytes:
    """Serialize the subset of JSON used here exactly as Go's ``json.Marshal``.

    Go leaves ordinary Unicode intact but escapes HTML-significant characters and
    the two JavaScript line separators. Python's JSON encoder needs those last steps
    applied explicitly.
    """
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise SignatureFormatError("notification is not valid JSON") from exc

    rendered = (
        rendered.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )
    try:
        return rendered.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise SignatureFormatError("notification contains invalid Unicode") from exc


def _object(value: object) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise SignatureFormatError("notification object has the wrong type")
    return value


def _go_string(node: Mapping[str, Any], field: str) -> str:
    value = node.get(field)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise SignatureFormatError(f"{field} has the wrong type")
    return value


def _go_bool(node: Mapping[str, Any], field: str) -> bool:
    value = node.get(field)
    if value is None:
        return False
    if not isinstance(value, bool):
        raise SignatureFormatError(f"{field} has the wrong type")
    return value


def _go_int(node: Mapping[str, Any], field: str) -> int:
    value = node.get(field)
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int):
        raise SignatureFormatError(f"{field} has the wrong type")
    return value


def canonical_notification(payload: Mapping[str, Any]) -> bytes:
    """Rebuild the exact struct serialized by eBay's official Go SDK.

    Unknown fields and incoming object order are deliberately ignored. Missing or
    null fields take Go's zero value because that is what unmarshalling into the SDK
    structs does before it marshals the message for signature verification.
    """
    metadata = _object(payload.get("metadata"))
    notification = _object(payload.get("notification"))
    data = _object(notification.get("data"))

    ordered = {
        "metadata": {
            "topic": _go_string(metadata, "topic"),
            "schemaVersion": _go_string(metadata, "schemaVersion"),
            "deprecated": _go_bool(metadata, "deprecated"),
        },
        "notification": {
            "notificationId": _go_string(notification, "notificationId"),
            "eventDate": _go_string(notification, "eventDate"),
            "publishDate": _go_string(notification, "publishDate"),
            "publishAttemptCount": _go_int(notification, "publishAttemptCount"),
            "data": {
                "username": _go_string(data, "username"),
                "userId": _go_string(data, "userId"),
                "eiasToken": _go_string(data, "eiasToken"),
            },
        },
    }
    return go_json_marshal(ordered)


def decode_signature_header(raw: str) -> SignatureHeader:
    """Decode the base64 JSON value carried by ``X-EBAY-SIGNATURE``."""
    try:
        decoded = base64.b64decode(raw, validate=True)
        body = json.loads(decoded)
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SignatureFormatError("invalid X-EBAY-SIGNATURE header") from exc

    if not isinstance(body, dict):
        raise SignatureFormatError("invalid X-EBAY-SIGNATURE object")
    kid = body.get("kid")
    signature = body.get("signature")
    if not isinstance(kid, str) or not kid.strip():
        raise SignatureFormatError("signature header contains no key id")
    if not isinstance(signature, str) or not signature.strip():
        raise SignatureFormatError("signature header contains no signature")
    return SignatureHeader(kid=kid, signature=signature)


def _normalize_public_key_pem(value: str | bytes) -> bytes:
    try:
        text = value.decode("ascii") if isinstance(value, bytes) else value
    except UnicodeDecodeError as exc:
        raise PublicKeyError("public key is not ASCII PEM") from exc

    # Some eBay responses have carried literal ``\n`` and others an entirely inline
    # PEM. Strip all body whitespace and wrap it ourselves so cryptography sees the
    # same valid SubjectPublicKeyInfo in either case.
    text = text.replace("\\n", "\n")
    begin = text.find(_PEM_BEGIN)
    end = text.find(_PEM_END, begin + len(_PEM_BEGIN))
    if begin < 0 or end < 0:
        raise PublicKeyError("public key response is not a PEM public key")

    compact = "".join(text[begin + len(_PEM_BEGIN) : end].split())
    try:
        base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise PublicKeyError("public key PEM body is invalid base64") from exc
    if not compact:
        raise PublicKeyError("public key PEM body is empty")

    lines = [compact[offset : offset + 64] for offset in range(0, len(compact), 64)]
    return f"{_PEM_BEGIN}\n{'\n'.join(lines)}\n{_PEM_END}\n".encode("ascii")


def load_public_key(value: str | bytes) -> ec.EllipticCurvePublicKey:
    """Load and constrain an eBay key to the required ECDSA P-256 type."""
    try:
        key = serialization.load_pem_public_key(_normalize_public_key_pem(value))
    except (TypeError, ValueError) as exc:
        raise PublicKeyError("public key could not be parsed") from exc
    if not isinstance(key, ec.EllipticCurvePublicKey):
        raise PublicKeyError("public key is not an elliptic-curve key")
    if not isinstance(key.curve, ec.SECP256R1):
        raise PublicKeyError("public key is not P-256")
    return key


def verify_notification_signature(
    public_key: ec.EllipticCurvePublicKey,
    signature_b64: str,
    canonical_payload: bytes,
) -> bool:
    """Verify eBay's DER ECDSA P-256 signature over SHA-1 of the payload."""
    try:
        signature = base64.b64decode(signature_b64, validate=True)
        public_key.verify(signature, canonical_payload, ec.ECDSA(hashes.SHA1()))
    except (binascii.Error, InvalidSignature, ValueError):
        return False
    return True


class PublicKeySource(Protocol):
    def get_public_key(self, kid: str) -> ec.EllipticCurvePublicKey: ...


@dataclass(frozen=True)
class _CachedKey:
    expires_at: float
    key: ec.EllipticCurvePublicKey


class PublicKeyClient:
    """Fetch eBay notification keys with OAuth and a bounded one-hour cache."""

    def __init__(
        self,
        credentials: Credentials,
        http: httpx.Client,
        *,
        base_url: str = PRODUCTION,
        cache_ttl_seconds: float = PUBLIC_KEY_CACHE_SECONDS,
        failed_cache_ttl_seconds: float = FAILED_KEY_CACHE_SECONDS,
        key_fetch_window_seconds: float = KEY_FETCH_WINDOW_SECONDS,
        max_key_fetches_per_window: int = MAX_KEY_FETCHES_PER_WINDOW,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if cache_ttl_seconds <= 0:
            raise ValueError("public-key cache TTL must be positive")
        if failed_cache_ttl_seconds <= 0:
            raise ValueError("failed-key cache TTL must be positive")
        if key_fetch_window_seconds <= 0:
            raise ValueError("key-fetch window must be positive")
        if max_key_fetches_per_window <= 0:
            raise ValueError("key-fetch limit must be positive")
        self._http = http
        self._base_url = base_url.rstrip("/")
        self._tokens = TokenCache(credentials, http, self._base_url)
        self._cache_ttl_seconds = cache_ttl_seconds
        self._failed_cache_ttl_seconds = failed_cache_ttl_seconds
        self._key_fetch_window_seconds = key_fetch_window_seconds
        self._max_key_fetches_per_window = max_key_fetches_per_window
        self._clock = clock
        self._cache: OrderedDict[str, _CachedKey] = OrderedDict()
        self._failed_cache: OrderedDict[str, float] = OrderedDict()
        self._fetch_times: deque[float] = deque()
        self._cache_lock = threading.Lock()
        # Never queue FastAPI's finite worker pool behind slow eBay I/O. A delivery
        # rejected while another key is fetched will be retried by eBay.
        self._fetch_lock = threading.Lock()

    def get_public_key(self, kid: str) -> ec.EllipticCurvePublicKey:
        if not kid or len(kid) > 500:
            raise PublicKeyError("public key id is invalid")

        cached = self._cached(kid)
        if cached is not None:
            return cached
        if not self._fetch_lock.acquire(blocking=False):
            raise PublicKeyError("another public-key request is already in progress")

        try:
            # A fetch could have completed between the first cache check and taking
            # the single-flight lock.
            cached = self._cached(kid)
            if cached is not None:
                return cached

            now = self._clock()
            with self._cache_lock:
                cutoff = now - self._key_fetch_window_seconds
                while self._fetch_times and self._fetch_times[0] <= cutoff:
                    self._fetch_times.popleft()
                if len(self._fetch_times) >= self._max_key_fetches_per_window:
                    raise PublicKeyError("public-key request budget is temporarily exhausted")
                self._fetch_times.append(now)

            try:
                key = self._fetch(kid)
            except PublicKeyError as exc:
                self._remember_failed(kid, now)
                # This is deliberately free of the key id, payload, and credentials.
                log.warning("eBay notification public-key fetch failed: %s", exc)
                raise

            with self._cache_lock:
                while len(self._cache) >= MAX_CACHED_KEYS:
                    self._cache.popitem(last=False)
                self._cache[kid] = _CachedKey(now + self._cache_ttl_seconds, key)
            return key
        finally:
            self._fetch_lock.release()

    def _cached(self, kid: str) -> ec.EllipticCurvePublicKey | None:
        now = self._clock()
        with self._cache_lock:
            cached = self._cache.get(kid)
            if cached is not None:
                if now < cached.expires_at:
                    self._cache.move_to_end(kid)
                    return cached.key
                del self._cache[kid]

            failed_until = self._failed_cache.get(kid)
            if failed_until is not None:
                if now < failed_until:
                    self._failed_cache.move_to_end(kid)
                    raise PublicKeyError("public key recently failed to load")
                del self._failed_cache[kid]
        return None

    def _remember_failed(self, kid: str, now: float) -> None:
        with self._cache_lock:
            while len(self._failed_cache) >= MAX_CACHED_FAILED_KEYS:
                self._failed_cache.popitem(last=False)
            self._failed_cache[kid] = now + self._failed_cache_ttl_seconds

    def _fetch(self, kid: str) -> ec.EllipticCurvePublicKey:
        try:
            token = self._tokens.token()
            encoded_kid = quote(kid, safe="")
            response = self._http.get(
                f"{self._base_url}{PUBLIC_KEY_PATH}/{encoded_kid}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                },
            )
        except (
            EbayError,
            httpx.HTTPError,
            json.JSONDecodeError,
            UnicodeDecodeError,
            TypeError,
            ValueError,
        ) as exc:
            raise PublicKeyError("eBay public-key request failed") from exc
        if response.status_code != 200:
            raise PublicKeyError(
                f"eBay public-key request returned HTTP {response.status_code}"
            )
        try:
            body = response.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise PublicKeyError("eBay public-key response was not JSON") from exc
        if not isinstance(body, dict):
            raise PublicKeyError("eBay public-key response had the wrong shape")

        algorithm = body.get("algorithm")
        digest = body.get("digest")
        if not isinstance(algorithm, str) or algorithm.upper() != "ECDSA":
            raise PublicKeyError("eBay public-key response did not specify ECDSA")
        if not isinstance(digest, str) or digest.replace("-", "").upper() != "SHA1":
            raise PublicKeyError("eBay public-key response did not specify SHA-1")
        raw_key = body.get("key")
        if not isinstance(raw_key, str) or not raw_key:
            raise PublicKeyError("eBay public-key response contained no key")
        return load_public_key(raw_key)


@dataclass(frozen=True)
class NotificationVerifier:
    public_keys: PublicKeySource

    def verify(self, payload: Mapping[str, Any], signature_header: str) -> bool:
        try:
            decoded = decode_signature_header(signature_header)
            canonical = canonical_notification(payload)
            public_key = self.public_keys.get_public_key(decoded.kid)
        except (PublicKeyError, SignatureFormatError):
            return False
        return verify_notification_signature(public_key, decoded.signature, canonical)
