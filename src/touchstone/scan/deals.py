"""Flagging listings priced below their cohort.

Conservative by construction. The reference distribution is built from *active*
asking prices, which are biased upward — overpriced inventory does not sell, so it
accumulates in the only pool we can see. "Below the 10th percentile of asking" is
therefore a weaker claim than "below market", and errs toward not flagging.

That is the correct direction for the error: a missed deal costs nothing, a false
deal costs a purchase.
"""

from __future__ import annotations

from dataclasses import dataclass

from touchstone.extract.specs import DEAL_CONFIDENCE_FLOOR

# A percentile over three listings is noise, not a distribution.
MIN_COHORT_N = 5

# How far below p10 a listing must sit, in units of the cohort's own spread.
#
# This threshold is not decoration. In any cohort of two or more, the cheapest
# listing is *always* below an interpolated p10 — that is arithmetic, not a signal.
# Flagging on "below p10" alone therefore marks the cheapest item in every cohort on
# every scan, which is both useless and actively misleading: it dresses up the bottom
# of a normal distribution as a find. A deal has to be anomalously cheap, not merely
# cheapest, so it must clear a full spread-unit below the tenth percentile.
MIN_SCORE = 1.0


@dataclass(frozen=True)
class DealCandidate:
    listing_id: str
    cohort_key: str
    per_gb: float
    cohort_p10: float
    cohort_n: int
    score: float


def score(per_gb: float, cohort_p10: float, cohort_median: float) -> float:
    """How far below p10, in units of the cohort's own spread.

    Normalizing by spread rather than by absolute dollars makes scores comparable
    between a cheap cohort and an expensive one — a $2 gap means something different
    at $3/GB than at $30/GB.
    """
    # A degenerate cohort (every listing at the same price) has no spread to divide
    # by; fall back to a fraction of the level so the score stays finite.
    spread = max(cohort_median - cohort_p10, cohort_p10 * 0.05, 1e-6)
    return (cohort_p10 - per_gb) / spread


def evaluate(
    *,
    listing_id: str,
    cohort_key: str,
    per_gb: float | None,
    cohort_p10: float | None,
    cohort_median: float | None,
    cohort_n: int,
    confidence: float | None,
    manual: bool = False,
) -> DealCandidate | None:
    """Decide whether one listing is worth flagging.

    Every gate here exists because failing it produces a *confident wrong answer*
    rather than a missing one:

    * no $/GB — nothing to compare;
    * thin cohort — a p10 over four listings is an artifact;
    * merely-cheapest — see MIN_SCORE; being the lowest in a cohort is arithmetic,
      not evidence;
    * low spec confidence — a mis-parsed capacity produces a spectacular fake
      bargain, and that is the single most likely way this system embarrasses
      itself. A human correction (``manual``) overrides the score gate, since a
      person has already looked.
    """
    if per_gb is None or cohort_p10 is None or cohort_median is None:
        return None
    if cohort_n < MIN_COHORT_N:
        return None
    if not manual and (confidence is None or confidence < DEAL_CONFIDENCE_FLOOR):
        return None
    if per_gb >= cohort_p10:
        return None

    value = score(per_gb, cohort_p10, cohort_median)
    if value < MIN_SCORE:
        return None

    return DealCandidate(
        listing_id=listing_id,
        cohort_key=cohort_key,
        per_gb=per_gb,
        cohort_p10=cohort_p10,
        cohort_n=cohort_n,
        score=round(value, 3),
    )
