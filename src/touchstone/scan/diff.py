"""Detecting listings that left the active pool.

This produces a *disappearance* series, never a sold series. A listing vanishes from
consecutive scans because a buyer bought it, because the seller ended or revised it,
because it expired unsold, or because it fell out of our result window without
ending at all. Those are indistinguishable from the outside, so the series is
labelled for what it is and there is no ``sold_price`` anywhere in this module.

The fourth case is the one that quietly corrupts the series, which is why only a
scan that saw its whole result set produces disappearances: when a query hits
eBay's 10,000-item ceiling, or stops at its own page budget before eBay's total,
the visible window is a moving best-match subset of the real result set, and items
crossing its edge are an artifact of paging, not a market event.
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


@dataclass(frozen=True)
class ScanWindow:
    """What one scan can vouch for about absence.

    A diff needs both scans to have seen the whole result set of the same query
    definition. ``truncated`` and ``query_signature`` are ``None`` on scans
    recorded before they were tracked; unknown is not whole.
    """

    capped: bool
    truncated: bool | None
    query_signature: str | None

    @property
    def whole(self) -> bool:
        return not self.capped and self.truncated is False and self.query_signature is not None


def find_disappearances(
    previous: list[PreviousListing],
    current_item_ids: set[str],
    *,
    previous_window: ScanWindow,
    current_window: ScanWindow,
) -> list[Disappearance]:
    """Listings present in the previous scan and absent from the current one.

    Returns an empty list unless both scans saw the whole result set of the same
    query definition. Each condition matters on both sides: a capped or
    truncated *previous* scan means the baseline was a window, a capped or
    truncated *current* scan means absence proves nothing, and a query edited
    between them means the two scans measured different populations.
    """
    if not (previous_window.whole and current_window.whole):
        return []
    if previous_window.query_signature != current_window.query_signature:
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
