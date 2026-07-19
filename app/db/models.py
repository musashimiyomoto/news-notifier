import enum
import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

EMBEDDING_DIM = 384  # must match app.config.Settings.embedding_dim / the FastEmbed model used


class Base(DeclarativeBase):
    pass


class MarketStatus(str, enum.Enum):
    active = "active"
    paused = "paused"
    resolved = "resolved"


class ReliabilityTier(str, enum.Enum):
    tier1_official = "tier1_official"
    tier2_major_media = "tier2_major_media"
    tier3_aggregator = "tier3_aggregator"
    tier4_social_blog = "tier4_social_blog"
    unknown = "unknown"


class ImpactHint(str, enum.Enum):
    supports_yes = "supports_yes"
    supports_no = "supports_no"
    neutral = "neutral"
    ambiguous = "ambiguous"


class DeliveryStatus(str, enum.Enum):
    pending = "pending"
    success = "success"
    failed = "failed"
    dead_letter = "dead_letter"


class BatchStatus(str, enum.Enum):
    open = "open"
    closed = "closed"


class Market(Base):
    __tablename__ = "markets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    external_market_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    description: Mapped[str] = mapped_column(Text)
    resolution_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[MarketStatus] = mapped_column(Enum(MarketStatus, name="market_status"), default=MarketStatus.active)

    callback_url: Mapped[str] = mapped_column(String(2048))
    callback_secret_encrypted: Mapped[str] = mapped_column(String(512))

    poll_interval_minutes: Mapped[int] = mapped_column(Integer, default=24 * 60)
    next_poll_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # High-water mark: the latest published_at ever included in a successfully
    # delivered webhook for this market. process_market filters candidates
    # older than this out of every subsequent cycle, so a late-indexed article
    # (search sources surface it days after publication) can't land in a batch
    # sent after a batch with newer articles already went out — see
    # app.worker.tasks._filter_older_than_watermark.
    max_delivered_published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    news_items: Mapped[list["NewsItem"]] = relationship(back_populates="market", cascade="all, delete-orphan")


class NewsBatch(Base):
    """One row per process_market fan-out cycle that produced >=1 candidate.
    resolved_count/expected_count + status=open->closed is the single gate that
    decides who gets to build this batch's DeliveryLog — see
    app.worker.batching.resolve_batch_candidate / force_close_batch."""

    __tablename__ = "news_batches"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    market_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("markets.id", ondelete="CASCADE"), index=True)
    expected_count: Mapped[int] = mapped_column(Integer)
    resolved_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[BatchStatus] = mapped_column(Enum(BatchStatus, name="batch_status"), default=BatchStatus.open)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class NewsItem(Base):
    __tablename__ = "news_items"
    __table_args__ = (UniqueConstraint("market_id", "canonical_url_hash", name="uq_news_market_url"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    market_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("markets.id", ondelete="CASCADE"), index=True)
    batch_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("news_batches.id", ondelete="SET NULL"), nullable=True, index=True
    )

    url: Mapped[str] = mapped_column(Text)
    canonical_url_hash: Mapped[str] = mapped_column(String(64), index=True)
    title_simhash: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text)
    proofs: Mapped[list[dict]] = mapped_column(JSONB, default=list)

    source_domain: Mapped[str] = mapped_column(String(255), index=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    credibility_score: Mapped[float] = mapped_column(Float)
    relevance_score: Mapped[float] = mapped_column(Float)
    impact_hint: Mapped[ImpactHint] = mapped_column(Enum(ImpactHint, name="impact_hint"), default=ImpactHint.neutral)

    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)
    related_news_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("news_items.id", ondelete="SET NULL"), nullable=True
    )

    delivered: Mapped[bool] = mapped_column(Boolean, default=False)

    market: Mapped["Market"] = relationship(back_populates="news_items")


class NewsBatchCandidate(Base):
    """One row per (batch, candidate) resolution — the idempotency guard AND
    the thing that makes 'is this batch done' knowable at all, since most drop
    reasons (not_relevant, dup_*, scrape_failed, ...) never produce a NewsItem.
    outcome is a plain string, not an Enum, matching ScrapeFailure.reason so a
    new drop reason doesn't need a migration."""

    __tablename__ = "news_batch_candidates"
    __table_args__ = (UniqueConstraint("batch_id", "canonical_url_hash", name="uq_batch_candidate"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    batch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("news_batches.id", ondelete="CASCADE"), index=True)
    canonical_url_hash: Mapped[str] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(String(50))
    news_item_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("news_items.id", ondelete="SET NULL"), nullable=True
    )

    resolved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Source(Base):
    __tablename__ = "sources"

    domain: Mapped[str] = mapped_column(String(255), primary_key=True)
    reliability_tier: Mapped[ReliabilityTier] = mapped_column(
        Enum(ReliabilityTier, name="reliability_tier"), default=ReliabilityTier.unknown
    )
    reliability_score: Mapped[float] = mapped_column(Float, default=0.5)
    category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ScrapeFailure(Base):
    __tablename__ = "scrape_failures"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    market_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("markets.id", ondelete="CASCADE"), index=True)

    url: Mapped[str] = mapped_column(Text)
    source_domain: Mapped[str] = mapped_column(String(255), index=True)
    reason: Mapped[str] = mapped_column(String(50))
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DeliveryLog(Base):
    __tablename__ = "delivery_log"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    market_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("markets.id", ondelete="CASCADE"), index=True)
    batch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    news_item_ids: Mapped[list[str]] = mapped_column(JSONB, default=list)

    attempt: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[DeliveryStatus] = mapped_column(
        Enum(DeliveryStatus, name="delivery_status"), default=DeliveryStatus.pending
    )
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    webhook_delivered: Mapped[bool] = mapped_column(Boolean, default=False)
    telegram_sent_news_item_ids: Mapped[list[str]] = mapped_column(JSONB, default=list)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
