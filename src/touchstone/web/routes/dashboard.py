"""The landing page: what is being tracked, what it cost, and what needs attention."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from touchstone.web import views
from touchstone.web.routes._common import DbSession, render

router = APIRouter()


@router.get("/", name="dashboard")
def dashboard(request: Request, session: DbSession) -> Any:
    return render(request, "dashboard.html", overview=views.overview(session))
