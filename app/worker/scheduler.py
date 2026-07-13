from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.db.models import BatchStatus, DeliveryLog, DeliveryStatus, Market, MarketStatus, NewsBatch
from app.db.session import async_session_factory
from app.worker.batching import force_close_batch
from app.worker.errors import serializable_job_errors
from app.worker.job_ids import process_market_job_id

# How long a delivery is allowed to sit at `pending` (committed but never
# enqueued, e.g. a worker crash between the two) or `failed` (its one webhook
# attempt came back non-2xx/errored — arq does NOT auto-retry a plain
# exception, see app.worker.tasks.deliver_batch, so this sweep is the only
# retry path) before the sweep re-enqueues it.
STUCK_DELIVERY_THRESHOLD = timedelta(minutes=5)

# A NewsBatch can be left `open` forever if one of its fanned-out
# process_candidate jobs never resolves (arq doesn't auto-retry a plain
# exception or a job_timeout expiry either — see app.worker.errors /
# app.llm.client). A crash-recovered candidate can legitimately take close to
# 2x job_timeout to resolve, so this needs real headroom before concluding a
# batch is truly stuck rather than just slow.
STUCK_BATCH_THRESHOLD = timedelta(minutes=30)

# A market whose next_poll_at is this far in the past almost certainly lost its
# self-scheduled process_market job (worker crash, Redis flush/eviction) rather
# than just being a few seconds late — see enqueue_due_markets below.
OVERDUE_THRESHOLD = timedelta(minutes=5)


@serializable_job_errors
async def enqueue_due_markets(ctx: dict) -> None:
    """Cron safety-net: process_market self-schedules its own next run (see
    app.worker.tasks.process_market) — this used to be the *only* driver, polling
    every minute, which meant markets sharing a cadence tier all woke up in the
    same tick (bursty load, wasted idle in between). Now it's a low-frequency
    backstop that only catches a market whose self-scheduled job never made it
    (worker crash, Redis eviction) rather than driving normal cadence, hence the
    OVERDUE_THRESHOLD grace period instead of a bare `<= now`."""
    cutoff = datetime.now(timezone.utc) - OVERDUE_THRESHOLD
    async with async_session_factory() as session:
        market_ids = (
            await session.execute(
                select(Market.id).where(Market.status == MarketStatus.active, Market.next_poll_at <= cutoff)
            )
        ).scalars().all()

    redis = ctx["redis"]
    now = datetime.now(timezone.utc)
    for market_id in market_ids:
        # _job_id matches what process_market's own self-schedule would have used
        # for this next_poll_at, so this can't double-enqueue against a job that
        # is merely late rather than lost. Recovered runs go out immediately
        # (_defer_until=now) rather than at the original stale next_poll_at.
        await redis.enqueue_job(
            "process_market",
            str(market_id),
            _job_id=process_market_job_id(str(market_id), now),
            _defer_until=now,
        )


@serializable_job_errors
async def enqueue_stuck_deliveries(ctx: dict) -> None:
    """Cron job: DeliveryLog rows are committed before the deliver_batch job is
    enqueued (see app.worker.tasks.process_market) — a crash or Redis hiccup in
    that window leaves a row at `pending` forever with nothing to recover it.
    Also sweeps `failed`: deliver_batch's own RuntimeError on a bad webhook
    response does not trigger an arq retry (arq only retries on an explicit
    arq.worker.Retry — see app.worker.errors), so without this a transient
    webhook failure (a 500, a timeout) would silently strand its delivery
    forever after exactly one attempt. Re-processing a `failed` row is already
    safe — deliver_batch's own top guard only skips `status == success`."""
    cutoff = datetime.now(timezone.utc) - STUCK_DELIVERY_THRESHOLD
    async with async_session_factory() as session:
        # Join Market and require it still active: a pending/failed delivery
        # for a paused/resolved (unsubscribed) market is deliberately
        # abandoned by deliver_batch, so re-enqueuing it here would loop forever.
        log_ids = (
            await session.execute(
                select(DeliveryLog.id)
                .join(Market, Market.id == DeliveryLog.market_id)
                .where(
                    DeliveryLog.status.in_([DeliveryStatus.pending, DeliveryStatus.failed]),
                    DeliveryLog.created_at <= cutoff,
                    Market.status == MarketStatus.active,
                )
            )
        ).scalars().all()

    redis = ctx["redis"]
    for log_id in log_ids:
        await redis.enqueue_job("deliver_batch", str(log_id), _job_id=f"deliver_batch:{log_id}")


@serializable_job_errors
async def enqueue_stuck_batches(ctx: dict) -> None:
    """Cron safety-net: a NewsBatch can be left `open` forever if one of its
    fanned-out process_candidate jobs never resolves (see
    STUCK_BATCH_THRESHOLD). Force-close anything open too long — whatever
    NewsItems it already has get delivered as one group (or nothing, if every
    candidate in it dropped) via app.worker.batching.force_close_batch, which
    shares the exact same close-and-finalize path as the normal parity case."""
    cutoff = datetime.now(timezone.utc) - STUCK_BATCH_THRESHOLD
    async with async_session_factory() as session:
        rows = (
            await session.execute(
                select(NewsBatch.id, NewsBatch.market_id)
                .join(Market, Market.id == NewsBatch.market_id)
                .where(
                    NewsBatch.status == BatchStatus.open,
                    NewsBatch.created_at <= cutoff,
                    Market.status == MarketStatus.active,
                )
            )
        ).all()

    redis = ctx["redis"]
    for batch_id, market_id in rows:
        async with async_session_factory() as session:
            log_id = await force_close_batch(session, market_id, batch_id)
        if log_id:
            await redis.enqueue_job("deliver_batch", log_id, _job_id=f"deliver_batch:{log_id}")
