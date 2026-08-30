"""Deterministic extraction of memory specs from listing titles.

No network, no model. This is the fast path; whatever it cannot parse with
confidence is escalated to ``extract/llm.py``.

The lot multiplier is the highest-consequence field in the system. A listing titled
"Lot of 4 x 32GB PC4-2400" at $200 is $1.56/GB; read as one 32GB module it is
$6.25/GB. That 4x error manufactures a bargain that does not exist *and* drags the
cohort statistics every other listing is scored against, so a single bad parse can
produce several false deals. Hence the consistency check and the confidence score
that gates deal flagging.

Three traps this module exists to survive:

* ``2Rx4`` contains an ``x`` and is a rank/organization code, **not** a multiplier.
  Rank patterns are consumed before any multiplier is looked for. Note honestly:
  removing that strip currently breaks no test, because every multiplier pattern
  also requires a following ``gb`` and the ``r`` in ``2Rx4`` blocks the match on its
  own. The strip is defence in depth against a future loosening of those patterns,
  not the thing presently holding the line. It was described here as load-bearing
  until a mutation showed otherwise.
* ``PC4-2400`` and ``PC4-19200`` denote the same speed in different notations — MT/s
  and peak bandwidth in MB/s respectively (19200 / 8 = 2400).
* ``128GB (4x32GB)`` states a total *and* its breakdown. ``128GB`` alone states only
  a total, and the module count is genuinely unknown rather than 1.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from touchstone.extract.normalize import normalize_title

# Confidence at or above this is trusted without a model.
LLM_THRESHOLD = 0.8

# A listing must clear this to be eligible for deal flagging. A mis-parsed capacity
# produces a spectacular fake bargain, which is the most likely way this system
# embarrasses itself.
DEAL_CONFIDENCE_FLOOR = 0.8

# DDR4 peak-bandwidth notation -> MT/s. Vendors use both forms interchangeably, and
# the arithmetic (bandwidth / 8) does not land on the marketing number, so it is a
# lookup rather than a division.
_BANDWIDTH_TO_MT = {
    12800: 1600,
    14900: 1866,
    17000: 2133,
    19200: 2400,
    21300: 2666,
    21333: 2666,
    23400: 2933,
    25600: 3200,
    28800: 3600,
}

# Plausible per-module capacities. Anything outside this is a misread — most often a
# speed (2400, 3200) mistaken for a capacity.
_VALID_CAPACITIES = {1, 2, 4, 8, 16, 32, 64, 128, 256}

_RANK = re.compile(r"\b([1248])r\s*x\s*(4|8|16)\b")
# The trailing letter is the DDR speed grade (PC4-2400T, PC4-2133P, PC4-2400L).
# Requiring a word boundary straight after the digits silently drops every
# real-world title using that notation, which is most of them.
_PC_FORM = re.compile(r"\bpc(3|4|5)[\s-]*(\d{3,5})[a-z]?\b")
_DDR_SPEED = re.compile(r"\bddr(3|4|5)[\s-]+(\d{3,4})\b")
_MHZ = re.compile(r"\b(\d{3,4})\s*mhz\b")
_DDR_GEN = re.compile(r"\bddr(3|4|5)\b")

# "128gb (4 x 32gb)" / "128gb 4x32gb"
_TOTAL_WITH_BREAKDOWN = re.compile(
    r"\b(\d{1,4})\s*gb\b[^0-9]{0,12}?\(?\s*(\d{1,2})\s*x\s*(\d{1,4})\s*gb\b"
)
# "lot of 4", "qty 4", "4 pcs", "set of 8"
_LOT_COUNT = re.compile(r"\b(?:lot\s+of|qty\.?|quantity|set\s+of)\s*(\d{1,2})\b")
_PCS_COUNT = re.compile(r"\b(\d{1,2})\s*(?:pcs?|pieces?|sticks?|modules?|dimms?)\b")
# "4 x 32gb" with no stated total
_COUNT_X_CAP = re.compile(r"\b(\d{1,2})\s*x\s*(\d{1,4})\s*gb\b")
_ANY_CAP = re.compile(r"\b(\d{1,4})\s*gb\b")


@dataclass(frozen=True)
class SpecCandidate:
    """A proposed spec, with how it was derived and how much to trust it."""

    capacity_per_module_gb: int | None = None
    module_count: int | None = None
    total_gb: int | None = None
    ddr_gen: str | None = None
    speed_mt: int | None = None
    form_factor: str | None = None
    rank_org: str | None = None
    ecc: bool | None = None
    registered: bool | None = None
    confidence: float = 0.0
    notes: str = ""

    @property
    def usable_for_per_gb(self) -> bool:
        return self.total_gb is not None and self.total_gb > 0


def _speed(text: str) -> int | None:
    match = _PC_FORM.search(text)
    if match:
        value = int(match.group(2))
        if value in _BANDWIDTH_TO_MT:
            return _BANDWIDTH_TO_MT[value]
        # PC4-2400 style: already MT/s.
        if 800 <= value <= 8000:
            return value
        return None
    match = _DDR_SPEED.search(text)
    if match:
        return int(match.group(2))
    match = _MHZ.search(text)
    if match:
        return int(match.group(1))
    return None


def _generation(text: str) -> str | None:
    match = _DDR_GEN.search(text)
    if match:
        return f"DDR{match.group(1)}"
    match = _PC_FORM.search(text)
    if match:
        return f"DDR{match.group(1)}"
    return None


def _form_factor(text: str) -> tuple[str | None, bool | None]:
    """Returns (form_factor, registered).

    Order matters: "load reduced" and "lrdimm" must be tested before the generic
    "reg"/"rdimm" patterns, since an LRDIMM is also registered and would otherwise
    be classified as an RDIMM — a different, non-substitutable product.
    """
    if re.search(r"\blr[\s-]?dimm\b|\bload[\s-]?reduced\b|\blrdimm\b", text):
        return "LRDIMM", True
    if re.search(r"\bso[\s-]?dimm\b|\bsodimm\b|\blaptop\b", text):
        return "SODIMM", False
    if re.search(r"\br[\s-]?dimm\b|\brdimm\b|\breg(?:istered)?\b", text):
        return "RDIMM", True
    if re.search(r"\bu[\s-]?dimm\b|\budimm\b|\bunbuffered\b", text):
        return "UDIMM", False
    return None, None


def _ecc(text: str) -> bool | None:
    if re.search(r"\bnon[\s-]?ecc\b", text):
        return False
    if re.search(r"\becc\b", text):
        return True
    return None


def _capacities(text: str) -> tuple[int | None, int | None, int | None, str]:
    """Returns (per_module_gb, module_count, total_gb, note).

    The rank pattern is stripped first so ``2Rx4`` can never be read as a
    multiplier. Belt and braces: the multiplier patterns below also require a
    trailing ``gb``, which already excludes rank codes. Keep both — a later relaxing
    of those patterns would otherwise reintroduce a systematic 2x/4x capacity error
    across every registered DIMM listing, silently.
    """
    stripped = _RANK.sub(" ", text)

    match = _TOTAL_WITH_BREAKDOWN.search(stripped)
    if match:
        total, count, per = int(match.group(1)), int(match.group(2)), int(match.group(3))
        if per in _VALID_CAPACITIES and count * per == total:
            return per, count, total, "total+breakdown, consistent"
        # The title contradicts itself. Trust nothing from it; the model gets a
        # chance, and until then this listing must not be scored.
        return None, None, None, f"total+breakdown inconsistent ({count}x{per} != {total})"

    match = _COUNT_X_CAP.search(stripped)
    if match:
        count, per = int(match.group(1)), int(match.group(2))
        if per in _VALID_CAPACITIES and 1 <= count <= 64:
            return per, count, count * per, "count x capacity"
        return None, None, None, "count x capacity out of range"

    # A stated lot count plus a single capacity elsewhere in the title.
    lot = _LOT_COUNT.search(stripped) or _PCS_COUNT.search(stripped)
    caps = [int(m.group(1)) for m in _ANY_CAP.finditer(stripped)]
    valid_caps = [c for c in caps if c in _VALID_CAPACITIES]

    if lot and valid_caps:
        count = int(lot.group(1))
        # With a lot count, the smallest stated capacity is the per-module figure:
        # a title saying "Lot of 4 32GB (128GB total)" states both, and the larger
        # number is the total.
        per = min(valid_caps)
        if 1 <= count <= 64:
            return per, count, count * per, "lot count x capacity"

    if len(valid_caps) == 1:
        return valid_caps[0], 1, valid_caps[0], "single capacity, assumed one module"

    if valid_caps:
        # Several capacities and no multiplier structure to relate them. Guessing
        # here is exactly how a fake bargain gets manufactured.
        return None, None, None, f"ambiguous capacities {sorted(set(valid_caps))}"

    return None, None, None, "no capacity found"


def extract_regex(title: str) -> SpecCandidate:
    """Parse a title deterministically. Always returns a candidate; check confidence.

    Confidence is built from what was actually recovered, not from how the parse
    felt. A candidate below LLM_THRESHOLD is escalated rather than stored as fact.
    """
    text = normalize_title(title)

    per, count, total, note = _capacities(text)
    speed = _speed(text)
    generation = _generation(text)
    form_factor, registered = _form_factor(text)
    ecc = _ecc(text)

    rank_match = _RANK.search(text)
    rank = f"{rank_match.group(1)}Rx{rank_match.group(2)}" if rank_match else None

    # Capacity is the field $/GB depends on, so it dominates. The rest refine the
    # cohort but cannot rescue a parse that does not know what was being sold.
    confidence = 0.0
    if total is not None:
        confidence = 0.55
        if speed is not None:
            confidence += 0.15
        if generation is not None:
            confidence += 0.10
        if form_factor is not None:
            confidence += 0.10
        if ecc is not None:
            confidence += 0.05
        if rank is not None:
            confidence += 0.05
    confidence = round(min(confidence, 1.0), 3)

    return SpecCandidate(
        capacity_per_module_gb=per,
        module_count=count,
        total_gb=total,
        ddr_gen=generation,
        speed_mt=speed,
        form_factor=form_factor,
        rank_org=rank,
        ecc=ecc,
        registered=registered,
        confidence=confidence,
        notes=note,
    )


def plausible(candidate: SpecCandidate) -> bool:
    """Range-check a candidate before it is trusted.

    Applied to model output as well as regex output: a model that reads "3200" as a
    capacity must be rejected by arithmetic, not by hoping it does not happen.
    """
    per = candidate.capacity_per_module_gb
    count = candidate.module_count
    total = candidate.total_gb

    if per is not None and per not in _VALID_CAPACITIES:
        return False
    if count is not None and not 1 <= count <= 64:
        return False
    if total is not None and not 1 <= total <= 4096:
        return False
    if per is not None and count is not None and total is not None and per * count != total:
        return False
    return not (candidate.speed_mt is not None and not 800 <= candidate.speed_mt <= 8000)
