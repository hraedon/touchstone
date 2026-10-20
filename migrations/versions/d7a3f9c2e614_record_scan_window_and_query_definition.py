"""Record whether a scan saw its whole result set, and which query definition it ran.

A scan that stops at its page budget before eBay's total has seen a best-match
window, not the population, and until now nothing said so: only the 10,000-item
ceiling was recorded, so a window scan was diffed as if it were whole and items
crossing the window's edge were written as disappearances. Editing a query's
keywords or categories had the same effect with no record at all.

Both columns are left NULL on existing rows. Whether an old scan was truncated
cannot be known after the fact (eBay's reported total was never stored), and a
backfill would be an inference written into the record as if observed. NULL is
read as "not known to be whole", so each query skips exactly one diff after
this migration and none thereafter.

Revision ID: d7a3f9c2e614
Revises: c2f1e4a7b8d3
Create Date: 2026-10-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d7a3f9c2e614"
down_revision: str | None = "c2f1e4a7b8d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("scan", sa.Column("truncated", sa.Boolean(), nullable=True))
    op.add_column("scan", sa.Column("query_signature", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("scan", "query_signature")
    op.drop_column("scan", "truncated")
