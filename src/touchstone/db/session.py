"""Engine and session construction.

The DSN comes from the environment only. There is no default pointing at a real
host: a misconfigured deploy must fail loudly rather than quietly write somewhere
unexpected.
"""

from __future__ import annotations

import os

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

_DSN_ENV = "TOUCHSTONE_DSN"


def dsn() -> str:
    value = os.environ.get(_DSN_ENV, "").strip()
    if not value:
        raise RuntimeError(f"{_DSN_ENV} is required (postgresql+psycopg://user:pass@host/db)")
    return value


def make_engine(url: str | None = None, *, echo: bool = False) -> Engine:
    return create_engine(url or dsn(), echo=echo, pool_pre_ping=True, future=True)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)
