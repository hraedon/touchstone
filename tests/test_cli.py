"""CLI safeguards that are not covered by the scanner's unit tests."""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from tests.fake_ebay import FakeEbay, Generation, item
from touchstone import cli
from touchstone.db.models import Query, RateBudget, Scan, ScanStatus
from touchstone.ebay.budget import today_utc
from touchstone.ebay.client import EbayClient, EbayError


def _patch_manual_command(
    session: Session,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    url: str,
    *,
    acquired: bool,
) -> list[bool]:
    lock_calls: list[bool] = []

    @contextmanager
    def tracking_lock(_: Engine) -> Iterator[bool]:
        lock_calls.append(acquired)
        yield acquired

    monkeypatch.setattr(cli, "make_engine", lambda: engine)
    monkeypatch.setattr(
        cli,
        "make_session_factory",
        lambda _: sessionmaker(
            bind=session.get_bind(),
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        ),
    )
    monkeypatch.setattr(cli, "writer_lock", tracking_lock)
    monkeypatch.setattr(
        cli,
        "EbayClient",
        lambda credentials: EbayClient(credentials=credentials, base_url=url),
    )
    monkeypatch.setenv("EBAY_CLIENT_ID", "id")
    monkeypatch.setenv("EBAY_CLIENT_SECRET", "secret")
    return lock_calls


def _query(session: Session, name: str) -> Query:
    query = Query(name=name, q="ecc ddr4", max_pages=1)
    session.add(query)
    session.flush()
    return query


def test_manual_scan_takes_the_writer_lock(
    session: Session,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query = _query(session, "manual")
    fake = FakeEbay(generations=[Generation(items=[item("v1|1|0", price=100.0)])])
    url = fake.start()
    lock_calls = _patch_manual_command(session, engine, monkeypatch, url, acquired=True)

    try:
        assert cli.cmd_scan(argparse.Namespace(query=str(query.id))) == 0
    finally:
        fake.stop()

    assert lock_calls == [True]
    assert fake.search_calls == 1


def test_manual_scan_commits_failed_spend(
    session: Session,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query = _query(session, "manual-failure")
    fake = FakeEbay(
        generations=[Generation(items=[])],
        rate_limit_fails=True,
        search_http_failures={1: 500},
    )
    url = fake.start()
    _patch_manual_command(session, engine, monkeypatch, url, acquired=True)

    try:
        with pytest.raises(EbayError):
            cli.cmd_scan(argparse.Namespace(query=str(query.id)))
    finally:
        fake.stop()

    session.expire_all()
    scan = session.scalars(select(Scan).where(Scan.query_id == query.id)).one()
    assert scan.status is ScanStatus.FAILED
    assert scan.api_calls == 1
    row = session.get(RateBudget, today_utc())
    assert row is not None
    assert row.calls_used == 1


def test_manual_scan_does_nothing_when_the_lock_is_contended(
    session: Session,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query = _query(session, "manual-contended")
    fake = FakeEbay(generations=[Generation(items=[item("v1|1|0", price=100.0)])])
    url = fake.start()
    lock_calls = _patch_manual_command(session, engine, monkeypatch, url, acquired=False)

    try:
        assert cli.cmd_scan(argparse.Namespace(query=str(query.id))) == 0
    finally:
        fake.stop()

    assert lock_calls == [False]
    assert fake.token_mints == 0
    assert fake.search_calls == 0
    assert session.scalars(select(Scan).where(Scan.query_id == query.id)).all() == []
