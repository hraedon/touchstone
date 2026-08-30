"""Test fixtures.

Tests run against a **real Postgres**, not SQLite. Two reasons, both learned the
hard way elsewhere in this estate:

* The schema uses Postgres types and constraint behavior. A green SQLite suite says
  nothing about whether the real schema works.
* ``create_all`` without ``drop_all`` on in-memory SQLite hides cross-test bleed —
  state leaks between tests and the suite passes anyway.

If ``TOUCHSTONE_TEST_DSN`` is set it is used. Otherwise a throwaway container is
started for the session and removed afterwards. If neither is possible the suite
**fails** rather than skipping: a skipped database test is a check whose failure mode
is silence, which is how a broken schema stays green.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session

from touchstone.db.models import Base

POSTGRES_IMAGE = "postgres:16"
CONTAINER_NAME = "touchstone-test-pg"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def _wait_ready(dsn: str, timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    last: Exception | None = None
    while time.time() < deadline:
        try:
            engine = create_engine(dsn)
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            engine.dispose()
            return
        except Exception as exc:
            last = exc
            time.sleep(0.5)
    raise RuntimeError(f"test Postgres never became ready: {last}")


@pytest.fixture(scope="session")
def dsn() -> Iterator[str]:
    configured = os.environ.get("TOUCHSTONE_TEST_DSN", "").strip()
    if configured:
        _wait_ready(configured)
        yield configured
        return

    if subprocess.run(["which", "docker"], capture_output=True).returncode != 0:
        pytest.fail(
            "No TOUCHSTONE_TEST_DSN and docker is unavailable. These tests need a "
            "real Postgres; skipping them would hide schema breakage."
        )

    port = _free_port()
    subprocess.run(["docker", "rm", "-f", CONTAINER_NAME], capture_output=True, check=False)
    started = subprocess.run(
        [
            "docker", "run", "-d", "--rm",
            "--name", CONTAINER_NAME,
            "-e", "POSTGRES_PASSWORD=touchstone",
            "-e", "POSTGRES_USER=touchstone",
            "-e", "POSTGRES_DB=touchstone_test",
            "-p", f"127.0.0.1:{port}:5432",
            POSTGRES_IMAGE,
        ],
        capture_output=True,
        text=True,
    )
    if started.returncode != 0:
        pytest.fail(f"could not start test Postgres: {started.stderr}")

    url = f"postgresql+psycopg://touchstone:touchstone@127.0.0.1:{port}/touchstone_test"
    try:
        _wait_ready(url)
        yield url
    finally:
        subprocess.run(["docker", "rm", "-f", CONTAINER_NAME], capture_output=True, check=False)


@pytest.fixture(scope="session")
def engine(dsn: str) -> Iterator[Engine]:
    eng = create_engine(dsn, future=True)
    Base.metadata.drop_all(eng)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """A session whose work is rolled back, so tests cannot bleed into each other."""
    connection = engine.connect()
    transaction = connection.begin()
    sess = Session(bind=connection, expire_on_commit=False, future=True)
    try:
        yield sess
    finally:
        sess.close()
        transaction.rollback()
        connection.close()
