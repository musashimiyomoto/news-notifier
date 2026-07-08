from datetime import datetime


def process_market_job_id(market_id: str, run_at: datetime) -> str:
    """Job id for a process_market run scheduled at `run_at`.

    Including the timestamp (rather than a bare f"process_market:{market_id}")
    keeps each poll cycle's job_id unique. arq refuses to enqueue a job_id while
    its previous job/result key still exists in Redis — and per arq's own
    execution order, that key isn't cleared until the coroutine using it
    *returns*. Since process_market now self-schedules its own next run from
    inside its own coroutine body (see app.worker.tasks.process_market), reusing
    the same fixed job_id there would always silently no-op (enqueue_job
    returns None) because the current cycle's job_id is still live.

    Kept in its own module (rather than app.worker.tasks) so the API layer can
    build a matching id for the subscribe-time immediate trigger without
    importing tasks.py's heavy deps (playwright, fastembed, ...).
    """
    return f"process_market:{market_id}:{run_at.isoformat()}"
