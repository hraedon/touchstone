"""The deal feed.

A "deal" is a listing whose $/GB sits a full spread-unit below the tenth percentile
of *asking* prices in its own cohort. It is not a claim about market value, and the
page says so: the cohort context, the count it was computed over, and the capacity
parse that produced the $/GB are all shown beside it, because a deal is only ever as
trustworthy as the parse underneath it — misreading "Lot of 4 x 32GB" as a single
32GB module manufactures a fourfold bargain out of nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request

from touchstone.db.models import Deal
from touchstone.web import charts, views
from touchstone.web.routes._common import DbSession, flash, not_found, redirect, render

router = APIRouter(prefix="/deals")


def _band(view: views.DealView) -> charts.PriceBand | None:
    """The signature element, when there is enough recorded context to draw it."""
    per_unit = view.per_unit
    if per_unit is None or view.cohort_median_per_unit is None:
        return None
    return charts.price_band(
        marked=per_unit,
        p10=view.cohort_p10,
        median=view.cohort_median_per_unit,
        minimum=view.cohort_min_per_unit,
        n=view.deal.cohort_n,
        unit=charts.unit_suffix(view.deal.unit),
    )


FEED_LIMIT = 100


def _feed_path(request: Request, *, query: int | None, dismissed: bool = False) -> str:
    """The feed URL with the operator's filter kept, so an action does not reset it."""
    params = [f"query={query}"] if query is not None else []
    if dismissed:
        params.append("dismissed=1")
    path = request.url_for("deal_feed").path
    return f"{path}?{'&'.join(params)}" if params else path


@router.get("", name="deal_feed")
def deal_feed(
    request: Request, session: DbSession, dismissed: int = 0, query: int | None = None
) -> Any:
    include = bool(dismissed)
    # The facets count the whole feed, not the filtered slice, so the filter bar
    # always shows where every flag lives.
    everything = views.deal_feed(session, include_dismissed=include, limit=None)
    facets = views.deal_facets(everything)
    shown = [
        view
        for view in everything
        if query is None or (view.query is not None and view.query.id == query)
    ]
    return render(
        request,
        "deals.html",
        deals=[{"view": view, "band": _band(view)} for view in shown[:FEED_LIMIT]],
        include_dismissed=include,
        facets=facets,
        selected_query=query,
        total=len(everything),
        shown_total=len(shown),
        feed_limit=FEED_LIMIT,
    )


@router.post("/{deal_id}/dismiss", name="deal_dismiss")
def deal_dismiss(
    deal_id: int, request: Request, session: DbSession, query: int | None = None
) -> Any:
    deal = session.get(Deal, deal_id)
    if deal is None:
        raise not_found("deal")
    deal.dismissed_at = datetime.now(UTC)
    session.commit()
    flash(request, "Deal dismissed.")
    return redirect(_feed_path(request, query=query))


@router.post("/{deal_id}/restore", name="deal_restore")
def deal_restore(
    deal_id: int, request: Request, session: DbSession, query: int | None = None
) -> Any:
    deal = session.get(Deal, deal_id)
    if deal is None:
        raise not_found("deal")
    deal.dismissed_at = None
    session.commit()
    flash(request, "Deal restored.")
    return redirect(_feed_path(request, query=query, dismissed=True))


@router.post("/{deal_id}/verify", name="deal_verify")
def deal_verify(
    deal_id: int, request: Request, session: DbSession, query: int | None = None
) -> Any:
    """Queue a ground-truth check of this deal's listing.

    This handler only records the request: the web tier holds no eBay
    credentials and must never spend budget, so the scanner answers it on its
    next pass — the same shape as a query's "scan now". Re-clicking while a
    request is pending does not jump the queue.
    """
    deal = session.get(Deal, deal_id)
    if deal is None:
        raise not_found("deal")
    if deal.verify_requested_at is None:
        deal.verify_requested_at = datetime.now(UTC)
        session.commit()
        flash(request, "Verification queued — the scanner will answer it on its next pass.")
    else:
        flash(request, "A verification of this deal is already queued.")
    return redirect(_feed_path(request, query=query))
