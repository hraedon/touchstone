"""Detecting listings that left the active pool.

This produces a *disappearance* series, never a sold series. A listing vanishes from
consecutive scans because a buyer bought it, because the seller ended or revised it,
because it expired unsold, or because it fell out of our result window without
ending at all. Those are indistinguishable from the outside, so the series is
labelled for what it is and there is no ``sold_price`` anywhere in this module.

The fourth case is the one that quietly corrupts the series, which is why a capped
scan produces no disappearances at all: when a query hits eBay's 10,000-item ceiling
the visible window is a moving subset of the real result set, and items crossing its
edge are an artifact of paging, not a market event.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PreviousListing:
    """What we knew about a listing at the end of the previous scan."""

    item_id: str
    cohort_key: str
    last_price: float
    last_total_cost: float | None
    currency: str


@dataclass(frozen=True)
class Disappearance:
    item_id: str
    cohort_key: str
    last_price: float
    last_total_cost: float | None
    currency: str


def find_disappearances(
    previous: list[PreviousListing],
    current_item_ids: set[str],
    *,
    previous_capped: bool,
    current_capped: bool,
) -> list[Disappearance]:
    """Listings present in the previous scan and absent from the current one.

    Returns an empty list when either scan was capped. Both matter: a capped
    *previous* scan means the baseline was a moving window, and a capped *current*
    scan means absence proves nothing.
    """
    if previous_capped or current_capped:
        return []

    return [
        Disappearance(
            item_id=item.item_id,
            cohort_key=item.cohort_key,
            last_price=item.last_price,
            last_total_cost=item.last_total_cost,
            currency=item.currency,
        )
        for item in previous
        if item.item_id not in current_item_ids
    ]
