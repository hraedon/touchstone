"""Deterministic extraction of memory and storage specs from listing titles.

No network, no model. This is the fast path; whatever it cannot parse with
confidence is escalated to ``extract/llm.py``. Per-item (``other``) goods have
no regex path at all — a part number is exactly the thing a regex reads
over-confidently — so they arrive via the model or a human correction.

The lot multiplier is the highest-consequence field in the system. A listing titled
"Lot of 4 x 32GB PC4-2400" at $200 is $1.56/GB; read as one 32GB module it is
$6.25/GB. That 4x error manufactures a bargain that does not exist *and* drags the
cohort statistics every other listing is scored against, so a single bad parse can
produce several false deals. The same arithmetic applies to "Lot of 8 x 3.84TB U.2"
drives. Hence the consistency check and the confidence score that gates deal
flagging.

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

For storage, the parallel trap is stated-in-GB drives: a "480GB SATA SSD" is a
0.48TB drive, not a memory module, and the memory validity set (480 is not a
module capacity) is what keeps the two categories from bleeding into each other.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import assert_never

from touchstone.db.models import SpecCategory
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

# Storage signals. "sata" never appears in a memory title and "ddr"/"dimm" never
# in a storage one, so these keyword sets are the dispatch between the two regex
# paths — and a title matching neither set goes straight to the model.
_STORAGE_SIGNALS = re.compile(r"\b(?:ssd|hdd|nvme|sas|sata|u\.?2|m\.?2|hard\s*drive)\b")
_MEMORY_SIGNALS = re.compile(r"\b(?:ddr\d?|dimm|rdimm|lrdimm|udimm|ecc|ram|pc[345]\b|memory)\b")

# "3.84tb", "1.92 tb", "16tb" — decimal steps are real (3.84, 1.92, 0.96).
_ANY_TB = re.compile(r"\b(\d{1,2}(?:\.\d{1,2})?)\s*tb\b")
_COUNT_X_TB = re.compile(r"\b(\d{1,2})\s*x\s*(\d{1,2}(?:\.\d{1,2})?)\s*tb\b")
_DRIVES_COUNT = re.compile(r"\b(\d{1,2})\s*(?:pcs?|pieces?|drives?|units?|disks?)\b")
_SSD = re.compile(r"\bssd\b|solid\s*state")
_HDD = re.compile(r"\bhdd\b|hard\s*drive|spinning")
_NVME = re.compile(r"\bnvme\b|\bpcie\b")
_SATA = re.compile(r"\bsata\b")
_SAS = re.compile(r"\bsas\b")
_U2 = re.compile(r"\bu\.?2\b")
_M2 = re.compile(r"\bm\.?2\b")
# The quote forms never reach these patterns: normalize_title strips quotes.
_25IN = re.compile(r"\b2\.5\b")
_35IN = re.compile(r"\b3\.5\b")

# A TB capacity outside this band is a misread (a model number caught by the
# decimal pattern, a "1.2TB" that was really a PCIe gen). Marketing steps cluster
# densely enough that a range plus a decimal-places check beats a whitelist.
_MIN_TB, _MAX_TB = 0.1, 80.0


@dataclass(frozen=True)
class SpecCandidate:
    """A proposed spec, with how it was derived and how much to trust it.

    The field set is a union over categories; ``category`` decides which of them
    this candidate is actually claiming.
    """

    category: SpecCategory | None = None

    # memory
    capacity_per_module_gb: int | None = None
    module_count: int | None = None
    total_gb: int | None = None
    ddr_gen: str | None = None
    speed_mt: int | None = None
    form_factor: str | None = None
    rank_org: str | None = None
    ecc: bool | None = None
    registered: bool | None = None

    # storage
    capacity_tb: float | None = None
    storage_type: str | None = None
    storage_iface: str | None = None
    storage_form: str | None = None

    # other
    product_model: str | None = None

    confidence: float = 0.0
    notes: str = ""

    @property
    def usable(self) -> bool:
        """Whether the candidate carries its own category's denominator.

        A memory spec needs GB, a storage spec needs TB, a per-item spec needs
        the model — without the denominator there is nothing to normalize by
        and nothing to cohort on.
        """
        match self.category:
            case None | SpecCategory.MEMORY:
                return self.total_gb is not None and self.total_gb > 0
            case SpecCategory.STORAGE:
                return self.capacity_tb is not None and self.capacity_tb > 0
            case SpecCategory.OTHER:
                return bool(self.product_model and self.product_model.strip())
            case _:
                assert_never(self.category)


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


def looks_like_memory(title: str) -> bool:
    return bool(_MEMORY_SIGNALS.search(normalize_title(title)))


def looks_like_storage(title: str) -> bool:
    return bool(_STORAGE_SIGNALS.search(normalize_title(title)))


def extract_regex(title: str) -> SpecCandidate:
    """Parse a memory title deterministically. Always returns a candidate.

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
        category=SpecCategory.MEMORY,
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


