from datetime import datetime, timezone

from sqlalchemy import select

from app.db.models import Market, MarketStatus
from app.db.session import async_session_factory


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
        await redis.enqueue_job("process_market", str(market_id))
