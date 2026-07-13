"""news batches

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-10

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    batch_status = postgresql.ENUM("open", "closed", name="batch_status", create_type=False)
    bind = op.get_bind()
    batch_status.create(bind, checkfirst=True)

    op.create_table(
        "news_batches",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "market_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("markets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("expected_count", sa.Integer, nullable=False),
        sa.Column("resolved_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("status", batch_status, nullable=False, server_default="open"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("ix_news_batches_market_id", "news_batches", ["market_id"])
    # Backs the enqueue_stuck_batches sweep (status='open' AND created_at <= cutoff).
    op.create_index("ix_news_batches_status_created_at", "news_batches", ["status", "created_at"])

    op.add_column(
        "news_items",
        sa.Column(
            "batch_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("news_batches.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_news_items_batch_id", "news_items", ["batch_id"])

    op.create_table(
        "news_batch_candidates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "batch_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("news_batches.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("canonical_url_hash", sa.String(64), nullable=False),
        sa.Column("outcome", sa.String(50), nullable=False),
        sa.Column(
            "news_item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("news_items.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.UniqueConstraint("batch_id", "canonical_url_hash", name="uq_batch_candidate"),
    )
    op.create_index("ix_news_batch_candidates_batch_id", "news_batch_candidates", ["batch_id"])


def downgrade() -> None:
    op.drop_table("news_batch_candidates")
    op.drop_index("ix_news_items_batch_id", table_name="news_items")
    op.drop_column("news_items", "batch_id")
    op.drop_table("news_batches")

    bind = op.get_bind()
    postgresql.ENUM(name="batch_status").drop(bind, checkfirst=True)
