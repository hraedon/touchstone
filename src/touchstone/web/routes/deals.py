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
    per_gb = view.per_gb
    if per_gb is None or view.cohort_median_per_gb is None:
        return None
    return charts.price_band(
        marked=per_gb,
        p10=view.cohort_p10,
        median=view.cohort_median_per_gb,
        minimum=view.cohort_min_per_gb,
        n=view.deal.cohort_n,
    )


@router.get("", name="deal_feed")
def deal_feed(request: Request, session: DbSession, dismissed: int = 0) -> Any:
    include = bool(dismissed)
    feed = views.deal_feed(session, include_dismissed=include)
    return render(
        request,
        "deals.html",
        deals=[{"view": view, "band": _band(view)} for view in feed],
        include_dismissed=include,
    )


@router.post("/{deal_id}/dismiss", name="deal_dismiss")
def deal_dismiss(deal_id: int, request: Request, session: DbSession) -> Any:
    deal = session.get(Deal, deal_id)
    if deal is None:
        raise not_found("deal")
    deal.dismissed_at = datetime.now(UTC)
    session.commit()
    flash(request, "Deal dismissed.")
    return redirect(request.url_for("deal_feed").path)


@router.post("/{deal_id}/restore", name="deal_restore")
def deal_restore(deal_id: int, request: Request, session: DbSession) -> Any:
    deal = session.get(Deal, deal_id)
    if deal is None:
        raise not_found("deal")
    deal.dismissed_at = None
    session.commit()
    flash(request, "Deal restored.")
    return redirect(request.url_for("deal_feed").path + "?dismissed=1")
