"""Cohort keys — the grouping within which a price is meaningful.

A price only means something next to the price of a substitutable good. Comparing a
32GB 2Rx4 PC4-2400 RDIMM against an 8GB UDIMM produces a number with no
interpretation, so every statistic and every deal score is scoped to a cohort.

What makes goods substitutable depends on what kind of good they are, so each
category has its own key format. The memory format predates categories and is
kept byte-stable on purpose: changing it would split every existing series in
two, and a discontinuity you cannot see is indistinguishable from a market
move. New categories prefix their keys so no field can ever collide across
formats.

Condition is part of every key rather than a modifier applied afterwards. New
goods and goods pulled from a decommissioned server are different goods at
different prices; a statistic blending them describes neither, and the cheaper
population would make the dearer one look like a standing bargain.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# Listings we could not spec go here rather than into a real cohort. Mixing an
# unknown quantity into a cohort corrupts the reference every other listing in it is
# scored against, which is worse than not scoring the unknown listing at all.
UNSPECCED = "unspecced"


@dataclass(frozen=True)
class CohortFields:
    """The attributes that make two memory listings comparable."""

    ddr_gen: str | None = None
    form_factor: str | None = None
    ecc: bool | None = None
    registered: bool | None = None
    capacity_per_module_gb: int | None = None
    speed_mt: int | None = None
    rank_org: str | None = None


@dataclass(frozen=True)
class StorageFields:
    """The attributes that make two storage listings comparable.

    Brand is deliberately absent: to a buyer shopping the pull market a Micron
    3.84TB U.2 NVMe SSD and an Intel one are the same good, and brand-keyed
    cohorts would mostly sit under the scoring floor forever.
    """

    storage_type: str | None = None  # SSD | HDD
    capacity_tb: float | None = None  # decimal: 3.84 is a real step, not rounding
    storage_iface: str | None = None  # NVMe | SATA | SAS
    storage_form: str | None = None  # 2.5 | 3.5 | U.2 | M.2


_SEP = re.compile(r"[\s_]+")
_REPEATED_HYPHEN = re.compile(r"-{2,}")


def normalize_model(model: str) -> str:
    """Canonical form for a part number, so variants of the same model collide.

    "E810 XXVDA2", "E810_XXVDA2" and "e810-xxvda2" are one good in one cohort.
    Separators are collapsed to single hyphens rather than stripped, so distinct
    part numbers stay distinct.
    """
    folded = unicodedata.normalize("NFKC", model).casefold().strip()
    folded = _SEP.sub("-", folded)
    folded = _REPEATED_HYPHEN.sub("-", folded).strip("-")
    return folded.upper()


def _part(label: str, value: object) -> str:
    if value is None:
        return f"{label}=?"
    if isinstance(value, bool):
        return f"{label}={'y' if value else 'n'}"
    return f"{label}={value}"


def _tb(value: float) -> str:
    """3.84 stays 3.84; 16.0 renders as 16 so keys stay short and stable."""
    text = f"{value:g}"
    return text


def cohort_key(fields: CohortFields | None, condition_id: str | None) -> str:
    """A stable, human-readable cohort identifier for memory listings.

    Readability is deliberate: these strings appear in the UI and in exported data,
    and an opaque hash would make a wrong cohort impossible to spot by eye.

    Returns the unspecced bucket when capacity is unknown — without capacity there
    is no $/GB and therefore nothing the cohort could normalize.

    The format is frozen: it predates categories, every historical aggregate
    carries it, and changing it would split every memory series in two.
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


def storage_key(fields: StorageFields | None, condition_id: str | None) -> str:
    """A stable, human-readable cohort identifier for storage listings.

    Returns the unspecced bucket when capacity is unknown — without TB there is
    no $/TB and therefore nothing the cohort could normalize.
    """
    condition = condition_id or "unknown"
    if fields is None or fields.capacity_tb is None:
        return f"{UNSPECCED}|cond={condition}"

    return "|".join(
        [
            "sto",
            _part("type", fields.storage_type),
            _part("cap", _tb(fields.capacity_tb)),
            _part("iface", fields.storage_iface),
            _part("form", fields.storage_form),
            _part("cond", condition),
        ]
    )


def item_key(model: str | None, condition_id: str | None) -> str:
    """A stable, human-readable cohort identifier for per-item listings.

    The normalized model is the identity of the good: an E810-XXVDA2 and an
    E810-CQDA2 share a brand and nothing else, so they must not share a cohort.
    Returns the unspecced bucket when no model could be read.
    """
    condition = condition_id or "unknown"
    if model is None or not model.strip():
        return f"{UNSPECCED}|cond={condition}"

    return f"itm|model={normalize_model(model)}|cond={condition}"


def is_unspecced(key: str) -> bool:
    return key.startswith(UNSPECCED)
