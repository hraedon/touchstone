"""categories and units

Revision ID: c2f1e4a7b8d3
Revises: b4e7c1a92d35
Create Date: 2026-10-20 20:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "c2f1e4a7b8d3"
down_revision: str | None = "b4e7c1a92d35"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Categories (memory / storage / other) and the per-unit metric.

    This is a rename with a backfill, never a recompute: every per_gb number
    keeps its exact value and gains the unit it was measured in ('gb'), and the
    only new derived value is item_spec.category='memory' for rows that already
    carry memory fields — assigned from recorded data, not re-derived from
    titles. See docs/measurement-model.md, "Categories, units, and cohorts".
    """
    op.add_column(
        "item_spec",
        sa.Column(
            "category",
            sa.Enum("memory", "storage", "other", native_enum=False, length=16),
            nullable=True,
        ),
    )
    op.add_column(
        "item_spec", sa.Column("capacity_tb", sa.Numeric(precision=5, scale=2), nullable=True)
    )
    op.add_column("item_spec", sa.Column("storage_type", sa.String(length=10), nullable=True))
    op.add_column("item_spec", sa.Column("storage_iface", sa.String(length=10), nullable=True))
    op.add_column("item_spec", sa.Column("storage_form", sa.String(length=10), nullable=True))
    op.add_column("item_spec", sa.Column("product_model", sa.String(length=120), nullable=True))
    # Every spec row written before categories exists only if memory extraction
    # succeeded (capacity present), so the backfill is a classification of
    # recorded data, not an inference from titles.
    op.execute("UPDATE item_spec SET category = 'memory' WHERE total_gb IS NOT NULL")

    op.add_column(
        "scan_aggregate",
        sa.Column(
            "unit",
            sa.Enum("gb", "tb", "item", native_enum=False, length=8),
            nullable=True,
        ),
    )
    for suffix in ("min", "p10", "p25", "median", "mean"):
        op.alter_column(
            f"scan_aggregate",
            f"per_gb_{suffix}",
            new_column_name=f"per_unit_{suffix}",
            existing_type=sa.Numeric(precision=12, scale=4),
            existing_nullable=True,
        )
    # The all-or-nothing specced rule means the five per_unit columns are set
    # or null together, so one predicate backfills the unit exactly.
    op.execute("UPDATE scan_aggregate SET unit = 'gb' WHERE per_unit_p10 IS NOT NULL")

    op.add_column(
        "deal",
        sa.Column(
            "unit",
            sa.Enum("gb", "tb", "item", native_enum=False, length=8),
            nullable=True,
        ),
    )
    op.alter_column(
        "deal",
        "per_gb",
        new_column_name="per_unit",
        existing_type=sa.Numeric(precision=12, scale=4),
        existing_nullable=True,
    )
    op.execute("UPDATE deal SET unit = 'gb' WHERE per_unit IS NOT NULL")


def downgrade() -> None:
    """Loses the category classification and unit labels (values are kept)."""
    op.drop_column("deal", "unit")
    op.alter_column(
        "deal",
        "per_unit",
        new_column_name="per_gb",
        existing_type=sa.Numeric(precision=12, scale=4),
        existing_nullable=True,
    )
    for suffix in ("min", "p10", "p25", "median", "mean"):
        op.alter_column(
            "scan_aggregate",
            f"per_unit_{suffix}",
            new_column_name=f"per_gb_{suffix}",
            existing_type=sa.Numeric(precision=12, scale=4),
            existing_nullable=True,
        )
    op.drop_column("scan_aggregate", "unit")
    op.drop_column("item_spec", "product_model")
    op.drop_column("item_spec", "storage_form")
    op.drop_column("item_spec", "storage_iface")
    op.drop_column("item_spec", "storage_type")
    op.drop_column("item_spec", "capacity_tb")
    op.drop_column("item_spec", "category")
