"""Chart geometry.

The arithmetic lives in Python rather than in markup precisely so it can be checked
here. Two of these assertions are honesty constraints, not cosmetics: a suppressed
point must produce a *break* in the line, and the disappearance series must be a
different kind of object entirely.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from touchstone.web import charts

T0 = datetime(2026, 8, 1, tzinfo=UTC)


def _line(values: list[float | None]) -> charts.LineSpec:
    return charts.LineSpec(
        label="median asking",
        role="median",
        samples=tuple(
            charts.Sample(at=T0 + timedelta(days=index), value=value)
            for index, value in enumerate(values)
        ),
    )


class TestSuppressedPointsBreakTheLine:
    def test_a_gap_splits_the_path_rather_than_bridging_it(self) -> None:
        plot = charts.line_plot([_line([1.0, 2.0, None, 4.0, 5.0])])
        assert len(plot.lines[0].segments) == 2, (
            "a suppressed point must break the line; one segment would interpolate "
            "a value that was never measured"
        )

    def test_an_unbroken_series_is_one_segment(self) -> None:
        plot = charts.line_plot([_line([1.0, 2.0, 3.0, 4.0])])
        assert len(plot.lines[0].segments) == 1

    def test_a_suppressed_point_contributes_no_dot(self) -> None:
        plot = charts.line_plot([_line([1.0, None, 3.0])])
        assert len(plot.lines[0].dots) == 2

    def test_an_isolated_point_draws_no_segment(self) -> None:
        """A single point is not a trend and must not be drawn as one."""
        plot = charts.line_plot([_line([None, 2.0, None])])
        assert plot.lines[0].segments == ()
        assert len(plot.lines[0].dots) == 1

    def test_an_entirely_suppressed_series_is_empty(self) -> None:
        assert charts.line_plot([_line([None, None])]).empty


class TestAxesAndMarks:
    def test_a_flat_series_is_not_drawn_on_the_axis(self) -> None:
        plot = charts.line_plot([_line([5.0, 5.0, 5.0])])
        ys = {dot.y for dot in plot.lines[0].dots}
        assert len(ys) == 1
        assert plot.top < ys.pop() < plot.bottom

    def test_a_discontinuity_inside_the_window_becomes_a_rule(self) -> None:
        plot = charts.line_plot(
            [_line([1.0, 2.0, 3.0])],
            discontinuities=[(T0 + timedelta(days=1), "Floor changed", "why")],
        )
        assert len(plot.rules) == 1
        assert plot.left <= plot.rules[0].x <= plot.right

    def test_a_discontinuity_outside_the_window_is_dropped(self) -> None:
        plot = charts.line_plot(
            [_line([1.0, 2.0])],
            discontinuities=[(T0 - timedelta(days=40), "Floor changed", "why")],
        )
        assert plot.rules == ()

    def test_capped_observations_are_flagged(self) -> None:
        plot = charts.line_plot([_line([1.0, 2.0, 3.0])], flagged=[T0 + timedelta(days=1)])
        flagged = [dot for dot in plot.lines[0].dots if dot.flagged]
        assert len(flagged) == 1
        assert flagged[0].at == T0 + timedelta(days=1)

    def test_paths_carry_no_nan_or_inf(self) -> None:
        plot = charts.line_plot([_line([0.0, 0.0])])
        for segment in plot.lines[0].segments:
            assert "nan" not in segment and "inf" not in segment


class TestDisappearancesAreADifferentObject:
    def test_the_disappearance_plot_is_bars_not_a_line(self) -> None:
        plot = charts.disappearance_plot([(T0, 3), (T0 + timedelta(days=1), 1)])
        assert isinstance(plot, charts.BarPlot)
        assert not hasattr(plot, "lines"), (
            "there must be no way to put an asking line and a disappearance count on "
            "the same axes"
        )
        assert len(plot.bars) == 2

    def test_bars_scale_to_the_peak_and_sit_on_the_baseline(self) -> None:
        plot = charts.disappearance_plot([(T0, 1), (T0 + timedelta(days=1), 4)])
        tallest = max(plot.bars, key=lambda bar: bar.height)
        assert tallest.count == 4
        assert tallest.y + tallest.height == pytest.approx(plot.bottom)

    def test_no_points_is_empty(self) -> None:
        assert charts.disappearance_plot([]).empty


class TestPriceBand:
    def test_the_marker_sits_left_of_the_band_when_below_p10(self) -> None:
        band = charts.price_band(marked=1.0, p10=1.5, median=2.5, minimum=1.0, n=12)
        assert band.marked_pos < band.p10_pos < band.median_pos
        assert band.band_width > 0

    def test_positions_stay_inside_the_gauge(self) -> None:
        band = charts.price_band(marked=0.01, p10=9.0, median=10.0, minimum=0.01, n=9)
        for position in (band.marked_pos, band.p10_pos, band.median_pos, band.low_pos):
            assert 0.0 <= position <= 100.0

    def test_a_degenerate_cohort_does_not_divide_by_zero(self) -> None:
        band = charts.price_band(marked=2.0, p10=2.0, median=2.0, minimum=2.0, n=7)
        assert band.marked_pos == pytest.approx(50.0, abs=0.001)

    def test_a_thin_cohort_marks_itself_suppressed(self) -> None:
        assert charts.price_band(marked=1.0, p10=2.0, median=3.0, minimum=1.0, n=4).suppressed
        assert not charts.price_band(marked=1.0, p10=2.0, median=3.0, minimum=1.0, n=5).suppressed


class TestDegenerateCohorts:
    """Real cohorts often have no spread; the gauge must say so, not look broken."""

    def test_a_cohort_whose_p10_equals_its_median_is_flagged_degenerate(self) -> None:
        band = charts.price_band(marked=4.4, p10=5.3, median=5.3, minimum=4.4, n=40)
        assert band.degenerate is True
        assert band.band_width == pytest.approx(0.0)

    def test_a_cohort_with_spread_is_not(self) -> None:
        band = charts.price_band(marked=4.4, p10=5.3, median=6.1, minimum=4.4, n=40)
        assert band.degenerate is False
        assert band.band_width > 0
