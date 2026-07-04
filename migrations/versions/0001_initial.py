"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-07-03

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Must match app.db.models.EMBEDDING_DIM / the embedding model's output size
# (nomic-embed-text = 768). Changing embedding models later means a new
# migration to alter this column's dimension.
EMBEDDING_DIM = 768


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # create_type=False: these are created explicitly below via .create(checkfirst=True).
    # Without it, op.create_table's own DDL visitor tries to CREATE TYPE again
    # (without checking first) as soon as the enum is used as a column type,
    # raising DuplicateObjectError against the type we just created.
    market_status = postgresql.ENUM("active", "paused", "resolved", name="market_status", create_type=False)
    reliability_tier = postgresql.ENUM(
        "tier1_official",
        "tier2_major_media",
        "tier3_aggregator",
        "tier4_social_blog",
        "unknown",
        name="reliability_tier",
        create_type=False,
    )
    impact_hint = postgresql.ENUM(
        "supports_yes", "supports_no", "neutral", "ambiguous", name="impact_hint", create_type=False
    )
    delivery_status = postgresql.ENUM(
        "pending", "success", "failed", "dead_letter", name="delivery_status", create_type=False
    )

    bind = op.get_bind()
    market_status.create(bind, checkfirst=True)
    reliability_tier.create(bind, checkfirst=True)
    impact_hint.create(bind, checkfirst=True)
    delivery_status.create(bind, checkfirst=True)

    op.create_table(
        "markets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("external_market_id", sa.String(255), nullable=False, unique=True),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("resolution_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", market_status, nullable=False, server_default="active"),
        sa.Column("callback_url", sa.String(2048), nullable=False),
        sa.Column("callback_secret_encrypted", sa.String(512), nullable=False),
        sa.Column("poll_interval_minutes", sa.Integer, nullable=False, server_default="1440"),
        sa.Column("next_poll_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_polled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("ix_markets_external_market_id", "markets", ["external_market_id"])
    op.create_index("ix_markets_next_poll_at", "markets", ["next_poll_at"])

    op.create_table(
        "news_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "market_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("markets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("canonical_url_hash", sa.String(64), nullable=False),
        sa.Column("title_simhash", sa.BigInteger, nullable=True),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("summary", sa.Text, nullable=False),
        sa.Column("proofs", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("source_domain", sa.String(255), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("discovered_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("credibility_score", sa.Float, nullable=False),
        sa.Column("relevance_score", sa.Float, nullable=False),
        sa.Column("impact_hint", impact_hint, nullable=False, server_default="neutral"),
        sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=True),
        sa.Column(
            "related_news_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("news_items.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("delivered", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.UniqueConstraint("market_id", "canonical_url_hash", name="uq_news_market_url"),
    )
    op.create_index("ix_news_items_market_id", "news_items", ["market_id"])
    op.create_index("ix_news_items_canonical_url_hash", "news_items", ["canonical_url_hash"])
    op.create_index("ix_news_items_source_domain", "news_items", ["source_domain"])
    # HNSW: no training/pre-existing-data requirement (unlike ivfflat), fine to
    # create up front on an empty table.
    op.execute(
        "CREATE INDEX ix_news_items_embedding ON news_items "
        "USING hnsw (embedding vector_cosine_ops)"
    )

    op.create_table(
        "sources",
        sa.Column("domain", sa.String(255), primary_key=True),
        sa.Column("reliability_tier", reliability_tier, nullable=False, server_default="unknown"),
        sa.Column("reliability_score", sa.Float, nullable=False, server_default="0.5"),
        sa.Column("category", sa.String(100), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )

    op.create_table(
        "delivery_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "market_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("markets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("news_item_ids", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("attempt", sa.Integer, nullable=False, server_default="0"),
        sa.Column("status", delivery_status, nullable=False, server_default="pending"),
        sa.Column("status_code", sa.Integer, nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_delivery_log_market_id", "delivery_log", ["market_id"])
    op.create_index("ix_delivery_log_batch_id", "delivery_log", ["batch_id"])


def downgrade() -> None:
    op.drop_table("delivery_log")
    op.drop_table("sources")
    op.execute("DROP INDEX IF EXISTS ix_news_items_embedding")
    op.drop_table("news_items")
    op.drop_table("markets")

    bind = op.get_bind()
    postgresql.ENUM(name="delivery_status").drop(bind, checkfirst=True)
    postgresql.ENUM(name="impact_hint").drop(bind, checkfirst=True)
    postgresql.ENUM(name="reliability_tier").drop(bind, checkfirst=True)
    postgresql.ENUM(name="market_status").drop(bind, checkfirst=True)
