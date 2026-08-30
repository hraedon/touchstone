"""Title -> spec extraction.

The lot-multiplier cases carry the most weight: a 4x capacity error manufactures a
bargain that does not exist and drags the cohort every other listing is scored
against.
"""

from __future__ import annotations

import pytest

from touchstone.extract.specs import (
    DEAL_CONFIDENCE_FLOOR,
    LLM_THRESHOLD,
    SpecCandidate,
    extract_regex,
    plausible,
)


class TestLotMultiplier:
    """The highest-consequence parse in the system."""

    @pytest.mark.parametrize(
        ("title", "per", "count", "total"),
        [
            # Plain single module.
            ("32GB 2Rx4 PC4-2400T-R Samsung Server RAM ECC REG", 32, 1, 32),
            # Explicit lot.
            ("Lot of 4 x 32GB DDR4 PC4-2133P ECC RDIMM Server Memory", 32, 4, 128),
            # Total stated with its breakdown.
            ("128GB (4x32GB) DDR4-2400 ECC REG Server RAM", 32, 4, 128),
            ("64GB (2 x 32GB) PC4-19200 2Rx4 ECC Registered", 32, 2, 64),
            # Bare multiplier, no stated total.
            ("8x16GB DDR4 PC4-2133P ECC RDIMM", 16, 8, 128),
            # Lot count separated from the capacity.
            ("Lot of 4 32GB PC4-2400 ECC RDIMM Server Memory", 32, 4, 128),
            # Piece-count phrasing.
            ("Samsung 16GB 1Rx4 PC4-2400T-R DDR4 ECC RDIMM 2 pcs", 16, 2, 32),
        ],
    )
    def test_multiplier_cases(self, title: str, per: int, count: int, total: int) -> None:
        spec = extract_regex(title)
        assert (spec.capacity_per_module_gb, spec.module_count, spec.total_gb) == (
            per,
            count,
            total,
        ), spec.notes

    def test_rank_code_is_not_a_multiplier(self) -> None:
        """`2Rx4` contains an x. Reading it as a multiplier would double every
        registered DIMM's capacity, systematically, across the whole corpus.

        This passes with or without the rank strip in `_capacities` — the
        multiplier patterns require a trailing `gb`, which already excludes rank
        codes. Recorded so nobody mistakes this for coverage of that strip.
        """
        spec = extract_regex("32GB 2Rx4 PC4-2400T-R ECC REG")
        assert spec.module_count == 1
        assert spec.total_gb == 32
        assert spec.rank_org == "2Rx4"

    def test_rank_code_with_a_lot_still_parses_both(self) -> None:
        spec = extract_regex("Lot of 8 x 16GB 2Rx4 PC4-2133P ECC RDIMM")
        assert spec.capacity_per_module_gb == 16
        assert spec.module_count == 8
        assert spec.total_gb == 128
        assert spec.rank_org == "2Rx4"

    def test_self_contradicting_title_yields_nothing_rather_than_a_guess(self) -> None:
        """"128GB (4x64GB)" does not multiply out. A guess here is how a fake
        bargain is manufactured, so the parse refuses and the model gets a turn."""
        spec = extract_regex("128GB (4x64GB) DDR4 ECC RDIMM")
        assert spec.total_gb is None
        assert spec.confidence < LLM_THRESHOLD
        assert "inconsistent" in spec.notes

    def test_ambiguous_multiple_capacities_refuses(self) -> None:
        spec = extract_regex("16GB 32GB 64GB mixed server memory job lot ECC")
        assert spec.total_gb is None
        assert "ambiguous" in spec.notes


class TestSpeed:
    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            ("32GB PC4-2400T-R ECC RDIMM", 2400),
            ("32GB PC4-19200 ECC RDIMM", 2400),   # bandwidth notation, same speed
            ("32GB PC4-17000 ECC RDIMM", 2133),
            ("32GB DDR4-2400 ECC RDIMM", 2400),
            ("32GB DDR4 2666MHz ECC RDIMM", 2666),
            ("32GB PC4-21300 ECC RDIMM", 2666),
        ],
    )
    def test_speed_notations_agree(self, title: str, expected: int) -> None:
        assert extract_regex(title).speed_mt == expected

    def test_missing_speed_is_none_not_zero(self) -> None:
        assert extract_regex("32GB ECC RDIMM server memory").speed_mt is None


