"""The operator's seller exclusion list.

The compliance position this supports is narrow and worth stating: the list is
hand-authored, was not derived from eBay, exists to *prevent* collection rather than
enable it, and the name is the entire holding. These tests exist to keep all four of
those true, because each one is easy to break by accident and none of them is visible
in a diff.
"""

from __future__ import annotations

import logging

import pytest

from touchstone.ebay import exclusions
from touchstone.ebay.client import _describe_filter


class TestParsing:
    def test_commas_newlines_and_comments(self) -> None:
        names = exclusions.parse("alpha, bravo\n# a note\n charlie \n\n")
        assert names == ("alpha", "bravo", "charlie")

    def test_it_is_order_insensitive_and_deduplicated(self) -> None:
        """Reordering the Secret must not read as a change to the population."""
        a = exclusions.parse("bravo,alpha,alpha")
        b = exclusions.parse("alpha\nbravo")
        assert a == b
        assert exclusions.digest(a, "salt") == exclusions.digest(b, "salt")

    def test_empty_is_empty(self) -> None:
        for raw in (None, "", "   ", "\n#only a comment\n"):
            assert exclusions.parse(raw) == ()
        assert exclusions.digest(()) == ""
        assert exclusions.as_filter(()) is None

    def test_an_entry_that_could_alter_the_filter_is_refused(self) -> None:
        """The list is interpolated into a filter expression; a stray brace or comma
        must not be able to change what that expression means."""
        for bad in ("alpha}", "alpha|bravo", "a b", "price:[1..2]", "{x}", "a" * 65):
            with pytest.raises(exclusions.ExclusionListError):
                exclusions.parse(bad)

    def test_a_list_longer_than_ebay_accepts_is_refused_here(self) -> None:
        """eBay rejects an over-long filter with a 400, stopping the scan outright.

        Measured against production: 351 names (7,728 chars) succeeded, 401 (8,828)
        returned HTTP 400. Failing here names the cause.
        """
        too_many = ",".join(f"seller{i}" for i in range(exclusions.MAX_EXCLUDED_SELLERS + 1))
        with pytest.raises(exclusions.ExclusionListError, match="limit is"):
            exclusions.parse(too_many)
        ok = ",".join(f"seller{i}" for i in range(exclusions.MAX_EXCLUDED_SELLERS))
        assert len(exclusions.parse(ok)) == exclusions.MAX_EXCLUDED_SELLERS

    def test_the_ceiling_stays_below_what_ebay_actually_accepts(self) -> None:
        """Guards the guard: the limit is only useful while it is conservative."""
        names = tuple(f"a_seller_name_{i:04d}" for i in range(exclusions.MAX_EXCLUDED_SELLERS))
        expression = exclusions.as_filter(names)
        assert expression is not None
        assert len(expression) < 7728, "production accepted 7,728 chars and rejected 8,828"


class TestTheDigestIdentifiesTheListNotThePeople:
    SALT = "0123456789abcdef"

    def test_it_changes_when_the_list_changes(self) -> None:
        assert exclusions.digest(("a", "b"), self.SALT) != exclusions.digest(
            ("a", "b", "c"), self.SALT
        )

    def test_a_one_name_list_is_not_a_reversible_hash_of_that_name(self) -> None:
        """The case that made a plain digest wrong.

        Excluding a single seller is an ordinary state, and an unkeyed digest of a
        one-element list is exactly sha256(username) — confirmable against a guess by
        anyone who can read the database.
        """
        import hashlib

        keyed = exclusions.digest(("alpha",), self.SALT)
        assert keyed != hashlib.sha256(b"alpha").hexdigest()[:16]
        assert keyed != hashlib.sha256(b"alpha").hexdigest()[:32]

    def test_a_different_deployment_produces_a_different_marker(self) -> None:
        """Which is what makes it unusable as a lookup against a candidate list."""
        assert exclusions.digest(("alpha",), "salt-one") != exclusions.digest(
            ("alpha",), "salt-two"
        )

    def test_a_missing_salt_is_refused_rather_than_defaulted(self) -> None:
        """Defaulting to an empty key would silently restore the plain hash."""
        with pytest.raises(exclusions.ExclusionListError, match="required"):
            exclusions.digest(("alpha",), "")

    def test_it_is_short_enough_for_the_column(self) -> None:
        assert len(exclusions.digest(("alpha", "bravo"), self.SALT)) <= 32


class TestFilterComposition:
    def test_it_merges_with_an_operator_filter(self) -> None:
        assert exclusions.combine("price:[10..20]", ("a", "b")) == (
            "price:[10..20],excludeSellers:{a|b}"
        )

    def test_no_list_leaves_the_filter_untouched(self) -> None:
        assert exclusions.combine("price:[10..20]", ()) == "price:[10..20]"
        assert exclusions.combine(None, ()) is None

    def test_no_operator_filter_gives_the_clause_alone(self) -> None:
        assert exclusions.combine(None, ("a",)) == "excludeSellers:{a}"


class TestTheDatabaseDoorStaysShut:
    """`filter_expr` is a stored column passed to eBay untouched.

    A seller filter typed into it would persist usernames in Postgres, and the three
    seller-column tests could not see it: they look for a column named after a
    seller, and this arrives inside a general-purpose one.
    """

    @pytest.mark.parametrize(
        "expression",
        [
            "excludeSellers:{bob}",
            "price:[1..2],excludeSellers:{bob|alice}",
            "EXCLUDESELLERS:{bob}",
            "sellers:{bob}",
            "price:[1..2], sellers:{bob}",
        ],
    )
    def test_seller_filters_are_refused(self, expression: str) -> None:
        with pytest.raises(exclusions.ExclusionListError, match="cannot be used"):
            exclusions.reject_seller_filters(expression)

    @pytest.mark.parametrize(
        "expression",
        [
            None,
            "",
            "price:[10..400],priceCurrency:USD",
            "conditionIds:{3000},buyingOptions:{FIXED_PRICE}",
            # Must not fire on unrelated fields that merely contain the substring.
            "sellerAccountTypes:{BUSINESS}",
            "excludeCategoryIds:{123}",
        ],
    )
    def test_ordinary_filters_pass(self, expression: str | None) -> None:
        exclusions.reject_seller_filters(expression)


class TestNoNameReachesTheLog:
    """httpx logs the whole request URL at INFO, and the filter travels in it.

    Left alone, every scan would write the exclusion list into container logs several
    times a day — a second, far less controlled copy of the one thing that is
    supposed to live only in a Secret.
    """

    def test_the_filter_is_described_by_shape_not_content(self) -> None:
        names = ("alpha_seller", "bravo_seller", "charlie_seller")
        expression = exclusions.combine("price:[10..20]", names)
        described = _describe_filter(expression)
        for name in names:
            assert name not in described
        assert "3 seller exclusions" in described

    def test_an_ordinary_filter_is_still_summarised(self) -> None:
        assert "chars" in _describe_filter("price:[10..20]")
        assert _describe_filter(None) == "-"

    def test_configure_logging_silences_httpx_request_lines(self) -> None:
        from touchstone.ebay.client import configure_logging

        configure_logging(verbose=False)
        assert logging.getLogger("httpx").level == logging.WARNING, (
            "httpx logs full request URLs, including the exclusion list"
        )


def test_the_environment_is_the_only_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(exclusions.ENV_VAR, "alpha,bravo")
    assert exclusions.from_env() == ("alpha", "bravo")
    monkeypatch.delenv(exclusions.ENV_VAR)
    assert exclusions.from_env() == ()
