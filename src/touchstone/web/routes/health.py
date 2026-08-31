"""Liveness and readiness, split on purpose.

They answer different questions and conflating them makes a database blip restart
every pod in a loop, which turns a brief outage into a longer one.

``/livez``  — is this process able to serve? No I/O, no database.
``/readyz`` — should traffic be sent here *now*? Checks the database, because every
              page needs it and a pod that cannot reach Postgres has nothing to show.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Response
from sqlalchemy import text

from touchstone.web.routes._common import DbSession

log = logging.getLogger("touchstone.web.health")

router = APIRouter()


@router.get("/livez", name="livez")
def livez() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", name="readyz")
def readyz(session: DbSession, response: Response) -> dict[str, Any]:
    """503 when the database is unreachable, so the pod leaves the load balancer.

    Deliberately not a liveness check: an unreachable database is a reason to stop
    receiving requests, not a reason to be killed and restarted.
    """
    try:
        session.execute(text("SELECT 1"))
    except Exception:
        log.warning("readiness check failed: the database did not answer")
        response.status_code = 503
        return {"status": "unavailable", "database": "unreachable"}
    return {"status": "ok", "database": "ok"}
