"""FastAPI application factory for the touchstone UI.

The web face is an alternative front end over the same calls the CLI makes, never a
privileged one. Two constraints shape this module:

* **A request must never spend API budget or block on eBay.** No handler constructs
  an ``EbayClient``; "scan now" records a request on the query and the scanner
  CronJob honours it on its next pass. An architecture test enforces the absence of
  the import, because the rule is easy to break with one convenient line.
* **The core never imports the web layer.** Also enforced by test. This module may
  reach downward freely; nothing below may reach back up.

There is no authentication. The UI is internal-only behind ``traefik-internal``;
half-building a login here would be worse than deciding it properly with the ingress
in Plan 004.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, sessionmaker
from starlette.middleware.sessions import SessionMiddleware

from touchstone.db.session import make_engine, make_session_factory
from touchstone.scan.aggregate import MIN_COHORT_N
from touchstone.scan.deals import MIN_SCORE
from touchstone.web.routes import router

WEB_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"

MAX_FORM_BYTES = 64 * 1024


@dataclass(frozen=True)
class WebSettings:
    dsn: str
    secret_key: str
    session_cookie_secure: bool = True

    def __post_init__(self) -> None:
        if not self.dsn:
            raise ValueError("TOUCHSTONE_DSN is required")
        if len(self.secret_key) < 32:
            raise ValueError("TOUCHSTONE_SECRET_KEY must be at least 32 characters")

    @classmethod
    def from_env(cls) -> WebSettings:
        dsn = os.environ.get("TOUCHSTONE_DSN", "").strip()
        if not dsn:
            raise RuntimeError("TOUCHSTONE_DSN is required")
        secret = os.environ.get("TOUCHSTONE_SECRET_KEY", "").strip()
        if not secret:
            # Deliberately fatal rather than generated. A per-process random key
            # would work perfectly in a one-replica test and silently log everyone
            # out on every restart and every rollout in production.
            raise RuntimeError(
                "TOUCHSTONE_SECRET_KEY is required (32+ chars; `openssl rand -hex 32`)"
            )
        return cls(
            dsn=dsn,
            secret_key=secret,
            session_cookie_secure=os.environ.get("TOUCHSTONE_INSECURE_COOKIES", "") != "1",
        )


def _money(value: float | None, places: int = 2) -> str:
    """Format a figure, or the honest dash when there is not one.

    An absent number renders as an em dash, never as 0.00. Missing shipping is
    unknown rather than free, and a zero would understate every total it entered.
    """
    if value is None:
        return "—"
    return f"{value:,.{places}f}"


def _per_gb(value: float | None) -> str:
    return "—" if value is None else f"{value:,.3f}"


def _stamp(value: datetime | None) -> str:
    if value is None:
        return "never"
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _since(value: datetime | None) -> str:
    if value is None:
        return "never"
    delta = datetime.now(UTC) - value.astimezone(UTC)
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _yes_no(value: bool | None) -> str:
    if value is None:
        return "?"
    return "yes" if value else "no"


def build_templates() -> Jinja2Templates:
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["money"] = _money
    templates.env.filters["per_gb"] = _per_gb
    templates.env.filters["stamp"] = _stamp
    templates.env.filters["since"] = _since
    templates.env.filters["yes_no"] = _yes_no
    templates.env.globals["MIN_COHORT_N"] = MIN_COHORT_N
    templates.env.globals["MIN_SCORE"] = MIN_SCORE
    return templates


def create_app(
    *,
    settings: WebSettings | None = None,
    session_factory: sessionmaker[Session] | None = None,
) -> FastAPI:
    """Build the app. Tests pass a session factory bound to the test database."""
    resolved = settings or WebSettings.from_env()
    if session_factory is None:
        session_factory = make_session_factory(make_engine(resolved.dsn))

    app = FastAPI(
        title="touchstone",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=resolved.secret_key,
        https_only=resolved.session_cookie_secure,
        same_site="strict",
    )
    app.state.settings = resolved
    app.state.session_factory = session_factory
    app.state.templates = build_templates()
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.middleware("http")
    async def _security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        # Strict, and self-only: the theme toggle is an external script for exactly
        # this reason. There is no CDN anywhere in the family.
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; "
            "form-action 'self'"
        )
        return response

    @app.middleware("http")
    async def _body_size_limit(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.method == "POST":
            raw = request.headers.get("content-length")
            if raw is not None and raw.isdigit() and int(raw) > MAX_FORM_BYTES:
                return Response(status_code=413, content="form body is too large")
        return await call_next(request)

    app.include_router(router)
    return app


def new_secret_key() -> str:
    """Convenience for generating a deployment secret; not called at runtime."""
    return secrets.token_hex(32)
