import logging

from arq import cron
from arq.connections import RedisSettings
from playwright.async_api import async_playwright

from app.config import get_settings
from app.worker.scheduler import enqueue_due_markets, enqueue_stuck_batches, enqueue_stuck_deliveries
from app.worker.tasks import deliver_batch, process_candidate, process_market

_settings = get_settings()


async def _startup(ctx: dict) -> None:
    await _configure_logging(ctx)
    # One long-lived Chromium shared by every process_candidate job in this
    # worker process. Each job scrapes exactly one URL (see the fan-out in
    # process_market), so launching a fresh browser per job paid a seconds-long
    # startup + RAM spike per candidate — pure overhead once the batch-oriented
    # scrape loop stopped being the call shape. Jobs still get an isolated
    # incognito context per URL inside scrape_urls. If this instance crashes
    # mid-life, process_candidate detects the dead connection and falls back to
    # an ephemeral per-job launch; a worker restart restores the shared one.
    ctx["playwright"] = await async_playwright().start()
    ctx["browser"] = await ctx["playwright"].chromium.launch(headless=True)


async def _shutdown(ctx: dict) -> None:
    browser = ctx.get("browser")
    if browser is not None:
        try:
            await browser.close()
        except Exception:  # noqa: BLE001 — already-dead browser must not fail shutdown
            pass
    playwright = ctx.get("playwright")
    if playwright is not None:
        await playwright.stop()


async def _configure_logging(ctx: dict) -> None:
    """arq only sets up its own `arq` logger, so our `app.*` INFO lines (the
    per-candidate funnel/drop reasons in app.worker.tasks) would otherwise be
    dropped by the root last-resort handler at WARNING. Give the `app` namespace
    its own stdout handler at INFO. propagate=False keeps this independent of
    arq's handler so lines aren't duplicated."""
    app_logger = logging.getLogger("app")
    if not app_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        app_logger.addHandler(handler)
        app_logger.setLevel(logging.INFO)
        app_logger.propagate = False


class WorkerSettings:
    """arq worker entrypoint: `arq app.worker.settings.WorkerSettings`"""

    redis_settings = RedisSettings.from_dsn(_settings.redis_url)
    on_startup = _startup
    on_shutdown = _shutdown
    functions = [process_market, process_candidate, deliver_batch]
    # Lets unsubscribe actively cancel a market's in-flight/queued jobs instead
    # of waiting for each to dequeue and no-op on the paused-status guard — see
    # app.worker.abort.abort_market_jobs and the DELETE /markets route.
    allow_abort_jobs = True
    # enqueue_due_markets is now just a safety net (process_market self-schedules
    # its own next run — see app.worker.tasks.process_market), so it only needs
    # to run often enough to catch a lost job within OVERDUE_THRESHOLD, not every
    # minute. enqueue_stuck_deliveries stays on its original cadence — it's still
    # the primary recovery path for stuck deliveries, not a backstop for one.
    # enqueue_stuck_batches runs the same cadence — STUCK_BATCH_THRESHOLD (not
    # the cron interval) is what bounds how long a batch can sit open.
    cron_jobs = [
        cron(enqueue_due_markets, minute={0, 10, 20, 30, 40, 50}),
        cron(enqueue_stuck_deliveries, minute=set(range(60))),
        cron(enqueue_stuck_batches, minute=set(range(60))),
    ]
    max_tries = 6
    # process_candidate does one scrape + one (slow, local-CPU) LLM extraction +
    # one embed. This must exceed Settings.llm_request_timeout_seconds (600) so
    # the arq job doesn't get killed *before* the LLM client's own timeout has a
    # chance to fire and be retried — plus headroom for scrape + embed.
    job_timeout = _settings.llm_request_timeout_seconds + 120
    # process_candidate is the only job that calls the LLM (extract_and_score),
    # one call per job. On CPU, llama.cpp's *total* token throughput is fixed, so
    # running more extractions concurrently than it has slots doesn't add
    # throughput — it just splits that fixed budget, making each request slower
    # and far more likely to hit its timeout. The `llm` service runs a single slot
    # (--parallel 1, docker-compose.yml); we keep this at 2 so exactly one
    # candidate holds the LLM while a second overlaps its scrape/embed, without a
    # pile of jobs blocking on the one slot. The semantic pre-filter (see
    # process_market) is what actually cuts total LLM volume.
    max_jobs = 2
