"""Public FastAPI service for eBay account-deletion notifications."""

from __future__ import annotations

import json
import os
import re
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any, cast

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from touchstone.db.session import make_engine, make_session_factory
from touchstone.ebay.client import Credentials
from touchstone.sink.crypto import NotificationVerifier, PublicKeyClient, challenge_response
from touchstone.sink.purge import handle_deletion, notification_id_of

_TOKEN = re.compile(r"[A-Za-z0-9_-]{32,80}\Z")
_DELETION_TOPIC = "MARKETPLACE_ACCOUNT_DELETION"
PRODUCTION_ENDPOINT_URL = "https://ebdel.hraedon.com/"
MAX_NOTIFICATION_BYTES = 64 * 1024

SessionProvider = Callable[[], AbstractContextManager[Session]]


async def _bounded_json_object(request: Request) -> dict[str, Any]:
    raw_length = request.headers.get("content-length")
    if raw_length is not None:
        try:
            content_length = int(raw_length)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid Content-Length") from exc
        if content_length < 0:
            raise HTTPException(status_code=400, detail="invalid Content-Length")
        if content_length > MAX_NOTIFICATION_BYTES:
            raise HTTPException(status_code=413, detail="notification body is too large")

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_NOTIFICATION_BYTES:
            raise HTTPException(status_code=413, detail="notification body is too large")

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="notification body is not JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="notification body must be an object")
    return cast(dict[str, Any], payload)


def _is_deletion_topic(payload: dict[str, Any]) -> bool:
    metadata = payload.get("metadata")
    return isinstance(metadata, dict) and metadata.get("topic") == _DELETION_TOPIC


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value:
        raise RuntimeError(f"{name} is required")
    if value != value.strip():
        raise RuntimeError(f"{name} must not contain surrounding whitespace")
    return value


@dataclass(frozen=True)
class SinkSettings:
    dsn: str
    verification_token: str
    endpoint_url: str
    ebay_credentials: Credentials

    def __post_init__(self) -> None:
        if not self.dsn:
            raise ValueError("TOUCHSTONE_DSN is required")
        if _TOKEN.fullmatch(self.verification_token) is None:
            raise ValueError(
                "VERIFICATION_TOKEN must be 32-80 alphanumeric, underscore, or hyphen characters"
            )
        try:
            endpoint = httpx.URL(self.endpoint_url)
        except (TypeError, ValueError) as exc:
            raise ValueError("ENDPOINT_URL must be a valid HTTPS URL") from exc
        if endpoint.scheme != "https" or not endpoint.host:
            raise ValueError("ENDPOINT_URL must be a valid HTTPS URL")
        if endpoint.query or endpoint.fragment:
            raise ValueError("ENDPOINT_URL must not contain a query or fragment")
        if self.endpoint_url != PRODUCTION_ENDPOINT_URL:
            raise ValueError(f"ENDPOINT_URL must be exactly {PRODUCTION_ENDPOINT_URL}")
        if not self.ebay_credentials.client_id or not self.ebay_credentials.client_secret:
            raise ValueError("EBAY_CLIENT_ID and EBAY_CLIENT_SECRET are required")

    @classmethod
    def from_env(cls) -> SinkSettings:
        return cls(
            dsn=_required_env("TOUCHSTONE_DSN"),
            verification_token=_required_env("VERIFICATION_TOKEN"),
            endpoint_url=_required_env("ENDPOINT_URL"),
            ebay_credentials=Credentials(
                _required_env("EBAY_CLIENT_ID"),
                _required_env("EBAY_CLIENT_SECRET"),
            ),
        )


@dataclass(frozen=True)
class _Runtime:
    settings: SinkSettings
    verifier: NotificationVerifier
    sessions: SessionProvider


def create_app(
    *,
    settings: SinkSettings | None = None,
    verifier: NotificationVerifier | None = None,
    session_provider: SessionProvider | None = None,
) -> FastAPI:
    """Create the sink, allowing deterministic dependencies in endpoint tests."""
    runtime: _Runtime | None = None
    owned_http: httpx.Client | None = None
    owned_engine: Engine | None = None

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        nonlocal owned_engine, owned_http, runtime

        resolved_settings = settings or SinkSettings.from_env()
        resolved_verifier = verifier
        if resolved_verifier is None:
            owned_http = httpx.Client(timeout=30.0)
            resolved_verifier = NotificationVerifier(
                PublicKeyClient(resolved_settings.ebay_credentials, owned_http)
            )

        resolved_sessions = session_provider
        if resolved_sessions is None:
            owned_engine = make_engine(resolved_settings.dsn)
            resolved_sessions = make_session_factory(owned_engine)

        runtime = _Runtime(resolved_settings, resolved_verifier, resolved_sessions)
        try:
            yield
        finally:
            runtime = None
            if owned_http is not None:
                owned_http.close()
            if owned_engine is not None:
                owned_engine.dispose()

    service = FastAPI(
        title="touchstone deletion sink",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    def current_runtime() -> _Runtime:
        if runtime is None:
            raise RuntimeError("sink startup has not completed")
        return runtime

    @service.get("/")
    def validate_endpoint(challenge_code: str = Query(min_length=1)) -> dict[str, str]:
        configured = current_runtime().settings
        return {
            "challengeResponse": challenge_response(
                challenge_code,
                configured.verification_token,
                configured.endpoint_url,
            )
        }

    @service.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @service.post("/", status_code=200, response_class=Response)
    def accept_notification(
        request: Request,
        payload: Annotated[dict[str, Any], Depends(_bounded_json_object)],
    ) -> Response:
        configured = current_runtime()
        signature = request.headers.get("X-EBAY-SIGNATURE")
        if (
            not _is_deletion_topic(payload)
            or signature is None
            or not configured.verifier.verify(payload, signature)
        ):
            raise HTTPException(status_code=412, detail="signature verification failed")
        try:
            notification_id = notification_id_of(payload)
        except ValueError as exc:
            raise HTTPException(status_code=412, detail="invalid notification payload") from exc

        # Commit before acknowledging. A database failure deliberately becomes a 5xx
        # so eBay redelivers instead of accepting a receipt that was never recorded.
        with configured.sessions() as session:
            handle_deletion(session, notification_id)
            session.commit()
        return Response(status_code=200)

    return service


app = create_app()