def _tb_capacities(text: str) -> tuple[float | None, int | None, str]:
    """Returns (capacity_tb_per_unit, unit_count, note) for a storage title.

    Mirrors the memory lot discipline: an explicit "8 x 3.84TB" wins, a stated
    lot count times the single stated capacity wins, and anything ambiguous is
    refused rather than guessed at.
    """
    match = _COUNT_X_TB.search(text)
    if match:
        count, per = int(match.group(1)), float(match.group(2))
        if _MIN_TB <= per <= _MAX_TB and 1 <= count <= 64:
            return per, count, "count x capacity"
        return None, None, "count x capacity out of range"

    tbs = [float(m.group(1)) for m in _ANY_TB.finditer(text)]
    valid = [value for value in tbs if _MIN_TB <= value <= _MAX_TB]

    lot = _LOT_COUNT.search(text) or _DRIVES_COUNT.search(text)
    if lot and valid:
        count = int(lot.group(1))
        if 1 <= count <= 64:
            # With a lot count, the smallest stated capacity is the per-unit
            # figure: "Lot of 8 3.84TB (30TB total)" states both.
            return min(valid), count, "lot count x capacity"

    # Stated in GB (480GB, 960GB): convert, and let the range check refuse the
    # ones that are memory-scale rather than drive-scale.
    if not valid:
        gbs = [int(m.group(1)) for m in _ANY_CAP.finditer(text)]
        converted = [round(value / 1000, 2) for value in gbs]
        valid = [value for value in converted if _MIN_TB <= value <= _MAX_TB]
        if valid:
            return valid[0], 1, "stated in gb, converted to tb"

    if len(valid) == 1:
        return valid[0], 1, "single capacity, assumed one unit"

    if valid:
        return None, None, f"ambiguous capacities {sorted(set(valid))}"

    return None, None, "no capacity found"


def _storage_type(text: str) -> str | None:
    if _SSD.search(text):
        return "SSD"
    if _HDD.search(text):
        return "HDD"
    return None


def _storage_iface(text: str) -> str | None:
    # An explicit "nvme" beats everything; bare "pcie" (no protocol named) is
    # treated as NVMe because that is what an enterprise pull title means by it.
    if re.search(r"\bnvme\b", text):
        return "NVMe"
    if _SAS.search(text):
        return "SAS"
    if _SATA.search(text):
        return "SATA"
    if re.search(r"\bpcie\b", text):
        return "NVMe"
    return None


def _storage_form(text: str) -> str | None:
    if _U2.search(text):
        return "U.2"
    if _M2.search(text):
        return "M.2"
    if _35IN.search(text):
        return "3.5"
    if _25IN.search(text):
        return "2.5"
    return None


def extract_storage_regex(title: str) -> SpecCandidate:
    """Parse a storage title deterministically. Always returns a candidate.

    The confidence arithmetic mirrors the memory path: capacity dominates
    because $/TB hangs off it, and the classifying fields only refine the
    cohort.
    """
    text = normalize_title(title)

    per, count, note = _tb_capacities(text)
    storage_type = _storage_type(text)
    iface = _storage_iface(text)
    form = _storage_form(text)

    confidence = 0.0
    if per is not None:
        confidence = 0.55
        if storage_type is not None:
            confidence += 0.15
        if iface is not None:
            confidence += 0.15
        if form is not None:
            confidence += 0.15
    confidence = round(min(confidence, 1.0), 3)

    return SpecCandidate(
        category=SpecCategory.STORAGE,
        module_count=count,
        capacity_tb=per,
        storage_type=storage_type,
        storage_iface=iface,
        storage_form=form,
        confidence=confidence,
        notes=note,
    )


def _plausible_memory(candidate: SpecCandidate) -> bool:
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


def _plausible_storage(candidate: SpecCandidate) -> bool:
    per = candidate.capacity_tb
    count = candidate.module_count
    if per is None:
        # No capacity to be wrong about; the other fields are unconstrained
        # strings that only refine a cohort.
        return count is None or 1 <= count <= 64
    if not _MIN_TB <= per <= _MAX_TB:
        return False
    if count is not None:
        if not 1 <= count <= 64:
            return False
        if per * count > 1024:
            return False
    return True


def _plausible_model(candidate: SpecCandidate) -> bool:
    model = candidate.product_model
    if model is None or not model.strip():
        return False
    return len(model.strip()) <= 120


def plausible(candidate: SpecCandidate) -> bool:
    """Range-check a candidate before it is trusted, by its own category.

    Applied to model output as well as regex output: a model that reads "3200" as a
    capacity must be rejected by arithmetic, not by hoping it does not happen.
    A candidate with no category is checked against the memory ranges — that is
    the shape every legacy caller assumed.
    """
    match candidate.category:
        case None | SpecCategory.MEMORY:
            return _plausible_memory(candidate)
        case SpecCategory.STORAGE:
            return _plausible_storage(candidate)
        case SpecCategory.OTHER:
            return _plausible_model(candidate)
        case _:
            assert_never(candidate.category)
