"""The extraction pass: fill in ``item_spec`` for titles that lack one.

Runs on its own schedule, from its own entry point, deliberately decoupled from
scanning. A scan records what eBay said; this decides what those words meant. Keeping
them apart is what guarantees a provider outage costs spec coverage and nothing else.

Work is keyed by ``title_hash``, not by listing, so a title shared by two hundred
listings is read once.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from touchstone.db.models import ExtractionMethod, ItemSpec, Listing
from touchstone.extract.llm import UmansExtractor
from touchstone.extract.normalize import normalize_title
from touchstone.extract.specs import LLM_THRESHOLD, SpecCandidate, extract_regex, plausible

log = logging.getLogger("touchstone.extract")


@dataclass
class ExtractionRun:
    considered: int
    by_regex: int
    by_model: int
    unresolved: int

    @property
    def resolved(self) -> int:
        return self.by_regex + self.by_model


def pending_titles(session: Session, limit: int = 500) -> list[tuple[str, str]]:
    """(title_hash, title) for distinct titles with no stored spec.

    ``distinct`` on the hash is the whole cost control: extraction is paid per
    distinct title, and a popular listing template appears hundreds of times.
    """
    stmt = (
        select(Listing.title_hash, Listing.title)
        .outerjoin(ItemSpec, ItemSpec.title_hash == Listing.title_hash)
        .where(ItemSpec.title_hash.is_(None))
        .distinct(Listing.title_hash)
        .limit(limit)
    )
    return [(row[0], row[1]) for row in session.execute(stmt).all()]


def _store(
    session: Session,
    title_hash: str,
    title: str,
    candidate: SpecCandidate,
    method: ExtractionMethod,
    model_id: str | None,
) -> None:
    session.merge(
        ItemSpec(
            title_hash=title_hash,
            normalized_title=normalize_title(title)[:500],
            capacity_per_module_gb=candidate.capacity_per_module_gb,
            module_count=candidate.module_count,
            total_gb=candidate.total_gb,
            ddr_gen=candidate.ddr_gen,
            speed_mt=candidate.speed_mt,
            form_factor=candidate.form_factor,
            rank_org=candidate.rank_org,
            ecc=candidate.ecc,
            registered=candidate.registered,
            method=method,
            confidence=candidate.confidence,
            model_id=model_id,
            extracted_at=datetime.now(UTC),
        )
    )


def run_extraction(
    session: Session,
    extractor: UmansExtractor | None = None,
    *,
    limit: int = 500,
) -> ExtractionRun:
    """Extract specs for pending titles. Safe to run with no extractor configured.

    With ``extractor=None`` only the regex path runs and low-confidence titles are
    left pending — which is the correct degraded behavior, not an error. They will
    be picked up on a later pass once a provider is available.
    """
    pending = pending_titles(session, limit=limit)
    by_regex = 0
    by_model = 0
    unresolved = 0

    for title_hash, title in pending:
        candidate = extract_regex(title)

        if candidate.confidence >= LLM_THRESHOLD and plausible(candidate):
            _store(session, title_hash, title, candidate, ExtractionMethod.REGEX, None)
            by_regex += 1
            continue

        if extractor is None:
            unresolved += 1
            continue

        model_candidate = extractor.extract(title)
        if model_candidate is not None:
            _store(
                session,
                title_hash,
                title,
                model_candidate,
                ExtractionMethod.LLM,
                extractor.model,
            )
            by_model += 1
            continue

        # Neither path could read it. Deliberately store nothing: an absent spec is
        # a known gap, while a stored bad one is a confident wrong answer that would
        # go on to corrupt a cohort.
        unresolved += 1

    session.flush()
    log.info(
        "extraction: %d considered, %d by regex, %d by model, %d unresolved",
        len(pending),
        by_regex,
        by_model,
        unresolved,
    )
    return ExtractionRun(
        considered=len(pending),
        by_regex=by_regex,
        by_model=by_model,
        unresolved=unresolved,
    )


def correct_spec(
    session: Session,
    title_hash: str,
    candidate: SpecCandidate,
    corrected_by: str,
) -> ItemSpec:
    """Record a human correction, which supersedes any model reading permanently.

    A correction is authoritative: it is never re-extracted, and it lifts the
    confidence gate on deal scoring for listings sharing this title, because a
    person has already looked at it.
    """
    if not plausible(candidate):
        raise ValueError("corrected spec fails the range check")

    spec = session.get(ItemSpec, title_hash)
    if spec is None:
        raise ValueError(f"no spec to correct for {title_hash}")

    spec.capacity_per_module_gb = candidate.capacity_per_module_gb
    spec.module_count = candidate.module_count
    spec.total_gb = candidate.total_gb
    spec.ddr_gen = candidate.ddr_gen
    spec.speed_mt = candidate.speed_mt
    spec.form_factor = candidate.form_factor
    spec.rank_org = candidate.rank_org
    spec.ecc = candidate.ecc
    spec.registered = candidate.registered
    spec.method = ExtractionMethod.MANUAL
    spec.confidence = 1.0
    spec.corrected_by = corrected_by
    spec.extracted_at = datetime.now(UTC)
    session.flush()
    return spec
