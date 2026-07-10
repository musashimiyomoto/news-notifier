"""scrape failures

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-10

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "scrape_failures",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "market_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("markets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("source_domain", sa.String(255), nullable=False),
        sa.Column("reason", sa.String(50), nullable=False),
        sa.Column("detail", sa.Text, nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("ix_scrape_failures_market_id", "scrape_failures", ["market_id"])
    op.create_index("ix_scrape_failures_source_domain", "scrape_failures", ["source_domain"])


def downgrade() -> None:
    op.drop_table("scrape_failures")
