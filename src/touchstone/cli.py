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
from typing import assert_never

from sqlalchemy import select

from touchstone.db.models import Query, Scan, ScanStatus
from touchstone.db.session import make_engine, make_session_factory
from touchstone.ebay.budget import BudgetGuard, recent_budgets
from touchstone.ebay.client import Credentials, EbayClient
from touchstone.scan.runner import ScanSkipped, run_scan

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
            print(f"{q.id:>4}  {q.name:<30} {state:<9} every {q.cadence_minutes}m  last={last}")
    return 0


def cmd_queries_add(args: argparse.Namespace) -> int:
    factory = make_session_factory(make_engine())
    with factory() as session:
        query = Query(
            name=args.name,
            q=args.q,
            category_ids=args.category_ids,
            filter_expr=args.filter,
            cadence_minutes=args.cadence,
            max_pages=args.max_pages,
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
    print(
        f"scan {result.scan_id}: {result.observed} listings "
        f"({result.new_listings} new), {result.disappearances} disappeared, "
        f"{result.cohorts} cohorts, {result.api_calls} API calls{capped}"
    )
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
        for scan in rows:
            flag = " CAPPED" if scan.capped else ""
            print(
                f"{scan.id:>5}  q={scan.query_id:<4} {scan.started_at.isoformat()}  "
                f"{_describe(scan.status):<16} n={scan.result_count:<6} "
                f"calls={scan.api_calls}{flag}"
            )
            if scan.error:
                print(f"        {scan.error}")
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
    add.set_defaults(func=cmd_queries_add)

    scan = sub.add_parser("scan", help="run one scan now")
    scan.add_argument("--query", required=True, help="query id or name")
    scan.set_defaults(func=cmd_scan)

    scans = sub.add_parser("scans", help="recent scans")
    scans.add_argument("--limit", type=int, default=20)
    scans.set_defaults(func=cmd_scans)

    sub.add_parser("budget", help="remaining API quota").set_defaults(func=cmd_budget)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s %(message)s",
    )
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    sys.exit(main())
