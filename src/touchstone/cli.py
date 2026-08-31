"""Command line entry point.

Enough to drive touchstone without a UI: define queries, run a scan, inspect the
budget. The web face (Plan 003) is an alternative front end over the same calls,
never a privileged one.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from contextlib import nullcontext
from typing import assert_never

from sqlalchemy import select

from touchstone.db.models import Deal, Query, Scan, ScanStatus
from touchstone.db.session import make_engine, make_session_factory
from touchstone.ebay import exclusions
from touchstone.ebay.budget import BudgetGuard, recent_budgets
from touchstone.ebay.client import Credentials, EbayClient, configure_logging
from touchstone.extract.llm import DEFAULT_MODEL, UmansExtractor
from touchstone.extract.runner import run_extraction
from touchstone.scan.retention import DEFAULT_RETENTION_DAYS, prune
from touchstone.scan.runner import ScanSkipped, run_scan
from touchstone.scan.schedule import run_tick, writer_lock

log = logging.getLogger("touchstone")


def _credentials() -> Credentials:
    client_id = os.environ.get("EBAY_CLIENT_ID", "").strip()
    client_secret = os.environ.get("EBAY_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise SystemExit("EBAY_CLIENT_ID and EBAY_CLIENT_SECRET are required")
    return Credentials(client_id, client_secret)


def _describe(status: ScanStatus) -> str:
    """Dispatch over a closed set, with assert_never so a new member cannot be
    silently unhandled at this site."""
    match status:
        case ScanStatus.RUNNING:
            return "running"
        case ScanStatus.COMPLETE:
            return "complete"
        case ScanStatus.FAILED:
            return "FAILED"
        case ScanStatus.SKIPPED_BUDGET:
            return "skipped (budget)"
        case _:
            assert_never(status)


def cmd_queries_list(args: argparse.Namespace) -> int:
    factory = make_session_factory(make_engine())
    with factory() as session:
        rows = session.scalars(select(Query).order_by(Query.name)).all()
        if not rows:
            print("no queries defined")
            return 0
        for q in rows:
            state = "enabled" if q.enabled else "disabled"
            last = q.last_scanned_at.isoformat() if q.last_scanned_at else "never"
            print(
                f"{q.id:>4}  {q.name:<30} {state:<9} every {q.cadence_minutes}m  "
                f"minfb={q.min_seller_feedback}  last={last}"
            )
    return 0


def cmd_queries_add(args: argparse.Namespace) -> int:
    try:
        exclusions.reject_seller_filters(args.filter)
    except exclusions.ExclusionListError as exc:
        raise SystemExit(str(exc)) from exc
    factory = make_session_factory(make_engine())
    with factory() as session:
        query = Query(
            name=args.name,
            q=args.q,
            category_ids=args.category_ids,
            filter_expr=args.filter,
            cadence_minutes=args.cadence,
            max_pages=args.max_pages,
            min_seller_feedback=args.min_feedback,
        )
        session.add(query)
        session.commit()
        print(f"added query {query.id}: {query.name}")
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    factory = make_session_factory(make_engine())
    with factory() as session:
        stmt = select(Query)
        stmt = stmt.where(Query.id == int(args.query)) if args.query.isdigit() else stmt.where(
            Query.name == args.query
        )
        query = session.scalars(stmt).first()
        if query is None:
            raise SystemExit(f"no such query: {args.query}")

        with EbayClient(credentials=_credentials()) as client:
            guard = BudgetGuard(session, client)
            try:
                result = run_scan(session, client, query, budget=guard)
            except ScanSkipped as exc:
                session.commit()  # keep the record of the refusal
                print(f"scan skipped: {exc}")
                return 2
            session.commit()

    capped = "  [CAPPED: result set exceeds 10,000; narrow the filter]" if result.capped else ""
    excluded = (
        f", {result.excluded_low_feedback} excluded (low seller feedback)"
        if result.excluded_low_feedback
        else ""
    )
    print(
        f"scan {result.scan_id}: {result.observed} listings "
        f"({result.new_listings} new){excluded}, {result.disappearances} disappeared, "
        f"{result.cohorts} cohorts, {result.deals} deals, "
        f"{result.api_calls} API calls{capped}"
    )
    return 0


def cmd_tick(args: argparse.Namespace) -> int:
    """Scan everything currently due. The entry point the scanner CronJob runs.

    Exits 0 when another pass already holds the lock: an overlapping run is normal
    operation, not a failure, and a CronJob that reports failure for it would train
    an operator to ignore its alerts.
    """
    engine = make_engine()
    factory = make_session_factory(engine)
    with writer_lock(engine) as acquired, factory() as session:
        if not acquired:
            print("another scanner pass holds the lock; nothing attempted")
            return 0
        with EbayClient(credentials=_credentials()) as client:
            result = run_tick(session, client, limit=args.limit)
        session.commit()

    print(result.summary())
    # A failure inside the pass is reported through the exit code so the CronJob
    # shows it, but only after every other due query has had its turn.
    return 1 if result.failed else 0


def cmd_prune(args: argparse.Namespace) -> int:
    """Drop old observations. Safe only because aggregates are never recomputed.

    Takes the same writer lock the scanner takes, and only when actually deleting.
    A prune that overlapped a scan could delete a listing the scan had just
    re-observed; a dry run reads and changes nothing, so it never needs to wait.
    """
    engine = make_engine()
    factory = make_session_factory(engine)
    applying = bool(args.apply)

    with writer_lock(engine) if applying else nullcontext(True) as acquired:
        if not acquired:
            print(
                "a scanner pass holds the writer lock; nothing was deleted. "
                "Retry when it finishes, or run without --apply to see the plan."
            )
            return 0
        with factory() as session:
            try:
                plan = prune(session, days=args.days, dry_run=not applying)
            except ValueError as exc:
                raise SystemExit(str(exc)) from exc
            if applying:
                session.commit()

    print(plan.summary())
    if plan.dry_run:
        print("dry run: nothing was deleted. Pass --apply to carry this out.")
    return 0


def cmd_budget(args: argparse.Namespace) -> int:
    factory = make_session_factory(make_engine())
    with factory() as session:
        with EbayClient(credentials=_credentials()) as client:
            state = BudgetGuard(session, client).state()
        session.commit()
        source = "eBay (authoritative)" if state.authoritative else "local ledger (fallback)"
        print(f"remaining: {state.remaining} / {state.limit}   usable now: {state.usable}")
        print(f"source:    {source}")
        print()
        for row in recent_budgets(session):
            print(f"  {row.day}  used={row.calls_used:<6} limit={row.calls_limit}")
    return 0


def cmd_scans(args: argparse.Namespace) -> int:
    factory = make_session_factory(make_engine())
    with factory() as session:
        stmt = select(Scan).order_by(Scan.started_at.desc()).limit(args.limit)
        rows = session.scalars(stmt).all()
        if not rows:
            print("no scans yet")
            return 0
        for scan in rows:
            flag = " CAPPED" if scan.capped else ""
            print(
                f"{scan.id:>5}  q={scan.query_id:<4} {scan.started_at.isoformat()}  "
                f"{_describe(scan.status):<16} n={scan.result_count:<6} "
                f"excl={scan.excluded_low_feedback:<5} calls={scan.api_calls}{flag}"
            )
            if scan.error:
                print(f"        {scan.error}")
    return 0


def cmd_extract(args: argparse.Namespace) -> int:
    """Fill in specs for titles that lack one.

    Deliberately a separate command from `scan`: a scan records what eBay said and
    must complete whether or not a model is reachable. This decides what those words
    meant, and is allowed to fail on its own.
    """
    api_key = os.environ.get("UMANS_API_KEY", "").strip()
    model = os.environ.get("TOUCHSTONE_EXTRACT_MODEL", DEFAULT_MODEL).strip()

    factory = make_session_factory(make_engine())
    with factory() as session:
        if not api_key:
            log.warning(
                "UMANS_API_KEY unset: regex only. Titles it cannot read confidently "
                "stay pending rather than being guessed at."
            )
            run = run_extraction(session, extractor=None, limit=args.limit)
        else:
            # A -lab model id raises here rather than being quietly used.
            with UmansExtractor(api_key=api_key, model=model) as extractor:
                run = run_extraction(session, extractor=extractor, limit=args.limit)
        session.commit()

    print(
        f"{run.considered} titles considered: {run.by_regex} by regex, "
        f"{run.by_model} by model, {run.unresolved} unresolved"
    )
    return 0


def cmd_deals(args: argparse.Namespace) -> int:
    factory = make_session_factory(make_engine())
    with factory() as session:
        stmt = select(Deal).order_by(Deal.score.desc()).limit(args.limit)
        if not args.all:
            stmt = stmt.where(Deal.dismissed_at.is_(None))
        rows = session.scalars(stmt).all()
        if not rows:
            print("no deals flagged")
            return 0
        for deal in rows:
            per_gb = f"{float(deal.per_gb):.2f}" if deal.per_gb is not None else "?"
            print(
                f"score={float(deal.score):>6.2f}  ${float(deal.total_cost):>8.2f}  "
                f"${per_gb}/GB  (cohort p10 ${float(deal.cohort_p10):.2f}/GB, "
                f"n={deal.cohort_n})  {deal.listing_id}"
            )
            print(f"        {deal.cohort_key}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="touchstone", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    queries = sub.add_parser("queries", help="manage tracked searches")
    qsub = queries.add_subparsers(dest="subcommand", required=True)
    qsub.add_parser("list").set_defaults(func=cmd_queries_list)

    add = qsub.add_parser("add")
    add.add_argument("name")
    add.add_argument("--q", required=True, help="eBay search keywords")
    add.add_argument("--category-ids")
    add.add_argument("--filter", help="raw eBay Browse filter expression")
    add.add_argument("--cadence", type=int, default=60, help="minutes between scans")
    add.add_argument("--max-pages", type=int, default=5, help="pages per scan (1 API call each)")
    add.add_argument(
        "--min-feedback",
        type=int,
        default=1,
        help="skip sellers below this feedback score (0 disables; default 1 "
        "excludes zero-feedback accounts)",
    )
    add.set_defaults(func=cmd_queries_add)

    scan = sub.add_parser("scan", help="run one scan now")
    scan.add_argument("--query", required=True, help="query id or name")
    scan.set_defaults(func=cmd_scan)

    tick = sub.add_parser("tick", help="scan every query that is due (the scheduler)")
    tick.add_argument(
        "--limit", type=int, default=None, help="at most this many queries this pass"
    )
    tick.set_defaults(func=cmd_tick)

    prune_cmd = sub.add_parser(
        "prune", help="drop observations older than a horizon (dry run by default)"
    )
    prune_cmd.add_argument(
        "--days",
        type=int,
        default=DEFAULT_RETENTION_DAYS,
        help=f"retention horizon in days (default {DEFAULT_RETENTION_DAYS})",
    )
    prune_cmd.add_argument(
        "--apply", action="store_true", help="actually delete; without this it reports only"
    )
    prune_cmd.set_defaults(func=cmd_prune)

    scans = sub.add_parser("scans", help="recent scans")
    scans.add_argument("--limit", type=int, default=20)
    scans.set_defaults(func=cmd_scans)

    extract = sub.add_parser("extract", help="extract specs for unread titles")
    extract.add_argument("--limit", type=int, default=500)
    extract.set_defaults(func=cmd_extract)

    deals = sub.add_parser("deals", help="listings flagged below their cohort")
    deals.add_argument("--limit", type=int, default=25)
    deals.add_argument("--all", action="store_true", help="include dismissed")
    deals.set_defaults(func=cmd_deals)

    sub.add_parser("budget", help="remaining API quota").set_defaults(func=cmd_budget)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(verbose=args.verbose)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    sys.exit(main())
