from arq import cron
from arq.connections import RedisSettings

from app.config import get_settings
from app.worker.scheduler import enqueue_due_markets
from app.worker.tasks import deliver_batch, process_market

_settings = get_settings()


class WorkerSettings:
    """arq worker entrypoint: `arq app.worker.settings.WorkerSettings`"""

    redis_settings = RedisSettings.from_dsn(_settings.redis_url)
    functions = [process_market, deliver_batch]
    # Runs every minute; each market's actual cadence is governed by its own
    # next_poll_at (see app.worker.tasks._next_poll_at), not by this tick rate.
    cron_jobs = [cron(enqueue_due_markets, minute=set(range(60)))]
    max_tries = 6
    job_timeout = 600  # a single market cycle can involve many scrape+LLM calls
