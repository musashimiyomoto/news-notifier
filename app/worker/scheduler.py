from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.db.models import DeliveryLog, DeliveryStatus, Market, MarketStatus
from app.db.session import async_session_factory

# How long a delivery is allowed to sit at `pending` (committed but never
# enqueued, e.g. a worker crash between the two) before the sweep re-enqueues it.
STUCK_DELIVERY_THRESHOLD = timedelta(minutes=5)


async def enqueue_due_markets(ctx: dict) -> None:
    """Cron job: find every active market whose next_poll_at has passed and
    enqueue one process_market job each. Runs frequently (see WorkerSettings);
    actual cadence per market is controlled by next_poll_at, not by this tick."""
    now = datetime.now(timezone.utc)
    async with async_session_factory() as session:
        market_ids = (
            await session.execute(
                select(Market.id).where(Market.status == MarketStatus.active, Market.next_poll_at <= now)
            )
        ).scalars().all()

    redis = ctx["redis"]
    for market_id in market_ids:
        # _job_id dedups against an identical enqueue already in flight (e.g. the
        # subscribe-time immediate trigger racing this cron tick for the same market).
        await redis.enqueue_job("process_market", str(market_id), _job_id=f"process_market:{market_id}")


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
