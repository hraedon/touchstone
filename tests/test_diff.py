"""Disappearance detection.

This is an inference series, not a sold series, and the tests hold that line.
"""

from __future__ import annotations

from touchstone.scan.diff import PreviousListing, ScanWindow, find_disappearances


def prev(item_id: str, price: float = 100.0) -> PreviousListing:
    return PreviousListing(
        item_id=item_id,
        cohort_key="q=1|cond=3000",
        last_price=price,
        last_total_cost=price,
        currency="USD",
    )


WHOLE = ScanWindow(capped=False, truncated=False, query_signature="sig-1")
CAPPED = ScanWindow(capped=True, truncated=False, query_signature="sig-1")


def test_missing_listing_is_a_disappearance() -> None:
    gone = find_disappearances(
        [prev("A"), prev("B")],
        {"A"},
        previous_window=WHOLE,
        current_window=WHOLE,
    )
    assert [d.item_id for d in gone] == ["B"]


def test_last_price_is_carried_forward() -> None:
    (only,) = find_disappearances(
        [prev("B", 42.5)], set(), previous_window=WHOLE, current_window=WHOLE
    )
    assert only.last_price == 42.5
    assert only.last_total_cost == 42.5


def test_nothing_disappears_when_all_still_present() -> None:
    assert (
        find_disappearances(
            [prev("A")], {"A", "C"}, previous_window=WHOLE, current_window=WHOLE
        )
        == []
    )


def test_capped_current_scan_produces_no_disappearances() -> None:
    """Beyond eBay's 10,000-item ceiling the visible set is a moving window.
    Items crossing its edge are a paging artifact, not a market event."""
    assert (
        find_disappearances(
            [prev("A"), prev("B")], set(), previous_window=WHOLE, current_window=CAPPED
        )
        == []
    )


def test_capped_previous_scan_also_produces_no_disappearances() -> None:
    """A capped baseline is just as unusable as a capped comparison."""
    assert (
        find_disappearances(
            [prev("A")], set(), previous_window=CAPPED, current_window=WHOLE
        )
        == []
    )


def test_disappearance_carries_no_sold_price() -> None:
    """A vanish may be a sale, a seller ending the listing, or an expiry. The type
    must not acquire a field that asserts it was a sale."""
    (only,) = find_disappearances(
        [prev("B")], set(), previous_window=WHOLE, current_window=WHOLE
    )
    assert not hasattr(only, "sold_price")
    assert not hasattr(only, "sold_at")


def gone_between(previous: ScanWindow, current: ScanWindow) -> list[str]:
    """Item ids that vanished between two scans of a one-item baseline."""
    return [
        d.item_id
        for d in find_disappearances(
            [prev("A")], set(), previous_window=previous, current_window=current
        )
    ]


def test_a_scan_stopped_at_its_page_budget_produces_no_disappearances() -> None:
    """Short of eBay's total, the scan saw a best-match window: an item that
    slid past the last page is a paging artifact, exactly as at the ceiling."""
    window = ScanWindow(capped=False, truncated=True, query_signature="sig-1")
    assert gone_between(WHOLE, window) == []
    assert gone_between(window, WHOLE) == []


def test_an_unrecorded_window_is_not_whole() -> None:
    """Scans from before truncation was tracked cannot vouch for absence."""
    unknown = ScanWindow(capped=False, truncated=None, query_signature=None)
    assert gone_between(unknown, WHOLE) == []


def test_an_edited_query_produces_no_disappearances() -> None:
    """New keywords or categories measure a different population; what the old
    definition saw and the new one does not is not a market event."""
    edited = ScanWindow(capped=False, truncated=False, query_signature="sig-2")
    assert gone_between(WHOLE, edited) == []


def test_two_whole_scans_of_the_same_definition_do_diff() -> None:
    """The guard against windows must not also silence the real series."""
    assert gone_between(WHOLE, WHOLE) == ["A"]
