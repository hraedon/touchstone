"""The operator's seller exclusion list.

A hand-authored list of eBay usernames whose listings touchstone should never
retrieve. It is deliberately unlike every other piece of seller-related data here,
and the difference is what makes holding it defensible:

* **It was not derived from eBay.** It is the operator's own judgement, typed by
  hand. Nothing in touchstone may add to it — see ``docs/deletion-compliance.md``.
  If this list ever grew automatically from observed API data it would become data
  *about* those sellers derived from the marketplace, and the argument for keeping it
  would collapse.
* **It exists to prevent collection, not to enable it.** Every name here is a
  seller whose listings touchstone will not fetch, store, aggregate, or score.
  Deleting the list would cause touchstone to start collecting their data again,
  which is the opposite of what an erasure request asks for.
* **The name is the entire holding.** No feedback score, no listing, no observation,
  no derived statistic. Nothing is recorded about these sellers anywhere.

It lives in the environment (a Kubernetes Secret), never in the database. The scanner
composes it into the outgoing Browse filter at call time so eBay does the exclusion
server-side — which also means those listings are never paged through, never parsed,
and never reach the database layer at all.

Changing the list changes which sellers are sampled, exactly as
``Query.min_seller_feedback`` does, so each scan records **how many** names were in
force and a **digest of the list as a whole**. Never a per-name hash: a digest of one
username is still an identifier of one person, individually linkable, and would be a
worse thing to store than the name. A digest over the whole list is a version marker
and identifies nobody.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re

ENV_VAR = "TOUCHSTONE_EXCLUDED_SELLERS"
SALT_VAR = "TOUCHSTONE_EXCLUSION_SALT"

# eBay accepted a 351-name filter (7,728 characters) and rejected 401 names (8,828)
# with an HTTP 400 from the edge — loudly, never by silently dropping the exclusions.
# Measured against production 2026-08-31. This ceiling sits well inside that, so the
# request is refused here, where the cause is legible, rather than at the edge.
MAX_EXCLUDED_SELLERS = 200

# eBay usernames are ASCII, 6-64 characters. Validated so a stray comment, quote, or
# brace in the Secret cannot alter the meaning of the filter expression it is
# interpolated into.
_USERNAME = re.compile(r"\A[A-Za-z0-9._*-]{1,64}\Z")


class ExclusionListError(ValueError):
    """The configured exclusion list cannot be used as-is."""


def parse(raw: str | None) -> tuple[str, ...]:
    """Read the list from its configured form: names separated by commas or newlines.

    Order-insensitive and de-duplicated, so that reordering the Secret does not read
    as a change to the sampled population.
    """
    if not raw or not raw.strip():
        return ()

    names: list[str] = []
    for chunk in re.split(r"[,\n\r]+", raw):
        name = chunk.strip()
        if not name or name.startswith("#"):
            continue
        if _USERNAME.fullmatch(name) is None:
            raise ExclusionListError(
                f"{ENV_VAR} contains an entry that is not a plausible eBay username "
                f"({len(name)} characters). Entries are comma- or newline-separated; "
                "'#' starts a comment."
            )
        names.append(name)

    unique = tuple(sorted(set(names), key=str.casefold))
    if len(unique) > MAX_EXCLUDED_SELLERS:
        raise ExclusionListError(
            f"{ENV_VAR} holds {len(unique)} names; the limit is "
            f"{MAX_EXCLUDED_SELLERS}. eBay rejects an over-long filter outright, so "
            "a larger list would stop every scan rather than quietly filtering less."
        )
    return unique


def from_env(environ: dict[str, str] | None = None) -> tuple[str, ...]:
    source = os.environ if environ is None else environ
    return parse(source.get(ENV_VAR))


def digest(names: tuple[str, ...], salt: str | None = None) -> str:
    """A version marker for the list, keyed so it identifies nobody.

    An unkeyed hash would not do. Over a list of one — a completely ordinary state,
    since you start by excluding a single bad seller — a plain digest *is*
    ``sha256(username)``, which anyone who can read the database can confirm against
    a guessed name. That would be storing a reversible pseudonymised identifier in
    the very table the design says holds none, and it would look safe while doing it.

    So this is an HMAC under a salt that lives beside the list in the same Secret.
    The database then carries a value that changes when the list changes and reveals
    nothing about its contents to anyone who cannot already read the list itself.
    """
    if not names:
        return ""
    key = (salt if salt is not None else os.environ.get(SALT_VAR, "")).encode("utf-8")
    if not key:
        raise ExclusionListError(
            f"{SALT_VAR} is required whenever {ENV_VAR} is set. Without it the "
            "recorded digest of a short list would be a reversible hash of the "
            "usernames in it. Generate one once: `openssl rand -hex 16`."
        )
    joined = "\n".join(sorted(names, key=str.casefold))
    return hmac.new(key, joined.encode("utf-8"), hashlib.sha256).hexdigest()[:16]


def as_filter(names: tuple[str, ...]) -> str | None:
    """The eBay Browse ``excludeSellers`` clause, or None when the list is empty."""
    if not names:
        return None
    joined = "|".join(names)
    return f"excludeSellers:{{{joined}}}"


def combine(filter_expr: str | None, names: tuple[str, ...]) -> str | None:
    """Merge the operator's own filter with the exclusion clause."""
    clause = as_filter(names)
    if clause is None:
        return filter_expr or None
    if not filter_expr:
        return clause
    return f"{filter_expr},{clause}"


# Filters that would put a seller identifier into the database if a user typed one
# into a query's stored `filter_expr`. The exclusion list is held in the environment
# precisely so no name reaches Postgres; this keeps the other door shut.
FORBIDDEN_IN_STORED_FILTER = ("excludesellers", "sellers")


def reject_seller_filters(filter_expr: str | None) -> None:
    """Refuse a stored filter that names sellers.

    ``Query.filter_expr`` is passed to eBay untouched and is a *stored column*, so a
    seller filter typed into it would persist usernames in the database — which the
    schema, the tests, and `docs/deletion-compliance.md` all say never happens. The
    seller-column tests cannot catch this: they look for a column named after a
    seller, and this would arrive inside a general-purpose one.
    """
    if not filter_expr:
        return
    lowered = filter_expr.lower()
    for field in FORBIDDEN_IN_STORED_FILTER:
        if re.search(rf"(?:\A|[,&\s]){re.escape(field)}\s*:", lowered):
            raise ExclusionListError(
                f"'{field}' cannot be used in a query's filter: it would store eBay "
                f"seller usernames in the database. Set {ENV_VAR} instead — the "
                "scanner applies it at call time and it is never persisted."
            )
