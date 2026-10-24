"""Rendered date labels must point inward at the SVG viewport boundaries."""

from __future__ import annotations

from datetime import UTC, datetime
from xml.etree import ElementTree

import pytest

from touchstone.web import charts
from touchstone.web.app import build_templates

START = datetime(2026, 9, 30, tzinfo=UTC)
END = datetime(2026, 10, 31, tzinfo=UTC)


def _render_chart(kind: str, *, single_instant: bool = False) -> ElementTree.Element:
    times = (START,) if single_instant else (START, END)
    plot: charts.Plot | charts.BarPlot
    if kind == "line":
        plot = charts.line_plot([
            charts.LineSpec("Median asking", "median", tuple(
                charts.Sample(at=at, value=2.0) for at in times
            )),
        ])
    else:
        plot = charts.disappearance_plot([(at, 1) for at in times])
    template = build_templates().get_template("_chart.html")
    macro = getattr(template.module, f"{kind}_chart")
    return ElementTree.fromstring(str(macro(plot, "Synthetic chart")))


def _date_labels(svg: ElementTree.Element) -> list[ElementTree.Element]:
    return [element for element in svg.findall("text") if element.text in {"30 Sep", "31 Oct"}]


@pytest.mark.parametrize("kind", ["line", "bar"])
def test_right_endpoint_label_extends_inward_from_the_final_date(kind: str) -> None:
    svg = _render_chart(kind)
    first, last = _date_labels(svg)
    # Centering at width - PAD_RIGHT places half the date text in the small
    # right gutter, clipping the final glyph. Keep the date at its true x and
    # extend the text leftward, wholly inside the plotted interval.
    assert last.text == "31 Oct"
    assert float(last.attrib["x"]) == charts.WIDTH - charts.PAD_RIGHT
    assert last.attrib["text-anchor"] == "end"
    assert float(first.attrib["x"]) < float(last.attrib["x"])


@pytest.mark.parametrize("kind", ["line", "bar"])
def test_left_endpoint_label_extends_inward_from_the_first_date(kind: str) -> None:
    first = _date_labels(_render_chart(kind))[0]
    assert first.text == "30 Sep"
    assert float(first.attrib["x"]) == charts.PAD_LEFT
    assert first.attrib["text-anchor"] == "start"


@pytest.mark.parametrize("kind", ["line", "bar"])
def test_single_instant_label_stays_centered_on_the_single_observation(kind: str) -> None:
    labels = _date_labels(_render_chart(kind, single_instant=True))
    assert len(labels) == 1
    assert float(labels[0].attrib["x"]) == (
        charts.PAD_LEFT + charts.WIDTH - charts.PAD_RIGHT
    ) / 2
    assert labels[0].attrib["text-anchor"] == "middle"


@pytest.mark.parametrize("kind", ["line", "bar"])
def test_value_axis_labels_still_align_with_the_value_axis(kind: str) -> None:
    svg = _render_chart(kind)
    dates = _date_labels(svg)
    values = [label for label in svg.findall("text") if label not in dates]
    assert values
    assert all(label.attrib["text-anchor"] == "end" for label in values)
    assert all(float(label.attrib["x"]) < charts.PAD_LEFT for label in values)
