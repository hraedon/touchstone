"""preserve unknown shipping cost

Revision ID: 7ac4e12b9f03
Revises: 59e51e06e599
Create Date: 2026-08-31 00:18:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "7ac4e12b9f03"
down_revision: str | None = "59e51e06e599"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Unknown shipping makes the total unknown rather than equal to item price.

    The old parser materialized missing shipping as zero. If such observations exist,
    their write-once aggregates may also be contaminated and cannot be repaired by a
    nullability change. Refuse rather than silently bless that history. Touchstone's
    first production deployment had no marketplace observations at this point.
    """
    missing_shipping = op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS ("
            "SELECT 1 FROM listing_observation WHERE shipping_cost IS NULL"
            ")"
        )
    )
    if missing_shipping:
        raise RuntimeError(
            "cannot migrate observations that inferred free shipping; "
            "quarantine affected aggregates and deals first"
        )

    op.alter_column(
        "listing_observation",
        "total_cost",
        existing_type=sa.Numeric(precision=12, scale=2),
        nullable=True,
    )
    op.alter_column(
        "listing_disappearance",
        "last_total_cost",
        existing_type=sa.Numeric(precision=12, scale=2),
        nullable=True,
    )


def downgrade() -> None:
    """Requires no observations or disappearances with unknown totals."""
    op.alter_column(
        "listing_disappearance",
        "last_total_cost",
        existing_type=sa.Numeric(precision=12, scale=2),
        nullable=False,
    )
    op.alter_column(
        "listing_observation",
        "total_cost",
        existing_type=sa.Numeric(precision=12, scale=2),
        nullable=False,
    )
