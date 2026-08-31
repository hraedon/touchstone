"""Managing tracked searches.

The rule this module exists to keep: **a request never spends API budget.** "Scan
now" writes ``query.scan_requested_at`` and returns; the scanner honours it on its
next pass. A handler that called eBay directly would put a third party's latency and
a shared 5,000-a-day allowance behind a page load, and would let a browser refresh
spend quota.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Form, Request
from sqlalchemy.exc import IntegrityError

from touchstone.db.models import Query
from touchstone.ebay import exclusions
from touchstone.web import views
from touchstone.web.routes._common import DbSession, flash, not_found, redirect, render

router = APIRouter(prefix="/queries")

# A page of Browse results is one API call. Beyond this a single query can eat the
# whole daily allowance on its own, so the form refuses rather than warns.
MAX_PAGES_CEILING = 50
MIN_CADENCE_MINUTES = 5


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _validate(
    *,
    name: str,
    q: str,
    cadence_minutes: int,
    max_pages: int,
    min_seller_feedback: int,
    filter_expr: str = "",
) -> list[str]:
    problems: list[str] = []
    try:
        # A seller filter here would persist usernames: filter_expr is a stored
        # column passed to eBay untouched. The seller-column tests cannot see it,
        # because it arrives inside a general-purpose column.
        exclusions.reject_seller_filters(filter_expr)
    except exclusions.ExclusionListError as exc:
        problems.append(str(exc))
    if not name.strip():
        problems.append("Name is required.")
    if not q.strip():
        problems.append("Search keywords are required.")
    if cadence_minutes < MIN_CADENCE_MINUTES:
        problems.append(f"Cadence must be at least {MIN_CADENCE_MINUTES} minutes.")
    if not 1 <= max_pages <= MAX_PAGES_CEILING:
        problems.append(f"Pages per scan must be between 1 and {MAX_PAGES_CEILING}.")
    if min_seller_feedback < 0:
        problems.append("Seller-feedback floor cannot be negative.")
    return problems


@router.get("", name="query_list")
def query_list(request: Request, session: DbSession) -> Any:
    return render(
        request,
        "queries.html",
        rows=views.query_rows(session),
        budget=views.budget_view(session),
    )


@router.get("/new", name="query_new")
def query_new(request: Request) -> Any:
    return render(request, "query_form.html", query=None, problems=[], form={})


@router.get("/{query_id}/edit", name="query_edit")
def query_edit(
    query_id: int, request: Request, session: DbSession
) -> Any:
    query = session.get(Query, query_id)
    if query is None:
        raise not_found("query")
    return render(request, "query_form.html", query=query, problems=[], form={})


@router.post("", name="query_create")
def query_create(
    request: Request,
    session: DbSession,
    name: Annotated[str, Form()] = "",
    q: Annotated[str, Form()] = "",
    category_ids: Annotated[str, Form()] = "",
    filter_expr: Annotated[str, Form()] = "",
    cadence_minutes: Annotated[int, Form()] = 60,
    max_pages: Annotated[int, Form()] = 5,
    min_seller_feedback: Annotated[int, Form()] = 1,
    enabled: Annotated[str, Form()] = "",
) -> Any:
    problems = _validate(
        name=name,
        q=q,
        cadence_minutes=cadence_minutes,
        max_pages=max_pages,
        min_seller_feedback=min_seller_feedback,
        filter_expr=filter_expr,
    )
    if problems:
        return render(
            request,
            "query_form.html",
            query=None,
            problems=problems,
            form=dict(request.query_params) | {"name": name, "q": q},
            status_code=400,
        )

    query = Query(
        name=name.strip(),
        q=q.strip(),
        category_ids=_clean(category_ids),
        filter_expr=_clean(filter_expr),
        cadence_minutes=cadence_minutes,
        max_pages=max_pages,
        min_seller_feedback=min_seller_feedback,
        enabled=enabled == "on",
    )
    session.add(query)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        return render(
            request,
            "query_form.html",
            query=None,
            problems=[f"A query named {name.strip()!r} already exists."],
            form={"name": name, "q": q},
            status_code=400,
        )
    flash(request, f"Query {query.name!r} created.")
    return redirect(request.url_for("query_list").path)


@router.post("/{query_id}", name="query_update")
def query_update(
    query_id: int,
    request: Request,
    session: DbSession,
    name: Annotated[str, Form()] = "",
    q: Annotated[str, Form()] = "",
    category_ids: Annotated[str, Form()] = "",
    filter_expr: Annotated[str, Form()] = "",
    cadence_minutes: Annotated[int, Form()] = 60,
    max_pages: Annotated[int, Form()] = 5,
    min_seller_feedback: Annotated[int, Form()] = 1,
    enabled: Annotated[str, Form()] = "",
) -> Any:
    query = session.get(Query, query_id)
    if query is None:
        raise not_found("query")

    problems = _validate(
        name=name,
        q=q,
        cadence_minutes=cadence_minutes,
        max_pages=max_pages,
        min_seller_feedback=min_seller_feedback,
        filter_expr=filter_expr,
    )
    if problems:
        return render(
            request, "query_form.html", query=query, problems=problems, form={}, status_code=400
        )

    floor_changed = query.min_seller_feedback != min_seller_feedback
    query.name = name.strip()
    query.q = q.strip()
    query.category_ids = _clean(category_ids)
    query.filter_expr = _clean(filter_expr)
    query.cadence_minutes = cadence_minutes
    query.max_pages = max_pages
    query.min_seller_feedback = min_seller_feedback
    query.enabled = enabled == "on"
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        return render(
            request,
            "query_form.html",
            query=query,
            problems=[f"A query named {name.strip()!r} already exists."],
            form={},
            status_code=400,
        )

    if floor_changed:
        # Say it out loud at the moment it is done. The trend page will draw the
        # boundary afterwards, but the person changing the floor is the one who
        # most needs to know the series either side is not comparable.
        flash(
            request,
            "Seller-feedback floor changed. Scans from here on sample a different "
            "set of sellers, and the trend will show a break rather than a move.",
            "warn",
        )
    else:
        flash(request, f"Query {query.name!r} saved.")
    return redirect(request.url_for("query_list").path)


@router.post("/{query_id}/enabled", name="query_toggle")
def query_toggle(
    query_id: int, request: Request, session: DbSession
) -> Any:
    query = session.get(Query, query_id)
    if query is None:
        raise not_found("query")
    query.enabled = not query.enabled
    session.commit()
    flash(request, f"Query {query.name!r} {'enabled' if query.enabled else 'disabled'}.")
    return redirect(request.url_for("query_list").path)


@router.post("/{query_id}/scan-request", name="query_scan_request")
def query_scan_request(
    query_id: int, request: Request, session: DbSession
) -> Any:
    """Ask for an out-of-cadence scan. Records the request; does not run one."""
    query = session.get(Query, query_id)
    if query is None:
        raise not_found("query")
    query.scan_requested_at = datetime.now(UTC)
    session.commit()
    flash(
        request,
        "Scan requested. The scanner picks it up on its next pass — nothing is "
        "called from this page.",
    )
    return redirect(request.url_for("query_list").path)


@router.post("/{query_id}/scan-request/cancel", name="query_scan_request_cancel")
def query_scan_request_cancel(
    query_id: int, request: Request, session: DbSession
) -> Any:
    query = session.get(Query, query_id)
    if query is None:
        raise not_found("query")
    query.scan_requested_at = None
    session.commit()
    flash(request, "Scan request withdrawn.")
    return redirect(request.url_for("query_list").path)
