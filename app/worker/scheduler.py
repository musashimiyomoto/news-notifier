from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.db.models import DeliveryLog, DeliveryStatus, Market, MarketStatus
from app.db.session import async_session_factory
from app.worker.errors import serializable_job_errors
from app.worker.job_ids import process_market_job_id

# How long a delivery is allowed to sit at `pending` (committed but never
# enqueued, e.g. a worker crash between the two) before the sweep re-enqueues it.
STUCK_DELIVERY_THRESHOLD = timedelta(minutes=5)

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
    Sweep for any that have sat too long and re-enqueue them."""
    cutoff = datetime.now(timezone.utc) - STUCK_DELIVERY_THRESHOLD
    async with async_session_factory() as session:
        log_ids = (
            await session.execute(
                select(DeliveryLog.id).where(
                    DeliveryLog.status == DeliveryStatus.pending, DeliveryLog.created_at <= cutoff
                )
            )
        ).scalars().all()

    redis = ctx["redis"]
    for log_id in log_ids:
        await redis.enqueue_job("deliver_batch", str(log_id), _job_id=f"deliver_batch:{log_id}")
