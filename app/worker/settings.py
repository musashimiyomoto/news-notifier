from arq import cron
from arq.connections import RedisSettings

from app.config import get_settings
from app.worker.scheduler import enqueue_due_markets, enqueue_stuck_deliveries
from app.worker.tasks import deliver_batch, process_candidate, process_market

_settings = get_settings()


class WorkerSettings:
    """arq worker entrypoint: `arq app.worker.settings.WorkerSettings`"""

    redis_settings = RedisSettings.from_dsn(_settings.redis_url)
    functions = [process_market, process_candidate, deliver_batch]
    # Runs every minute; each market's actual cadence is governed by its own
    # next_poll_at (see app.worker.tasks._next_poll_at), not by this tick rate.
    cron_jobs = [
        cron(enqueue_due_markets, minute=set(range(60))),
        cron(enqueue_stuck_deliveries, minute=set(range(60))),
    ]
    max_tries = 6
    # No single job is heavy anymore: process_market only dispatches, and each
    # process_candidate is one scrape + one LLM extraction + one embed. The old
    # 600s existed because the whole per-market batch ran in one job.
    job_timeout = 180
