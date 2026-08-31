"""Trends: what the tracked cohorts have been *asking*, over time.

Every honesty constraint in the plan lands on this page, so they are stated once
here and implemented in ``views`` and ``charts`` rather than in the template:

1. The series is an **asking-price index**, never a market price, value, or worth.
   Active listings are all that the Browse API exposes, and that pool is biased
   upward: correctly-priced stock sells and leaves it, overpriced stock does not and
   accumulates in it.
2. The **disappearance series is separate** — its own panel, its own axis, its own
   chart type — and is labelled an inference, because a listing may vanish through a
   sale, a seller ending it, or an expiry, and nothing here can tell those apart.
3. Cohorts thinner than ``MIN_COHORT_N`` are **suppressed**: the count is shown, the
   statistics are not.
4. Both **discontinuities are drawn** as rules on the chart with an explanation.
5. **Capped scans are marked** on the points they produced.

Data comes from ``scan_aggregate`` alone. There is no path from this page to
``listing_observation``, which is what keeps a chart drawn today identical to the
same chart drawn after retention pruning.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from starlette.status import HTTP_400_BAD_REQUEST

from touchstone.web import charts, views
from touchstone.web.routes._common import DbSession, not_found, render

router = APIRouter(prefix="/queries")

WINDOW_CHOICES = (7, 30, 90, 365)
DEFAULT_WINDOW_DAYS = 30
# Enough lines to read; more than this and a chart of forty cohorts is a smear.
MAX_SERIES_CHARTED = 6


def _metric_lines(series: views.CohortSeries, metric: str) -> list[charts.LineSpec]:
    """Build the p10 / median pair for one cohort.

    A suppressed point contributes a ``None`` sample, which ``line_plot`` renders as
    a break in the line rather than a bridge across it.
    """

    def sample(point: views.AskingPoint, attribute: str) -> charts.Sample:
        value = None if point.suppressed else getattr(point, attribute)
        return charts.Sample(at=point.observed_at, value=value)

    if metric == "per_gb":
        return [
            charts.LineSpec(
                label="median asking $/GB",
                role="median",
                samples=tuple(sample(p, "per_gb_median") for p in series.points),
            ),
            charts.LineSpec(
                label="p10 asking $/GB",
                role="p10",
                samples=tuple(sample(p, "per_gb_p10") for p in series.points),
            ),
        ]
    return [
        charts.LineSpec(
            label="median asking price",
            role="median",
            samples=tuple(sample(p, "price_median") for p in series.points),
        ),
        charts.LineSpec(
            label="p10 asking price",
            role="p10",
            samples=tuple(sample(p, "price_p10") for p in series.points),
        ),
    ]


@router.get("/{query_id}/trend", name="query_trend")
def query_trend(
    query_id: int,
    request: Request,
    session: DbSession,
    days: int = DEFAULT_WINDOW_DAYS,
    cohort: str | None = None,
    metric: str = "per_gb",
) -> Any:
    if days not in WINDOW_CHOICES:
        return render(
            request,
            "error.html",
            message=f"Window must be one of {', '.join(str(d) for d in WINDOW_CHOICES)} days.",
            status_code=HTTP_400_BAD_REQUEST,
        )
    if metric not in {"per_gb", "price"}:
        return render(
            request,
            "error.html",
            message="Metric must be per_gb or price.",
            status_code=HTTP_400_BAD_REQUEST,
        )

    trend = views.query_trend(
        session,
        query_id,
        since=views.default_since(days),
        cohort_key=cohort,
    )
    if trend is None:
        raise not_found("query")

    rules = tuple(
        (marker.at, marker.label, marker.detail) for marker in trend.discontinuities
    )
    charted = trend.series[:MAX_SERIES_CHARTED]
    panels = [
        {
            "series": series,
            "plot": charts.line_plot(
                _metric_lines(series, metric),
                discontinuities=rules,
                flagged=tuple(p.observed_at for p in series.points if p.capped),
            ),
        }
        for series in charted
    ]

    return render(
        request,
        "trend.html",
        trend=trend,
        panels=panels,
        hidden_series=trend.series[MAX_SERIES_CHARTED:],
        disappearance_plot=charts.disappearance_plot(
            [(point.detected_at, point.count) for point in trend.disappearances]
        ),
        days=days,
        window_choices=WINDOW_CHOICES,
        metric=metric,
        cohort=cohort,
    )
