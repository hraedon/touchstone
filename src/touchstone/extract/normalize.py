"""Deterministic title normalization and hashing.

This is the *only* part of extraction that runs inside a scan, and it is pure string
handling — no model, no network, no inference. Its job is to collapse titles that
differ only cosmetically onto one key, so that the (Plan 002) extractor pays for each
distinct title once rather than once per listing.

Normalization must stay stable: changing it changes every ``title_hash`` and orphans
the extraction cache. A change here is a migration, not a tweak.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

_WHITESPACE = re.compile(r"\s+")
# Sellers pad titles with separators and decoration that carry no spec information.
# The en/em dashes are deliberate members of the class, not typos for a hyphen.
_NOISE = re.compile(r"[•–—|/\\!*\[\](){}<>~^`\"']+")  # noqa: RUF001


def normalize_title(title: str) -> str:
    """Collapse a listing title to a stable comparison form.

    Deliberately conservative: it lowercases, strips decoration, and normalizes
    whitespace and unicode. It does not reorder tokens, expand abbreviations, or
    correct spelling — those would merge titles that describe different goods, and a
    wrong merge silently corrupts a cohort.
    """
    folded = unicodedata.normalize("NFKC", title).casefold()
    folded = _NOISE.sub(" ", folded)
    folded = folded.replace(",", " ")
    return _WHITESPACE.sub(" ", folded).strip()


def title_hash(title: str) -> str:
    """Stable key for the extraction cache. sha256 of the normalized title."""
    return hashlib.sha256(normalize_title(title).encode("utf-8")).hexdigest()
