"""Cohort keys and deal scoring."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from touchstone.db.models import VerifyOutcome
from touchstone.extract.cohort import (
    UNSPECCED,
    CohortFields,
    StorageFields,
    cohort_key,
    is_unspecced,
    item_key,
    storage_key,
)
from touchstone.extract.specs import DEAL_CONFIDENCE_FLOOR
from touchstone.scan.deals import (
    MIN_COHORT_N,
    MIN_SCORE,
    availability,
    evaluate,
    score,
)

FULL = CohortFields(
    ddr_gen="DDR4",
    form_factor="RDIMM",
    ecc=True,
    registered=True,
    capacity_per_module_gb=32,
    speed_mt=2400,
    rank_org="2Rx4",
)


class TestCohortKey:
    def test_identical_specs_share_a_cohort(self) -> None:
        assert cohort_key(FULL, "3000") == cohort_key(FULL, "3000")

    def test_condition_separates_cohorts(self) -> None:
        """New and pulled-from-a-server memory are different goods. Blending them
        would make the dearer population look like a standing bargain."""
        assert cohort_key(FULL, "1000") != cohort_key(FULL, "3000")

    def test_capacity_separates_cohorts(self) -> None:
        smaller = CohortFields(**{**FULL.__dict__, "capacity_per_module_gb": 16})
        assert cohort_key(smaller, "3000") != cohort_key(FULL, "3000")

    def test_form_factor_separates_rdimm_from_lrdimm(self) -> None:
        lr = CohortFields(**{**FULL.__dict__, "form_factor": "LRDIMM"})
        assert cohort_key(lr, "3000") != cohort_key(FULL, "3000")

    def test_unknown_capacity_goes_to_the_unspecced_bucket(self) -> None:
        """An unknown quantity must not pollute a real cohort — it would corrupt
        the reference every other listing in it is scored against."""
        vague = CohortFields(ddr_gen="DDR4", form_factor="RDIMM")
        key = cohort_key(vague, "3000")
        assert is_unspecced(key)
        assert key.startswith(UNSPECCED)

    def test_none_fields_are_unspecced(self) -> None:
        assert is_unspecced(cohort_key(None, "3000"))

    def test_key_is_human_readable(self) -> None:
        """These strings surface in the UI and exports; an opaque hash would make a
        wrong cohort impossible to spot by eye."""
        key = cohort_key(FULL, "3000")
        assert "cap=32" in key
        assert "mt=2400" in key
        assert "cond=3000" in key

    def test_missing_optional_field_is_marked_not_dropped(self) -> None:
        partial = CohortFields(capacity_per_module_gb=32, ddr_gen=None)
        key = cohort_key(partial, "3000")
        # An absent attribute is recorded as unknown rather than silently omitted,
        # so two differently-unknown listings do not collide with a known one.
        assert "gen=?" in key


class TestScore:
    def test_further_below_p10_scores_higher(self) -> None:
        cheap = score(per_unit=1.0, cohort_p10=2.0, cohort_median=3.0)
        cheaper = score(per_unit=0.5, cohort_p10=2.0, cohort_median=3.0)
        assert cheaper > cheap

    def test_scores_are_comparable_across_price_levels(self) -> None:
        """Normalizing by cohort spread is what makes a $2 gap at $3/GB comparable
        to a $20 gap at $30/GB."""
        cheap_cohort = score(per_unit=1.0, cohort_p10=2.0, cohort_median=3.0)
        dear_cohort = score(per_unit=10.0, cohort_p10=20.0, cohort_median=30.0)
        assert cheap_cohort == dear_cohort

    def test_degenerate_cohort_does_not_divide_by_zero(self) -> None:
        value = score(per_unit=1.0, cohort_p10=2.0, cohort_median=2.0)
        assert value > 0
        assert value == value  # not NaN


class TestEvaluate:
    def base(self, **overrides: object) -> dict[str, object]:
        args: dict[str, object] = {
            "listing_id": "v1|1|0",
            "cohort_key": "gen=DDR4|cap=32",
            "per_unit": 1.0,
            "cohort_p10": 2.0,
            "cohort_median": 3.0,
            "cohort_n": 10,
            "confidence": 0.9,
        }
        args.update(overrides)
        return args

    def test_below_p10_with_a_solid_spec_is_flagged(self) -> None:
        candidate = evaluate(**self.base())  # type: ignore[arg-type]
        assert candidate is not None
        assert candidate.listing_id == "v1|1|0"

    def test_at_or_above_p10_is_not_flagged(self) -> None:
        assert evaluate(**self.base(per_unit=2.0)) is None  # type: ignore[arg-type]
        assert evaluate(**self.base(per_unit=2.5)) is None  # type: ignore[arg-type]

    def test_thin_cohort_is_not_flagged(self) -> None:
        """A p10 over four listings is an artifact, not a distribution."""
        assert evaluate(**self.base(cohort_n=MIN_COHORT_N - 1)) is None  # type: ignore[arg-type]
        assert evaluate(**self.base(cohort_n=MIN_COHORT_N)) is not None  # type: ignore[arg-type]

    def test_low_confidence_spec_is_not_flagged_however_cheap(self) -> None:
        """A mis-parsed capacity produces a spectacular fake bargain. Cheapness is
        exactly the symptom of the bug, so it must not be the trigger."""
        assert evaluate(**self.base(per_unit=0.01, confidence=0.5)) is None  # type: ignore[arg-type]

    def test_missing_confidence_is_not_treated_as_confident(self) -> None:
        assert evaluate(**self.base(confidence=None)) is None  # type: ignore[arg-type]

    def test_a_human_correction_overrides_the_confidence_gate(self) -> None:
        """Someone already looked at it."""
        candidate = evaluate(**self.base(confidence=0.1, manual=True))  # type: ignore[arg-type]
        assert candidate is not None

    def test_no_per_unit_means_nothing_to_compare(self) -> None:
        assert evaluate(**self.base(per_unit=None)) is None  # type: ignore[arg-type]

    def test_no_cohort_reference_means_no_flag(self) -> None:
        assert evaluate(**self.base(cohort_p10=None)) is None  # type: ignore[arg-type]
        assert evaluate(**self.base(cohort_median=None)) is None  # type: ignore[arg-type]

    def test_confidence_floor_is_the_documented_one(self) -> None:
        just_under = evaluate(**self.base(confidence=DEAL_CONFIDENCE_FLOOR - 0.001))  # type: ignore[arg-type]
        just_over = evaluate(**self.base(confidence=DEAL_CONFIDENCE_FLOOR))  # type: ignore[arg-type]
        assert just_under is None
        assert just_over is not None


class TestMerelyCheapestIsNotADeal:
    """The threshold that stops the feed being noise.

    In any cohort of two or more the minimum is *always* below an interpolated p10
    — arithmetic, not a signal. Without MIN_SCORE the cheapest listing in every
    cohort is flagged on every scan, dressing up the bottom of a normal distribution
    as a find.
    """

    def test_bottom_of_a_tight_distribution_is_not_flagged(self) -> None:
        # A cohort clustered 3.125 to 3.34 $/GB: the cheapest is barely under p10.
        assert (
            evaluate(
                listing_id="cheapest",
                cohort_key="k",
                per_unit=3.125,
                cohort_p10=3.1406,
                cohort_median=3.2031,
                cohort_n=8,
                confidence=0.9,
            )
            is None
        )

    def test_a_genuine_outlier_is_flagged(self) -> None:
        candidate = evaluate(
            listing_id="bargain",
            cohort_key="k",
            per_unit=1.0,
            cohort_p10=2.575,
            cohort_median=3.22,
            cohort_n=9,
            confidence=0.9,
        )
        assert candidate is not None
        assert candidate.score >= MIN_SCORE

    def test_threshold_boundary(self) -> None:
        # Exactly one spread-unit below p10 qualifies; a hair less does not.
        at = evaluate(
            listing_id="at",
            cohort_key="k",
            per_unit=1.0,
            cohort_p10=2.0,
            cohort_median=3.0,
            cohort_n=8,
            confidence=0.9,
        )
        under = evaluate(
            listing_id="under",
            cohort_key="k",
            per_unit=1.01,
            cohort_p10=2.0,
            cohort_median=3.0,
            cohort_n=8,
            confidence=0.9,
        )
        assert at is not None and at.score == MIN_SCORE
        assert under is None


class TestAvailability:
    """The read-time state that retires deals whose listing is gone.

    Three recorded timestamps decide it — the listing's last sighting, its
    newest disappearance, and the newest verification — and nothing is stored:
    a later sighting or a fresh observation revokes a retired state on its own.
    """

    T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    def at(self, hours: float) -> datetime:
        return self.T0 + timedelta(hours=hours)

    def state(self, **overrides: object) -> str:
        args: dict[str, object] = {
            "last_seen_at": self.at(1),
            "newest_disappearance_at": None,
            "verified_at": None,
            "verify_outcome": None,
            "verify_requested_at": None,
        }
        args.update(overrides)
        return availability(**args)  # type: ignore[arg-type]

    def test_a_listing_still_seen_is_open(self) -> None:
        assert self.state() == "open"

    def test_a_disappearance_newer_than_the_last_sighting_retires_the_deal(
        self,
    ) -> None:
        assert (
            self.state(newest_disappearance_at=self.at(3)) == "left_market"
        )

    def test_a_later_sighting_revokes_the_disappearance(self) -> None:
        """Any query re-seeing the listing puts the deal back by itself."""
        assert (
            self.state(last_seen_at=self.at(5), newest_disappearance_at=self.at(3))
            == "open"
        )

    def test_a_disappearance_at_the_sighting_instant_stays_open(self) -> None:
        """Two queries scanning in the same second is ambiguity, not evidence;
        ambiguity keeps the deal visible."""
        assert (
            self.state(
                last_seen_at=self.at(3), newest_disappearance_at=self.at(3)
            )
            == "open"
        )

    def test_a_verified_unavailable_outcome_retires_the_deal(self) -> None:
        assert (
            self.state(
                verified_at=self.at(2),
                verify_outcome=VerifyOutcome.UNAVAILABLE,
            )
            == "verified_unavailable"
        )

    def test_a_re_sighting_after_an_unavailable_verification_reopens(self) -> None:
        """getItem has transient failures; a listing demonstrably seen again
        outranks an older not-retrievable response."""
        assert (
            self.state(
                last_seen_at=self.at(5),
                verified_at=self.at(2),
                verify_outcome=VerifyOutcome.UNAVAILABLE,
            )
            == "open"
        )

    def test_an_available_verification_outranks_an_older_disappearance(self) -> None:
        assert (
            self.state(
                newest_disappearance_at=self.at(3),
                verified_at=self.at(4),
                verify_outcome=VerifyOutcome.AVAILABLE,
            )
            == "open"
        )

    def test_a_disappearance_newer_than_an_available_verification_retires(self) -> None:
        """A verification is a snapshot, not a lease."""
        assert (
            self.state(
                newest_disappearance_at=self.at(5),
                verified_at=self.at(4),
                verify_outcome=VerifyOutcome.AVAILABLE,
            )
            == "left_market"
        )

    def test_an_unknown_outcome_is_not_evidence(self) -> None:
        """A transient failure retires nothing; the disappearance still can."""
        assert (
            self.state(verified_at=self.at(2), verify_outcome=VerifyOutcome.UNKNOWN)
            == "open"
        )
        assert (
            self.state(
                newest_disappearance_at=self.at(3),
                verified_at=self.at(2),
                verify_outcome=VerifyOutcome.UNKNOWN,
            )
            == "left_market"
        )

    def test_a_queued_request_is_display_only(self) -> None:
        assert self.state(verify_requested_at=self.at(2)) == "verify_queued"

    def test_a_retired_state_outranks_a_queued_request(self) -> None:
        """The queue badge is informational; hiding still hides."""
        assert (
            self.state(
                newest_disappearance_at=self.at(3), verify_requested_at=self.at(2)
            )
            == "left_market"
        )


class TestStorageCohortKeys:
    def test_the_full_tuple_makes_the_key(self) -> None:
        assert (
            storage_key(
                StorageFields(
                    storage_type="SSD",
                    capacity_tb=3.84,
                    storage_iface="NVMe",
                    storage_form="U.2",
                ),
                "3000",
            )
            == "sto|type=SSD|cap=3.84|iface=NVMe|form=U.2|cond=3000"
        )

    def test_capacity_unknown_is_unspecced(self) -> None:
        assert is_unspecced(storage_key(StorageFields(storage_type="SSD"), "3000"))

    def test_decimal_capacities_keep_their_decimals(self) -> None:
        assert "cap=3.84" in storage_key(StorageFields(capacity_tb=3.84), "3000")

    def test_whole_terrabytes_do_not_render_as_decimals(self) -> None:
        assert "cap=16" in storage_key(StorageFields(capacity_tb=16.0), "3000")

    def test_interface_separates_substitutability(self) -> None:
        sata = storage_key(
            StorageFields(storage_type="SSD", capacity_tb=1.92, storage_iface="SATA"), "3000"
        )
        nvme = storage_key(
            StorageFields(storage_type="SSD", capacity_tb=1.92, storage_iface="NVMe"), "3000"
        )
        assert sata != nvme

    def test_drive_type_separates_ssd_from_hdd(self) -> None:
        ssd = storage_key(StorageFields(storage_type="SSD", capacity_tb=16), "3000")
        hdd = storage_key(StorageFields(storage_type="HDD", capacity_tb=16), "3000")
        assert ssd != hdd


class TestItemCohortKeys:
    def test_the_model_is_the_key(self) -> None:
        assert item_key("E810-XXVDA2", "3000") == "itm|model=E810-XXVDA2|cond=3000"

    def test_separator_variants_of_one_model_collide(self) -> None:
        assert item_key("E810-XXVDA2", "3000") == item_key("E810 XXVDA2", "3000")
        assert item_key("E810-XXVDA2", "3000") == item_key("e810_xxvda2", "3000")
        assert item_key("E810-XXVDA2", "3000") == item_key(" e810-xxvda2 ", "3000")

    def test_variants_of_one_adapter_are_different_goods(self) -> None:
        assert item_key("E810-XXVDA2", "3000") != item_key("E810-CQDA2", "3000")

    def test_no_model_is_unspecced(self) -> None:
        assert is_unspecced(item_key(None, "3000"))
        assert is_unspecced(item_key("   ", "3000"))


class TestMemoryKeyStability:
    def test_the_memory_format_is_frozen_byte_for_byte(self) -> None:
        """Historical aggregates carry this exact string; any change splits every
        memory series in two and reads as a market move."""
        assert (
            cohort_key(FULL, "3000")
            == "gen=DDR4|ff=RDIMM|ecc=y|reg=y|cap=32|mt=2400|rank=2Rx4|cond=3000"
        )
