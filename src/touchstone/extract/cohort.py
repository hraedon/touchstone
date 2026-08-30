"""Cohort keys — the grouping within which a price is meaningful.

A price only means something next to the price of a substitutable good. Comparing a
32GB 2Rx4 PC4-2400 RDIMM against an 8GB UDIMM produces a number with no
interpretation, so every statistic and every deal score is scoped to a cohort.

Condition is part of the key rather than a modifier applied afterwards. New memory
and memory pulled from a decommissioned server are different goods at different
prices; a statistic blending them describes neither, and the cheaper population would
make the dearer one look like a standing bargain.
"""

from __future__ import annotations

from dataclasses import dataclass

# Listings we could not spec go here rather than into a real cohort. Mixing an
# unknown quantity into a cohort corrupts the reference every other listing in it is
# scored against, which is worse than not scoring the unknown listing at all.
UNSPECCED = "unspecced"


@dataclass(frozen=True)
class CohortFields:
    """The attributes that make two listings comparable."""

    ddr_gen: str | None = None
    form_factor: str | None = None
    ecc: bool | None = None
    registered: bool | None = None
    capacity_per_module_gb: int | None = None
    speed_mt: int | None = None
    rank_org: str | None = None


def _part(label: str, value: object) -> str:
    if value is None:
        return f"{label}=?"
    if isinstance(value, bool):
        return f"{label}={'y' if value else 'n'}"
    return f"{label}={value}"


def cohort_key(fields: CohortFields | None, condition_id: str | None) -> str:
    """A stable, human-readable cohort identifier.

    Readability is deliberate: these strings appear in the UI and in exported data,
    and an opaque hash would make a wrong cohort impossible to spot by eye.

    Returns the unspecced bucket when capacity is unknown — without capacity there
    is no $/GB and therefore nothing the cohort could normalize.
    """
    condition = condition_id or "unknown"
    if fields is None or fields.capacity_per_module_gb is None:
        return f"{UNSPECCED}|cond={condition}"

    return "|".join(
        [
            _part("gen", fields.ddr_gen),
            _part("ff", fields.form_factor),
            _part("ecc", fields.ecc),
            _part("reg", fields.registered),
            _part("cap", fields.capacity_per_module_gb),
            _part("mt", fields.speed_mt),
            _part("rank", fields.rank_org),
            _part("cond", condition),
        ]
    )


def is_unspecced(key: str) -> bool:
    return key.startswith(UNSPECCED)
