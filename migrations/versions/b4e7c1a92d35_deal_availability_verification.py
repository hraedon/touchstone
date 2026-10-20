"""deal availability verification columns

Revision ID: b4e7c1a92d35
Revises: 6a392d4d32e4
Create Date: 2026-10-20 12:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "b4e7c1a92d35"
down_revision: str | None = "6a392d4d32e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Record on-request getItem verifications on the deal itself.

    All three columns are nullable and backfill-free: a deal that has never been
    verified simply has none. The left-the-market state is deliberately absent
    from the schema — it is derived at read time from listing_disappearance
    (which retention never prunes), and writing it would turn an inference into
    a fact. See docs/measurement-model.md, "When a deal leaves the feed".
    """
    op.add_column(
        "deal",
        sa.Column("verify_requested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "deal", sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "deal",
        sa.Column(
            "verify_outcome",
            sa.Enum("available", "unavailable", "unknown", native_enum=False, length=32),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("deal", "verify_outcome")
    op.drop_column("deal", "verified_at")
    op.drop_column("deal", "verify_requested_at")
