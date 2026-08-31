"""Route registration for the touchstone UI."""

from __future__ import annotations

from fastapi import APIRouter

from touchstone.web.routes import dashboard, deals, health, queries, specs, trends, watch

router = APIRouter()
router.include_router(health.router)
router.include_router(dashboard.router)
router.include_router(queries.router)
# Registered after queries so /queries/{id}/trend does not shadow /queries/new.
router.include_router(trends.router)
router.include_router(deals.router)
router.include_router(specs.router)
router.include_router(watch.router)

__all__ = ["router"]
