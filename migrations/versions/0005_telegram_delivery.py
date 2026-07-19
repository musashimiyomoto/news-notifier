"""track per-channel delivery progress

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-20

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "delivery_log",
        sa.Column("webhook_delivered", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "delivery_log",
        sa.Column(
            "telegram_sent_news_item_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
    )


def downgrade() -> None:
    op.drop_column("delivery_log", "telegram_sent_news_item_ids")
    op.drop_column("delivery_log", "webhook_delivered")
