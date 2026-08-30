"""Disappearance detection.

This is an inference series, not a sold series, and the tests hold that line.
"""

from __future__ import annotations

from touchstone.scan.diff import PreviousListing, find_disappearances


def prev(item_id: str, price: float = 100.0) -> PreviousListing:
    return PreviousListing(
        item_id=item_id,
        cohort_key="q=1|cond=3000",
        last_price=price,
        last_total_cost=price,
        currency="USD",
    )


def test_missing_listing_is_a_disappearance() -> None:
    gone = find_disappearances(
        [prev("A"), prev("B")],
        {"A"},
        previous_capped=False,
        current_capped=False,
    )
    assert [d.item_id for d in gone] == ["B"]


def test_last_price_is_carried_forward() -> None:
    (only,) = find_disappearances(
        [prev("B", 42.5)], set(), previous_capped=False, current_capped=False
    )
    assert only.last_price == 42.5
    assert only.last_total_cost == 42.5


def test_nothing_disappears_when_all_still_present() -> None:
    assert (
        find_disappearances(
            [prev("A")], {"A", "C"}, previous_capped=False, current_capped=False
        )
        == []
    )


def test_capped_current_scan_produces_no_disappearances() -> None:
    """Beyond eBay's 10,000-item ceiling the visible set is a moving window.
    Items crossing its edge are a paging artifact, not a market event."""
    assert (
        find_disappearances(
            [prev("A"), prev("B")], set(), previous_capped=False, current_capped=True
        )
        == []
    )


def test_capped_previous_scan_also_produces_no_disappearances() -> None:
    """A capped baseline is just as unusable as a capped comparison."""
    assert (
        find_disappearances(
            [prev("A")], set(), previous_capped=True, current_capped=False
        )
        == []
    )


def test_disappearance_carries_no_sold_price() -> None:
    """A vanish may be a sale, a seller ending the listing, or an expiry. The type
    must not acquire a field that asserts it was a sale."""
    (only,) = find_disappearances(
        [prev("B")], set(), previous_capped=False, current_capped=False
    )
    assert not hasattr(only, "sold_price")
    assert not hasattr(only, "sold_at")