class TestFormFactor:
    def test_lrdimm_is_not_classified_as_rdimm(self) -> None:
        """An LRDIMM is registered, so a naive 'reg' test captures it. They are not
        substitutable and must not share a cohort."""
        spec = extract_regex("64GB 4Rx4 PC4-2400L LRDIMM Load Reduced ECC")
        assert spec.form_factor == "LRDIMM"
        assert spec.registered is True

    @pytest.mark.parametrize(
        ("title", "form", "registered"),
        [
            ("32GB PC4-2400T-R ECC REG RDIMM", "RDIMM", True),
            ("8GB DDR4 2666 ECC UDIMM Unbuffered", "UDIMM", False),
            ("16GB DDR4 SODIMM laptop memory", "SODIMM", False),
        ],
    )
    def test_form_factors(self, title: str, form: str, registered: bool) -> None:
        spec = extract_regex(title)
        assert spec.form_factor == form
        assert spec.registered is registered


class TestEcc:
    def test_non_ecc_is_not_read_as_ecc(self) -> None:
        """'non-ECC' contains 'ecc'. Reading it as ECC puts consumer memory in a
        server-memory cohort, where it is much cheaper and looks like a deal."""
        assert extract_regex("16GB DDR4 2666 Non-ECC UDIMM").ecc is False

    def test_ecc_detected(self) -> None:
        assert extract_regex("32GB DDR4 ECC RDIMM").ecc is True

    def test_unstated_ecc_is_none(self) -> None:
        assert extract_regex("32GB DDR4 2400 memory").ecc is None


class TestConfidence:
    def test_fully_specified_title_clears_the_threshold(self) -> None:
        spec = extract_regex("32GB 2Rx4 PC4-2400T-R DDR4 ECC REG RDIMM Server Memory")
        assert spec.confidence >= LLM_THRESHOLD

    def test_capacity_free_title_scores_zero(self) -> None:
        spec = extract_regex("Server memory upgrade kit, see photos")
        assert spec.confidence == 0.0
        assert spec.total_gb is None

    def test_sparse_title_is_escalated_not_trusted(self) -> None:
        spec = extract_regex("32GB memory")
        assert spec.total_gb == 32
        assert spec.confidence < LLM_THRESHOLD

    def test_deal_floor_is_at_least_the_llm_threshold(self) -> None:
        """A spec too weak to store without a model must never be strong enough to
        flag a purchase."""
        assert DEAL_CONFIDENCE_FLOOR >= LLM_THRESHOLD


class TestPlausible:
    def test_speed_read_as_capacity_is_rejected(self) -> None:
        """The classic model failure: 'PC4-3200' becomes capacity 3200GB."""
        assert not plausible(SpecCandidate(capacity_per_module_gb=3200, module_count=1))

    def test_inconsistent_arithmetic_is_rejected(self) -> None:
        assert not plausible(
            SpecCandidate(capacity_per_module_gb=32, module_count=4, total_gb=64)
        )

    def test_consistent_candidate_accepted(self) -> None:
        assert plausible(
            SpecCandidate(
                capacity_per_module_gb=32, module_count=4, total_gb=128, speed_mt=2400
            )
        )

    def test_absurd_module_count_rejected(self) -> None:
        assert not plausible(SpecCandidate(capacity_per_module_gb=32, module_count=900))

    def test_every_regex_result_over_the_corpus_is_plausible(self) -> None:
        """Guards the guard: the extractor must never emit something its own range
        check would reject."""
        corpus = [
            "32GB 2Rx4 PC4-2400T-R Samsung ECC REG",
            "Lot of 4 x 32GB DDR4 PC4-2133P ECC RDIMM",
            "128GB (4x32GB) DDR4-2400 ECC REG",
            "128GB (4x64GB) DDR4 ECC RDIMM",
            "16GB DDR4 2666 Non-ECC UDIMM",
            "Server memory upgrade kit, see photos",
            "8x16GB DDR4 PC4-2133P ECC RDIMM",
        ]
        for title in corpus:
            assert plausible(extract_regex(title)), title
