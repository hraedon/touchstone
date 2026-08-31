"""Correcting the capacity parse.

The single most consequential field touchstone infers is total capacity, because
$/GB divides by it. A fourfold error there both invents a spectacular bargain and
poisons the cohort every other listing is scored against. So correction is a
first-class screen, and the worklist is ordered by how many listings share a title:
an hour spent on a template used by two hundred listings moves two hundred listings.

A correction is authoritative — ``method=manual``, ``confidence=1.0`` — and lifts the
confidence gate on deal scoring for that title, because a person has now looked at it.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Form, Request

from touchstone.extract.runner import correct_spec
from touchstone.extract.specs import SpecCandidate
from touchstone.web import views
from touchstone.web.routes._common import DbSession, flash, not_found, redirect, render

router = APIRouter(prefix="/specs")


def _optional_int(raw: str) -> int | None:
    stripped = raw.strip()
    return int(stripped) if stripped else None


def _tristate(raw: str) -> bool | None:
    """yes / no / unknown.

    Unknown has to be expressible. Forcing a boolean would make an operator assert
    something they cannot see in the title, and a confident wrong attribute is worse
    for cohorting than an absent one.
    """
    match raw.strip().lower():
        case "yes":
            return True
        case "no":
            return False
        case _:
            return None


def _optional_text(raw: str) -> str | None:
    stripped = raw.strip()
    return stripped or None


@router.get("", name="spec_worklist")
def spec_worklist(request: Request, session: DbSession) -> Any:
    return render(request, "specs.html", rows=views.spec_worklist(session))


@router.get("/{title_hash}", name="spec_edit")
def spec_edit(title_hash: str, request: Request, session: DbSession) -> Any:
    spec, title, listing_count = views.spec_detail(session, title_hash)
    if not title:
        raise not_found("title")
    return render(
        request,
        "spec_form.html",
        spec=spec,
        title=title,
        title_hash=title_hash,
        listing_count=listing_count,
        problems=[],
    )


@router.post("/{title_hash}", name="spec_correct")
def spec_correct(
    title_hash: str,
    request: Request,
    session: DbSession,
    capacity_per_module_gb: Annotated[str, Form()] = "",
    module_count: Annotated[str, Form()] = "",
    total_gb: Annotated[str, Form()] = "",
    ddr_gen: Annotated[str, Form()] = "",
    speed_mt: Annotated[str, Form()] = "",
    form_factor: Annotated[str, Form()] = "",
    rank_org: Annotated[str, Form()] = "",
    ecc: Annotated[str, Form()] = "",
    registered: Annotated[str, Form()] = "",
    corrected_by: Annotated[str, Form()] = "",
) -> Any:
    spec, title, listing_count = views.spec_detail(session, title_hash)
    if not title:
        raise not_found("title")

    problems: list[str] = []
    who = corrected_by.strip()
    if not who:
        problems.append("Say who is making the correction; it is recorded on the spec.")

    try:
        candidate = SpecCandidate(
            capacity_per_module_gb=_optional_int(capacity_per_module_gb),
            module_count=_optional_int(module_count),
            total_gb=_optional_int(total_gb),
            ddr_gen=_optional_text(ddr_gen),
            speed_mt=_optional_int(speed_mt),
            form_factor=_optional_text(form_factor),
            rank_org=_optional_text(rank_org),
            ecc=_tristate(ecc),
            registered=_tristate(registered),
            confidence=1.0,
            notes="manual correction",
        )
    except ValueError:
        problems.append("Capacity, module count, total and speed must be whole numbers.")
        candidate = SpecCandidate()

    if not problems:
        try:
            correct_spec(session, title_hash, candidate, corrected_by=who, title=title)
        except ValueError as exc:
            # plausible() refuses e.g. a capacity that is not a real module size, or
            # a per-module x count that does not equal the stated total.
            problems.append(str(exc))

    if problems:
        session.rollback()
        return render(
            request,
            "spec_form.html",
            spec=spec,
            title=title,
            title_hash=title_hash,
            listing_count=listing_count,
            problems=problems,
            status_code=400,
        )

    session.commit()
    flash(
        request,
        f"Spec corrected for {listing_count} listing(s) sharing this title. It is now "
        "authoritative and will not be re-extracted.",
    )
    return redirect(request.url_for("spec_worklist").path)
