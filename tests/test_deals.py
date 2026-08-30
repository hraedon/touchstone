"""Cohort keys and deal scoring."""

from __future__ import annotations

from touchstone.extract.cohort import UNSPECCED, CohortFields, cohort_key, is_unspecced
from touchstone.extract.specs import DEAL_CONFIDENCE_FLOOR
from touchstone.scan.deals import MIN_COHORT_N, MIN_SCORE, evaluate, score

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
        cheap = score(per_gb=1.0, cohort_p10=2.0, cohort_median=3.0)
        cheaper = score(per_gb=0.5, cohort_p10=2.0, cohort_median=3.0)
        assert cheaper > cheap

    def test_scores_are_comparable_across_price_levels(self) -> None:
        """Normalizing by cohort spread is what makes a $2 gap at $3/GB comparable
        to a $20 gap at $30/GB."""
        cheap_cohort = score(per_gb=1.0, cohort_p10=2.0, cohort_median=3.0)
        dear_cohort = score(per_gb=10.0, cohort_p10=20.0, cohort_median=30.0)
        assert cheap_cohort == dear_cohort

    def test_degenerate_cohort_does_not_divide_by_zero(self) -> None:
        value = score(per_gb=1.0, cohort_p10=2.0, cohort_median=2.0)
        assert value > 0
        assert value == value  # not NaN


class TestEvaluate:
    def base(self, **overrides: object) -> dict[str, object]:
        args: dict[str, object] = {
            "listing_id": "v1|1|0",
            "cohort_key": "gen=DDR4|cap=32",
            "per_gb": 1.0,
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
        assert evaluate(**self.base(per_gb=2.0)) is None  # type: ignore[arg-type]
        assert evaluate(**self.base(per_gb=2.5)) is None  # type: ignore[arg-type]

    def test_thin_cohort_is_not_flagged(self) -> None:
        """A p10 over four listings is an artifact, not a distribution."""
        assert evaluate(**self.base(cohort_n=MIN_COHORT_N - 1)) is None  # type: ignore[arg-type]
        assert evaluate(**self.base(cohort_n=MIN_COHORT_N)) is not None  # type: ignore[arg-type]

    def test_low_confidence_spec_is_not_flagged_however_cheap(self) -> None:
        """A mis-parsed capacity produces a spectacular fake bargain. Cheapness is
        exactly the symptom of the bug, so it must not be the trigger."""
        assert evaluate(**self.base(per_gb=0.01, confidence=0.5)) is None  # type: ignore[arg-type]

    def test_missing_confidence_is_not_treated_as_confident(self) -> None:
        assert evaluate(**self.base(confidence=None)) is None  # type: ignore[arg-type]

    def test_a_human_correction_overrides_the_confidence_gate(self) -> None:
        """Someone already looked at it."""
        candidate = evaluate(**self.base(confidence=0.1, manual=True))  # type: ignore[arg-type]
        assert candidate is not None

    def test_no_per_gb_means_nothing_to_compare(self) -> None:
        assert evaluate(**self.base(per_gb=None)) is None  # type: ignore[arg-type]

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
                per_gb=3.125,
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
            per_gb=1.0,
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
            per_gb=1.0,
            cohort_p10=2.0,
            cohort_median=3.0,
            cohort_n=8,
            confidence=0.9,
        )
        under = evaluate(
            listing_id="under",
            cohort_key="k",
            per_gb=1.01,
            cohort_p10=2.0,
            cohort_median=3.0,
            cohort_n=8,
            confidence=0.9,
        )
        assert at is not None and at.score == MIN_SCORE
        assert under is None
