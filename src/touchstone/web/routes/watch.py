"""The watchlist: one listing, pinned, with its full observation history.

This is the one place where per-listing prices are drawn rather than cohort
statistics — and it is honest about it, because a single listing's price over time
is an observation, not a statistic. Missing shipping stays missing: an unknown
delivered cost renders as a dash, never as the item price, and never as zero.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Form, Request
from sqlalchemy import select

from touchstone.db.models import Listing, Watch
from touchstone.web import charts, views
from touchstone.web.routes._common import DbSession, flash, not_found, redirect, render

router = APIRouter(prefix="/watch")


def _price_plot(view: views.WatchView) -> charts.Plot:
    return charts.line_plot(
        [
            charts.LineSpec(
                label="asking price",
                role="median",
                samples=tuple(
                    charts.Sample(at=point.observed_at, value=point.price)
                    for point in view.observations
                ),
            ),
            charts.LineSpec(
                label="delivered total",
                role="p10",
                samples=tuple(
                    charts.Sample(at=point.observed_at, value=point.total_cost)
                    for point in view.observations
                ),
            ),
        ],
        height=200,
    )


@router.get("", name="watch_list")
def watch_list(request: Request, session: DbSession) -> Any:
    return render(request, "watch.html", watches=views.watch_list(session))


@router.post("", name="watch_add")
def watch_add(
    request: Request,
    session: DbSession,
    listing_id: Annotated[str, Form()] = "",
    note: Annotated[str, Form()] = "",
) -> Any:
    item_id = listing_id.strip()
    listing = session.get(Listing, item_id) if item_id else None
    if listing is None:
        flash(request, "No listing with that id has been observed.", "warn")
        return redirect(request.url_for("watch_list").path)

    existing = session.scalars(select(Watch).where(Watch.listing_id == item_id)).first()
    if existing is None:
        session.add(Watch(listing_id=item_id, note=note.strip() or None))
        session.commit()
        flash(request, "Listing pinned.")
    else:
        existing.note = note.strip() or existing.note
        session.commit()
        flash(request, "Already pinned; note updated.")
    return redirect(request.url_for("watch_list").path)


@router.get("/{listing_id}", name="watch_detail")
def watch_detail(listing_id: str, request: Request, session: DbSession) -> Any:
    view = views.watch_detail(session, listing_id)
    if view is None:
        raise not_found("watched listing")
    return render(request, "watch_detail.html", view=view, plot=_price_plot(view))


@router.post("/{listing_id}/unpin", name="watch_remove")
def watch_remove(listing_id: str, request: Request, session: DbSession) -> Any:
    watch = session.scalars(select(Watch).where(Watch.listing_id == listing_id)).first()
    if watch is None:
        raise not_found("watched listing")
    session.delete(watch)
    session.commit()
    flash(request, "Listing unpinned.")
    return redirect(request.url_for("watch_list").path)
