"""Statistics that go into scan_aggregate."""

from __future__ import annotations

import pytest

from touchstone.extract.cohort import CohortFields, cohort_key
from touchstone.scan.aggregate import Priced, Stats, cohort_stats, percentile


def test_percentile_interpolates() -> None:
    values = [10.0, 20.0, 30.0, 40.0]
    assert percentile(values, 0.0) == 10.0
    assert percentile(values, 1.0) == 40.0
    # position = 0.5 * 3 = 1.5 -> halfway between 20 and 30
    assert percentile(values, 0.5) == 25.0


def test_percentile_single_value() -> None:
    assert percentile([7.0], 0.1) == 7.0
    assert percentile([7.0], 0.9) == 7.0


def test_percentile_rejects_empty_and_out_of_range() -> None:
    with pytest.raises(ValueError):
        percentile([], 0.5)
    with pytest.raises(ValueError):
        percentile([1.0, 2.0], 1.5)


def test_stats_of() -> None:
    stats = Stats.of([30.0, 10.0, 20.0])
    assert stats.n == 3
    assert stats.minimum == 10.0
    assert stats.median == 20.0
    assert stats.mean == 20.0


def test_cohorts_split_by_key() -> None:
    items = [
        Priced("a", 100.0, "USD"),
        Priced("a", 200.0, "USD"),
        Priced("b", 50.0, "USD"),
    ]
    cohorts = {c.cohort_key: c for c in cohort_stats(items)}
    assert set(cohorts) == {"a", "b"}
    assert cohorts["a"].price.n == 2
    assert cohorts["b"].price.median == 50.0


def test_mixed_currencies_are_separate_cohorts_not_blended() -> None:
    """A median across currencies is a meaningless number.

    Converting silently would put an exchange-rate estimate into the truth path, so
    the currencies are split instead.
    """
    items = [
        Priced("a", 100.0, "USD"),
        Priced("a", 100.0, "GBP"),
    ]
    cohorts = cohort_stats(items)
    assert len(cohorts) == 2
    assert {c.currency for c in cohorts} == {"USD", "GBP"}
    assert all(c.price.n == 1 for c in cohorts)


def test_per_gb_is_none_when_specs_are_partial() -> None:
    """A $/GB over the specced subset would describe a different population than
    the price figure printed beside it."""
    items = [
        Priced("a", 100.0, "USD", total_gb=32),
        Priced("a", 200.0, "USD", total_gb=None),
    ]
    (cohort,) = cohort_stats(items)
    assert cohort.price.n == 2
    assert cohort.per_gb is None


def test_per_gb_computed_when_every_member_is_specced() -> None:
    items = [
        Priced("a", 64.0, "USD", total_gb=32),
        Priced("a", 128.0, "USD", total_gb=32),
    ]
    (cohort,) = cohort_stats(items)
    assert cohort.per_gb is not None
    assert cohort.per_gb.minimum == 2.0
    assert cohort.per_gb.median == 3.0


def test_cohort_key_separates_conditions() -> None:
    """New and pulled-from-a-server memory are different goods at different prices;
    a statistic blending them describes neither."""
    fields = CohortFields(capacity_per_module_gb=32, ddr_gen="DDR4")
    assert cohort_key(fields, "1000") != cohort_key(fields, "3000")
    assert cohort_key(fields, None) == cohort_key(fields, None)


def test_aggregate_module_exposes_no_recompute_path() -> None:
    """scan_aggregate must never be regenerable from listing rows.

    That is what keeps a chart honest after a deletion. This test exists to make a
    well-meaning 'let's just recompute the aggregates' refactor fail loudly.
    """
    import touchstone.scan.aggregate as module

    offenders = [name for name in dir(module) if "recompute" in name.lower()]
    assert offenders == [], f"aggregates must not be recomputable, found: {offenders}"
