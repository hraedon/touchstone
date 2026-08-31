"""Geometry for the server-rendered charts.

No JavaScript and no chart library: these functions turn data into coordinates, and
the templates turn coordinates into SVG. Keeping the arithmetic here rather than in
markup is what makes it testable — and the arithmetic is where the honesty rules can
be broken silently.

Two of those rules are enforced by this module rather than by a caller remembering:

* **A suppressed point is a gap, not a value.** A line is emitted as separate path
  segments so it visibly breaks where a cohort was too thin to describe. Drawing one
  continuous polyline across the gap would interpolate a value that was never
  measured and is exactly the sort of quiet fabrication the whole design avoids.
* **The disappearance series gets its own plot.** There is no way to ask this module
  for a chart with an asking line and a disappearance line on shared axes, because
  the two are not the same kind of claim and putting them on one axis says they are.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

WIDTH = 720
HEIGHT = 240
PAD_LEFT = 56
PAD_RIGHT = 16
PAD_TOP = 14
PAD_BOTTOM = 28


@dataclass(frozen=True)
class Sample:
    """One observation on a line.

    ``value`` is None when there is nothing legitimate to draw — the cohort was
    suppressed for thinness, or the statistic was never recorded. It is never
    substituted with a neighbour, an average, or zero.
    """

    at: datetime
    value: float | None


@dataclass(frozen=True)
class LineSpec:
    label: str
    role: str
    samples: tuple[Sample, ...]


@dataclass(frozen=True)
class Dot:
    x: float
    y: float
    at: datetime
    value: float
    flagged: bool = False


@dataclass(frozen=True)
class PlottedLine:
    label: str
    role: str
    segments: tuple[str, ...]
    dots: tuple[Dot, ...]


@dataclass(frozen=True)
class RuleMark:
    x: float
    label: str
    detail: str


@dataclass(frozen=True)
class Tick:
    pos: float
    label: str


@dataclass(frozen=True)
class Plot:
    width: int
    height: int
    left: float
    right: float
    top: float
    bottom: float
    lines: tuple[PlottedLine, ...]
    rules: tuple[RuleMark, ...]
    x_ticks: tuple[Tick, ...]
    y_ticks: tuple[Tick, ...]

    @property
    def empty(self) -> bool:
        return not any(line.segments or line.dots for line in self.lines)


def _nice_bounds(low: float, high: float) -> tuple[float, float]:
    """Pad a value range so a flat series is not drawn as a line on the axis."""
    if high < low:
        low, high = high, low
    if high - low < 1e-9:
        pad = abs(high) * 0.1 or 1.0
        return low - pad, high + pad
    pad = (high - low) * 0.08
    return low - pad, high + pad


def _fmt(value: float) -> str:
    """Round coordinates so the emitted path stays readable and diffable."""
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _money(value: float) -> str:
    if abs(value) >= 100:
        return f"{value:,.0f}"
    if abs(value) >= 1:
        return f"{value:,.2f}"
    return f"{value:.3f}"


def line_plot(
    lines: Sequence[LineSpec],
    *,
    discontinuities: Sequence[tuple[datetime, str, str]] = (),
    flagged: Sequence[datetime] = (),
    width: int = WIDTH,
    height: int = HEIGHT,
) -> Plot:
    """Plot one or more lines that share a time axis and a value axis.

    ``flagged`` marks observations whose underlying scan was capped: eBay refused to
    show past 10,000 results, so that scan sampled a moving window rather than the
    whole population. The point is still real and is still drawn; it is marked so a
    step in the series is not read as a market move.
    """
    left, right = float(PAD_LEFT), float(width - PAD_RIGHT)
    top, bottom = float(PAD_TOP), float(height - PAD_BOTTOM)

    times = [sample.at for line in lines for sample in line.samples]
    values = [
        sample.value for line in lines for sample in line.samples if sample.value is not None
    ]
    if not times or not values:
        return Plot(
            width=width,
            height=height,
            left=left,
            right=right,
            top=top,
            bottom=bottom,
            lines=tuple(
                PlottedLine(label=line.label, role=line.role, segments=(), dots=())
                for line in lines
            ),
            rules=(),
            x_ticks=(),
            y_ticks=(),
        )

    t_min, t_max = min(times), max(times)
    span = (t_max - t_min).total_seconds()
    v_low, v_high = _nice_bounds(min(values), max(values))

    def x_of(at: datetime) -> float:
        if span <= 0:
            return (left + right) / 2
        return left + (at - t_min).total_seconds() / span * (right - left)

    def y_of(value: float) -> float:
        return bottom - (value - v_low) / (v_high - v_low) * (bottom - top)

    flagged_at = set(flagged)
    plotted: list[PlottedLine] = []
    for line in lines:
        segments: list[str] = []
        dots: list[Dot] = []
        current: list[str] = []
        for sample in line.samples:
            if sample.value is None:
                # A break, not a bridge. See the module docstring.
                if len(current) > 1:
                    segments.append(" ".join(current))
                current = []
                continue
            x, y = x_of(sample.at), y_of(sample.value)
            current.append(f"{'M' if not current else 'L'} {_fmt(x)},{_fmt(y)}")
            dots.append(
                Dot(x=x, y=y, at=sample.at, value=sample.value, flagged=sample.at in flagged_at)
            )
        if len(current) > 1:
            segments.append(" ".join(current))
        plotted.append(
            PlottedLine(
                label=line.label, role=line.role, segments=tuple(segments), dots=tuple(dots)
            )
        )

    rules = tuple(
        RuleMark(x=x_of(at), label=label, detail=detail)
        for at, label, detail in discontinuities
        if t_min <= at <= t_max
    )

    y_ticks = tuple(
        Tick(pos=y_of(v_low + (v_high - v_low) * fraction), label=_money(
            v_low + (v_high - v_low) * fraction
        ))
        for fraction in (0.0, 0.5, 1.0)
    )
    x_ticks = (
        (Tick(pos=x_of(t_min), label=t_min.strftime("%d %b")),)
        if span <= 0
        else (
            Tick(pos=x_of(t_min), label=t_min.strftime("%d %b")),
            Tick(pos=x_of(t_max), label=t_max.strftime("%d %b")),
        )
    )

    return Plot(
        width=width,
        height=height,
        left=left,
        right=right,
        top=top,
        bottom=bottom,
        lines=tuple(plotted),
        rules=rules,
        x_ticks=x_ticks,
        y_ticks=y_ticks,
    )


@dataclass(frozen=True)
class Bar:
    x: float
    y: float
    width: float
    height: float
    at: datetime
    count: int


@dataclass(frozen=True)
class BarPlot:
    width: int
    height: int
    left: float
    right: float
    top: float
    bottom: float
    bars: tuple[Bar, ...]
    y_ticks: tuple[Tick, ...]
    x_ticks: tuple[Tick, ...]

    @property
    def empty(self) -> bool:
        return not self.bars


def disappearance_plot(
    points: Sequence[tuple[datetime, int]],
    *,
    width: int = WIDTH,
    height: int = 130,
) -> BarPlot:
    """A count-per-interval bar chart, deliberately a different shape of chart.

    Bars rather than a line, its own axis, its own panel. This series counts
    listings that stopped appearing; whether any of them sold is unknown and
    unknowable from the Browse API. Drawing it like the asking series would invite
    exactly the reading the schema refuses to support.
    """
    left, right = float(PAD_LEFT), float(width - PAD_RIGHT)
    top, bottom = float(PAD_TOP), float(height - PAD_BOTTOM)
    if not points:
        return BarPlot(
            width=width,
            height=height,
            left=left,
            right=right,
            top=top,
            bottom=bottom,
            bars=(),
            y_ticks=(),
            x_ticks=(),
        )

    times = [at for at, _ in points]
    t_min, t_max = min(times), max(times)
    span = (t_max - t_min).total_seconds()
    peak = max(count for _, count in points) or 1
    slot = (right - left) / max(len(points), 1)
    bar_width = max(min(slot * 0.6, 18.0), 2.0)

    bars: list[Bar] = []
    for at, count in points:
        centre = (
            (left + right) / 2
            if span <= 0
            else left + (at - t_min).total_seconds() / span * (right - left)
        )
        bar_height = (count / peak) * (bottom - top)
        bars.append(
            Bar(
                x=centre - bar_width / 2,
                y=bottom - bar_height,
                width=bar_width,
                height=bar_height,
                at=at,
                count=count,
            )
        )

    y_ticks = (Tick(pos=bottom, label="0"), Tick(pos=top, label=str(peak)))
    x_ticks = (
        (Tick(pos=(left + right) / 2, label=t_min.strftime("%d %b")),)
        if span <= 0
        else (
            Tick(pos=left, label=t_min.strftime("%d %b")),
            Tick(pos=right, label=t_max.strftime("%d %b")),
        )
    )
    return BarPlot(
        width=width,
        height=height,
        left=left,
        right=right,
        top=top,
        bottom=bottom,
        bars=tuple(bars),
        y_ticks=y_ticks,
        x_ticks=x_ticks,
    )


# ---------------------------------------------------------------------------
# The signature element: the cohort price band
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PriceBand:
    """Where a listing sits against the cohort it was scored against.

    touchstone's one view the CLI cannot give: the p10-to-median spread drawn as a
    gauge with the flagged listing marked on it. "Below p10" is arithmetic — in any
    cohort of two or more, something is. What matters is *how far* below, in units of
    the cohort's own spread, and that is a distance the eye reads better than a
    number.

    All positions are percentages of the gauge width, so the template needs no
    arithmetic of its own.
    """

    low: float
    p10: float
    median: float
    marked: float
    n: int
    currency: str
    unit: str

    scale_min: float
    scale_max: float

    @property
    def suppressed(self) -> bool:
        from touchstone.scan.aggregate import MIN_COHORT_N

        return self.n < MIN_COHORT_N

    def _pos(self, value: float) -> float:
        width = self.scale_max - self.scale_min
        if width <= 0:
            return 50.0
        return max(0.0, min(100.0, (value - self.scale_min) / width * 100.0))

    @property
    def p10_pos(self) -> float:
        return self._pos(self.p10)

    @property
    def median_pos(self) -> float:
        return self._pos(self.median)

    @property
    def low_pos(self) -> float:
        return self._pos(self.low)

    @property
    def marked_pos(self) -> float:
        return self._pos(self.marked)

    @property
    def band_left(self) -> float:
        return min(self.p10_pos, self.median_pos)

    @property
    def band_width(self) -> float:
        return abs(self.median_pos - self.p10_pos)

    @property
    def degenerate(self) -> bool:
        """True when p10 and the median coincide, so the cohort has no spread.

        Common in this market and not an error: a cohort of forty listings of the
        same module can have most of them at one price. It matters to say so,
        because the score is expressed in spread-units and there is no spread —
        ``scan.deals.score`` falls back to a fraction of the level, and a gauge
        showing a band of zero width would otherwise look like a rendering fault.
        """
        return abs(self.median - self.p10) < 1e-9


def price_band(
    *,
    marked: float,
    p10: float,
    median: float,
    minimum: float | None,
    n: int,
    currency: str = "USD",
    unit: str = "/GB",
) -> PriceBand:
    """Build the gauge, with a margin either side so the marker never sits on the rim."""
    low = minimum if minimum is not None else min(marked, p10)
    lo = min(marked, low, p10, median)
    hi = max(marked, low, p10, median)
    pad = (abs(hi) * 0.1 or 1.0) if hi - lo < 1e-9 else (hi - lo) * 0.15
    return PriceBand(
        low=low,
        p10=p10,
        median=median,
        marked=marked,
        n=n,
        currency=currency,
        unit=unit,
        scale_min=lo - pad,
        scale_max=hi + pad,
    )
