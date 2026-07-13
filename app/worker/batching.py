"""Batches a market poll cycle's candidates into one sorted webhook delivery.

process_market fans out one process_candidate job per selected candidate,
running concurrently. Most drop reasons (not_relevant, dup_*, scrape_failed,
...) never produce a NewsItem row, so counting stored items alone can never
tell you a cycle is "done" — every candidate's resolution (stored or dropped)
is recorded in NewsBatchCandidate, and that ledger doubles as the completion
counter (NewsBatch.resolved_count/expected_count).

The single invariant everything here rests on: exactly one execution ever
transitions a NewsBatch from open to closed, via one atomic
`UPDATE ... WHERE status = 'open' ... RETURNING`, and only that execution
gathers and delivers the batch's items. This makes the design crash-idempotent
without needing arq retries (which, per app.worker.errors, don't actually
happen for a plain exception) — a re-picked-up job that already resolved its
candidate just hits the NewsBatchCandidate unique constraint and no-ops.

Kept dependency-light (sqlalchemy + models + app.dedup.simhash only, no
playwright/fastembed) so app.worker.scheduler's cron can import it without
pulling in the heavy worker deps — same principle as app.worker.abort.
"""

import uuid

from sqlalchemy import case, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BatchStatus, DeliveryLog, NewsBatch, NewsBatchCandidate, NewsItem
from app.dedup.simhash import to_signed_64


def _market_lock_key(market_id: uuid.UUID) -> int:
    """Stable signed-int8 key for pg_advisory_xact_lock, derived from the market
    UUID. Reentrant within a transaction — safe to call again here even if the
    caller (process_candidate's dedup section) already holds it in the same
    transaction."""
    return to_signed_64(market_id.int & ((1 << 64) - 1))


async def _finalize_batch_delivery(session: AsyncSession, market_id: uuid.UUID, batch_id: uuid.UUID) -> str | None:
    item_ids = (
        await session.execute(
            select(NewsItem.id)
            .where(NewsItem.market_id == market_id, NewsItem.batch_id == batch_id)
            .order_by(NewsItem.published_at.desc().nulls_last(), NewsItem.discovered_at.desc())
        )
    ).scalars().all()
    if not item_ids:
        return None  # every candidate in this batch dropped — nothing to deliver
    log = DeliveryLog(market_id=market_id, batch_id=batch_id, news_item_ids=[str(i) for i in item_ids])
    session.add(log)
    await session.flush()
    return str(log.id)


async def create_solo_delivery(session: AsyncSession, market_id: uuid.UUID, news_item_id: uuid.UUID) -> str:
    """Straggler fallback: this candidate resolved after its batch was already
    finalized elsewhere (closed by a peer reaching parity, or by the
    enqueue_stuck_batches sweep). Deliver it on its own rather than losing it
    — the same one-item-per-webhook shape every candidate used before batching
    existed."""
    log = DeliveryLog(market_id=market_id, batch_id=uuid.uuid4(), news_item_ids=[str(news_item_id)])
    session.add(log)
    await session.flush()
    return str(log.id)


async def resolve_batch_candidate(
    session: AsyncSession,
    market_id: uuid.UUID,
    batch_id: uuid.UUID,
    canonical_url_hash: str,
    outcome: str,
    news_item_id: uuid.UUID | None = None,
) -> str | None:
    """Record one candidate's terminal outcome and, if it completes the batch,
    close it and build the single sorted DeliveryLog. Commits the current
    transaction (whatever else the caller staged, e.g. the NewsItem insert,
    commits atomically with this resolution — no window where an item exists
    but isn't yet reflected in resolved_count). Returns a delivery_log_id to
    enqueue for deliver_batch, or None if there's nothing to send yet (or
    ever, for this call)."""
    await session.execute(select(func.pg_advisory_xact_lock(_market_lock_key(market_id))))

    inserted = (
        await session.execute(
            pg_insert(NewsBatchCandidate)
            .values(
                batch_id=batch_id,
                canonical_url_hash=canonical_url_hash,
                outcome=outcome,
                news_item_id=news_item_id,
            )
            .on_conflict_do_nothing(index_elements=["batch_id", "canonical_url_hash"])
            .returning(NewsBatchCandidate.id)
        )
    ).first()
    if inserted is None:
        # This exact (batch, candidate) resolution already happened — a crash
        # re-pickup re-running the same job body. Idempotent no-op, not a
        # double-count.
        await session.commit()
        return None

    new_count = NewsBatch.resolved_count + 1
    row = (
        await session.execute(
            update(NewsBatch)
            .where(NewsBatch.id == batch_id, NewsBatch.status == BatchStatus.open)
            .values(
                resolved_count=new_count,
                status=case((new_count >= NewsBatch.expected_count, BatchStatus.closed), else_=NewsBatch.status),
            )
            .returning(NewsBatch.status)
        )
    ).first()

    if row is None:
        # Batch already closed — a peer reached parity first, or
        # enqueue_stuck_batches force-closed it. This resolution missed it.
        await session.commit()
        if news_item_id is None:
            return None
        log_id = await create_solo_delivery(session, market_id, news_item_id)
        await session.commit()
        return log_id

    if row.status != BatchStatus.closed:
        await session.commit()
        return None  # not the last one in yet

    log_id = await _finalize_batch_delivery(session, market_id, batch_id)
    await session.commit()
    return log_id


async def force_close_batch(session: AsyncSession, market_id: uuid.UUID, batch_id: uuid.UUID) -> str | None:
    """Sweep-triggered close (see app.worker.scheduler.enqueue_stuck_batches).
    Same CAS as resolve_batch_candidate, just triggered by a timeout instead
    of parity — shares _finalize_batch_delivery so there's exactly one code
    path that ever builds a batch's DeliveryLog."""
    await session.execute(select(func.pg_advisory_xact_lock(_market_lock_key(market_id))))
    row = (
        await session.execute(
            update(NewsBatch)
            .where(NewsBatch.id == batch_id, NewsBatch.status == BatchStatus.open)
            .values(status=BatchStatus.closed)
            .returning(NewsBatch.id)
        )
    ).first()
    if row is None:
        await session.commit()
        return None  # the normal path already closed it
    log_id = await _finalize_batch_delivery(session, market_id, batch_id)
    await session.commit()
    return log_id
