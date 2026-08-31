"""Shared plumbing for the route modules: sessions, rendering, and flash messages."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated, Any, cast

from fastapi import Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, sessionmaker
from starlette.status import HTTP_303_SEE_OTHER

_FLASH_KEY = "touchstone_flash"
MAX_FLASHES = 5


def get_session(request: Request) -> Iterator[Session]:
    factory = cast("sessionmaker[Session]", request.app.state.session_factory)
    session = factory()
    try:
        yield session
    finally:
        session.close()


# Annotated form so the dependency is not a call in a default argument.
DbSession = Annotated[Session, Depends(get_session)]


def templates(request: Request) -> Jinja2Templates:
    return cast(Jinja2Templates, request.app.state.templates)


def flash(request: Request, message: str, level: str = "ok") -> None:
    """Queue a one-shot message for the next rendered page."""
    queued = request.session.get(_FLASH_KEY)
    messages = list(queued) if isinstance(queued, list) else []
    messages.append({"message": message, "level": level})
    request.session[_FLASH_KEY] = messages[-MAX_FLASHES:]


def _drain_flashes(request: Request) -> list[dict[str, str]]:
    queued = request.session.pop(_FLASH_KEY, None)
    if not isinstance(queued, list):
        return []
    return [entry for entry in queued if isinstance(entry, dict)]


def render(
    request: Request, template: str, /, *, status_code: int = 200, **context: Any
) -> Any:
    return templates(request).TemplateResponse(
        request,
        template,
        {"flashes": _drain_flashes(request), **context},
        status_code=status_code,
    )


def redirect(url: str) -> RedirectResponse:
    """POST-redirect-GET, so a refresh never repeats a mutation."""
    return RedirectResponse(url, status_code=HTTP_303_SEE_OTHER)


def not_found(what: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"no such {what}")
