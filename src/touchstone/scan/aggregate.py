"""Per-cohort statistics, computed once at scan time.

These values are written to ``scan_aggregate`` and never recomputed. That is not an
optimization — it is what makes the history survive a deletion. If aggregates were
derived on demand from ``listing`` rows, purging a seller would retroactively change
every chart that ever included them, with no way to tell a real market move from an
erasure. See ``docs/measurement-model.md``.

There is deliberately no ``recompute_aggregates`` function in this module. If you
find yourself writing one, read the design spine first.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

# An aggregate over a handful of listings is not a market statistic; over one, it is
# a single seller's asking price wearing a disguise. Rows below this are still
# stored (the count is itself a fact) but suppressed at display time.
MIN_COHORT_N = 5


def percentile(sorted_values: list[float], p: float) -> float:
    """Linear-interpolated percentile over an ascending list.

    ``p`` is a fraction in [0, 1]. Matches the common "linear" / numpy-default
    method so figures are reproducible and comparable across scans.
    """
    if not sorted_values:
        raise ValueError("percentile of an empty sequence")
    if len(sorted_values) == 1:
        return sorted_values[0]
    if not 0.0 <= p <= 1.0:
        raise ValueError(f"percentile p must be in [0, 1], got {p}")

    position = p * (len(sorted_values) - 1)
    low = int(position)
    high = min(low + 1, len(sorted_values) - 1)
    weight = position - low
    return sorted_values[low] * (1.0 - weight) + sorted_values[high] * weight


@dataclass(frozen=True)
class Stats:
    n: int
    minimum: float
    p10: float
    p25: float
    median: float
    mean: float

    @classmethod
    def of(cls, values: list[float]) -> Stats:
        ordered = sorted(values)
        return cls(
            n=len(ordered),
            minimum=ordered[0],
            p10=percentile(ordered, 0.10),
            p25=percentile(ordered, 0.25),
            median=percentile(ordered, 0.50),
            mean=sum(ordered) / len(ordered),
        )


@dataclass(frozen=True)
class CohortStats:
    cohort_key: str
    currency: str
    price: Stats
    # None when the cohort has no usable spec — a cohort with no denominator has
    # no meaningful normalized price, and inventing one would be worse than
    # leaving it null.
    per_unit: Stats | None


@dataclass(frozen=True)
class Priced:
    """The minimum an aggregate needs about one observed listing.

    ``units`` is the category's denominator — GB for memory, TB for storage, 1
    for per-item goods — carried as a float so decimal TB divides exactly.
    """

    cohort_key: str
    total_cost: float | None
    currency: str
    units: float | None = None


def cohort_stats(items: list[Priced]) -> list[CohortStats]:
    """Group observed listings into cohorts and compute each cohort's statistics.

    Mixed currencies within a cohort are not converted — they are split into
    separate cohorts. A median over blended currencies is a meaningless number, and
    silently applying an exchange rate would put an estimate into the truth path.
    """
    buckets: dict[tuple[str, str], list[Priced]] = defaultdict(list)
    for item in items:
        buckets[(item.cohort_key, item.currency)].append(item)

    results: list[CohortStats] = []
    for (key, currency), group in sorted(buckets.items()):
        # Item price remains observable when shipping is absent, but the delivered
        # total is not. Unknown totals do not belong in a total-cost distribution.
        known: list[tuple[float, float | None]] = []
        for item in group:
            if item.total_cost is not None:
                known.append((item.total_cost, item.units))
        if not known:
            continue

        prices = [cost for cost, _units in known]

        per_unit_values = [
            cost / units for cost, units in known if units is not None and units > 0
        ]
        # Only report a normalized price when every known-total member has a
        # denominator. A partial figure would silently describe a different
        # population than the price figure beside it; unknown-total members are
        # already outside both distributions.
        per_unit = (
            Stats.of(per_unit_values)
            if per_unit_values and len(per_unit_values) == len(known)
            else None
        )

        results.append(
            CohortStats(
                cohort_key=key,
                currency=currency,
                price=Stats.of(prices),
                per_unit=per_unit,
            )
        )
    return results
