import logging
import random
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from sqlalchemy import func, select

from app.config import get_settings
from app.db.models import DeliveryLog, DeliveryStatus, ImpactHint, Market, MarketStatus, NewsBatch, NewsItem, ScrapeFailure
from app.db.session import async_session_factory
from app.dedup.simhash import from_signed_64, hamming_distance, simhash, to_signed_64
from app.dedup.vector_dedup import find_similar
from app.delivery.webhook import send_webhook
from app.llm.embeddings import embed_text
from app.llm.extraction import extract_and_score
from app.llm.prefilter import select_relevant_candidates
from app.llm.query_gen import generate_queries
from app.scoring.credibility import compute_credibility
from app.scraping.playwright_scraper import scrape_urls
from app.search.aggregator import parse_published_at, search_all_sources, url_hash
from app.security import decrypt_secret
from app.worker.abort import register_market_job
from app.worker.batching import _market_lock_key, create_solo_delivery, resolve_batch_candidate
from app.worker.errors import serializable_job_errors
from app.worker.job_ids import process_market_job_id

logger = logging.getLogger(__name__)


def _domain_of(url: str) -> str:
    return urlsplit(url).netloc.removeprefix("www.")


def _filter_by_recency(candidates: list[dict], max_age_days: int, now: datetime) -> list[dict]:
    """Drop candidates published longer ago than max_age_days. Candidates with a
    missing/unparseable published_at are kept — see Settings.candidate_max_age_days.
    max_age_days <= 0 disables the filter."""
    if max_age_days <= 0:
        return candidates
    cutoff = now - timedelta(days=max_age_days)
    kept = []
    for candidate in candidates:
        published_at = parse_published_at(candidate.get("published_at"))
        if published_at is None or published_at >= cutoff:
            kept.append(candidate)
    return kept


def _filter_older_than_watermark(candidates: list[dict], watermark: datetime | None) -> list[dict]:
    """Drop candidates published before the newest article already delivered
    for this market. Search sources sometimes surface an article days after
    its actual publication (late indexing) — without this, such a straggler
    would land in today's batch even though a batch with newer articles was
    already delivered yesterday, breaking cross-batch chronological order.
    Candidates with a missing/unparseable published_at are kept, matching
    _filter_by_recency's policy — losing a good article to a bad date string
    is worse than holding it back."""
    if watermark is None:
        return candidates
    kept = []
    for candidate in candidates:
        published_at = parse_published_at(candidate.get("published_at"))
        if published_at is None or published_at >= watermark:
            kept.append(candidate)
    return kept


# Adaptive polling: the closer resolution_date is, the more often we look.
# Tiers are (max_time_remaining, poll_step); default_minutes is the fallback
# for markets far from resolution / with no resolution_date at all.
_ADAPTIVE_TIERS = [
    (timedelta(hours=24), timedelta(hours=1)),
    (timedelta(days=7), timedelta(hours=6)),
    (timedelta(days=30), timedelta(hours=24)),
]


def _next_poll_at(
    resolution_date: datetime | None,
    now: datetime,
    default_minutes: int,
    jitter_fraction: float = 0.0,
) -> datetime:
    """`jitter_fraction` randomizes the chosen step by +/- that fraction (e.g. 0.15
    = up to 15% earlier or later), so markets sharing a tier don't all wake up in
    the same worker tick — see Settings.poll_jitter_fraction. Applied uniformly
    across every branch (including the two fixed-cadence ones) rather than just
    the tiered case, since a burst of same-second subscriptions with no
    resolution_date would otherwise all collide on the default-interval branch."""
    if resolution_date is None:
        step = timedelta(minutes=default_minutes)
    else:
        remaining = resolution_date - now
        if remaining <= timedelta(0):
            # Past resolution_date but not yet marked resolved by the client —
            # keep checking at the tightest cadence rather than silently going quiet.
            step = timedelta(hours=1)
        else:
            step = timedelta(minutes=default_minutes)
            for max_remaining, tier_step in _ADAPTIVE_TIERS:
                if remaining <= max_remaining:
                    step = tier_step
                    break

    if jitter_fraction:
        step *= 1 + random.uniform(-jitter_fraction, jitter_fraction)
    return now + step


@serializable_job_errors
async def process_market(ctx: dict, market_id: str) -> None:
    """Dispatcher: generate queries -> search -> filter fresh candidates -> fan out
    one process_candidate job per URL. Deliberately does NO scraping or LLM
    extraction itself — those are the slow, per-article steps that used to run
    serially in one job and blow past job_timeout on a market's first cycle
    (where every candidate is fresh). Keeping only the bounded query+search work
    here means this job stays fast.

    All candidates selected this cycle share one NewsBatch, so the articles
    they produce go out as a single webhook sorted by published_at instead of
    trickling in one at a time in whatever order each async job happens to
    finish — see app.worker.batching.resolve_batch_candidate, which every
    process_candidate call eventually invokes.

    Self-schedules its own next run at the end (see the _defer_until enqueue
    below) instead of relying solely on scheduler.enqueue_due_markets polling the
    DB every tick. That crontick still exists as a safety net (catches a market
    whose self-scheduled job was lost to a worker crash/Redis flush), but is no
    longer the primary driver — see WorkerSettings.cron_jobs."""
    settings = get_settings()

    async with async_session_factory() as session:
        market = await session.get(Market, uuid.UUID(market_id))
        if market is None or market.status != MarketStatus.active:
            return

        queries = await generate_queries(market.description)
        candidates = await search_all_sources(queries, settings.search_results_per_source)

        existing_hashes = set(
            (
                await session.execute(
                    select(NewsItem.canonical_url_hash).where(NewsItem.market_id == market.id)
                )
            ).scalars().all()
        )
        fresh_candidates = [c for c in candidates if c["canonical_url_hash"] not in existing_hashes]

        # Cheap pre-filters before the expensive per-candidate pipeline: drop
        # stale coverage, then keep only the top-K semantically closest to the
        # market. Both run on already-fetched search metadata (title + date), so
        # they cost ~nothing compared to a scrape + local-LLM extraction per URL.
        recent_candidates = _filter_by_recency(
            fresh_candidates, settings.candidate_max_age_days, datetime.now(timezone.utc)
        )
        # Hold back anything older than what we've already delivered, so a
        # late-indexed article can't arrive in a batch after a batch with
        # newer articles was already sent — see _filter_older_than_watermark.
        unstale_candidates = _filter_older_than_watermark(recent_candidates, market.max_delivered_published_at)
        selected_candidates = await select_relevant_candidates(
            market.description,
            unstale_candidates,
            settings.candidate_prefilter_top_k,
            settings.candidate_prefilter_min_similarity,
        )
        logger.info(
            "process_market market=%s candidates: found=%d fresh=%d recent=%d unstale=%d selected=%d",
            market_id,
            len(candidates),
            len(fresh_candidates),
            len(recent_candidates),
            len(unstale_candidates),
            len(selected_candidates),
        )

        market.last_polled_at = datetime.now(timezone.utc)
        market.next_poll_at = _next_poll_at(
            market.resolution_date,
            market.last_polled_at,
            market.poll_interval_minutes,
            settings.poll_jitter_fraction,
        )
        next_poll_at = market.next_poll_at

        # One NewsBatch per cycle, committed before any process_candidate job
        # can start — otherwise a job could resolve and look up a batch row
        # that doesn't exist yet.
        batch_id: uuid.UUID | None = None
        if selected_candidates:
            batch = NewsBatch(market_id=market.id, expected_count=len(selected_candidates))
            session.add(batch)
            await session.flush()
            batch_id = batch.id

        await session.commit()

    redis = ctx["redis"]
    # _job_id includes next_poll_at (see process_market_job_id) so this doesn't
    # collide with the still-in-flight current job's own job_id — arq holds a
    # job_id's key until the enqueuing coroutine returns, and this call happens
    # from inside that same coroutine, before it returns.
    await redis.enqueue_job(
        "process_market",
        market_id,
        _job_id=process_market_job_id(market_id, next_poll_at),
        _defer_until=next_poll_at,
    )

    for candidate in selected_candidates:
        # _job_id dedups a re-enqueue of the same URL across overlapping cycles
        # (same idea as scheduler.enqueue_due_markets), so a candidate that's slow
        # to process isn't picked up twice by the next poll tick.
        candidate_job_id = f"process_candidate:{market_id}:{candidate['canonical_url_hash']}"
        job = await redis.enqueue_job(
            "process_candidate",
            market_id,
            candidate,
            str(batch_id) if batch_id else None,
            _job_id=candidate_job_id,
        )
        # Track it so unsubscribe can abort this market's in-flight/queued
        # candidates instead of letting each one dequeue and no-op — see
        # app.worker.abort and the DELETE /markets route.
        await register_market_job(redis, market_id, candidate_job_id)

        if job is None and batch_id is not None:
            # job_id collided with a prior cycle's still-live job or its kept
            # result (arq keeps a job's result for 1h by default) — that job's
            # eventual resolution belongs to *that* batch, not this one. This
            # batch still expects one slot for this candidate, so resolve it
            # now via the same primitive every other outcome goes through,
            # rather than leaving expected_count permanently short.
            async with async_session_factory() as collision_session:
                log_id = await resolve_batch_candidate(
                    collision_session,
                    uuid.UUID(market_id),
                    batch_id,
                    candidate["canonical_url_hash"],
                    "collided_prior_cycle",
                )
            if log_id:
                await redis.enqueue_job("deliver_batch", log_id, _job_id=f"deliver_batch:{log_id}")


@serializable_job_errors
async def process_candidate(ctx: dict, market_id: str, candidate: dict, batch_id: str | None = None) -> None:
    """Heavy per-URL job: scrape -> extract/score -> embed -> dedup -> store one
    NewsItem, then resolve this candidate against its NewsBatch (see
    app.worker.batching). Runs independently per article so one slow/flaky LLM
    call can't stall (or time out) the others — the resolve() step is what
    coalesces every candidate's outcome into a single sorted webhook per poll
    cycle instead of one webhook per article. batch_id defaults to None so a
    job already queued under the old 3-arg signature at deploy time falls back
    to today's exact one-item-per-webhook behavior instead of erroring.

    Cross-job dedup that the old serial loop got for free (in-batch title simhash,
    incremental vector dedup) is done here against the DB under a per-market
    advisory lock, so two concurrent candidate jobs for the same market can't
    both slip in a near-duplicate."""
    settings = get_settings()
    batch_uuid = uuid.UUID(batch_id) if batch_id else None

    async def resolve(session, outcome: str, news_item_id: uuid.UUID | None = None) -> None:
        """Record this candidate's terminal outcome and enqueue a delivery if
        that closes/completes something to send. Commits `session`."""
        if batch_uuid is None:
            if news_item_id is None:
                await session.commit()
                return
            log_id = await create_solo_delivery(session, uuid.UUID(market_id), news_item_id)
            await session.commit()
        else:
            log_id = await resolve_batch_candidate(
                session, uuid.UUID(market_id), batch_uuid, candidate["canonical_url_hash"], outcome, news_item_id
            )
        if log_id:
            await ctx["redis"].enqueue_job("deliver_batch", log_id, _job_id=f"deliver_batch:{log_id}")

    async with async_session_factory() as session:
        market = await session.get(Market, uuid.UUID(market_id))
        if market is None or market.status != MarketStatus.active:
            await resolve(session, "market_inactive")
            return

        # Reuse the worker's long-lived browser (see app.worker.settings._startup)
        # instead of paying a full Chromium launch per candidate. If it died
        # (crash/OOM), fall back to an ephemeral launch for this job rather than
        # failing it — no relaunch here to avoid concurrent-relaunch races.
        browser = ctx.get("browser")
        if browser is not None and not browser.is_connected():
            logger.warning("process_candidate shared browser disconnected; using ephemeral launch")
            browser = None
        scraped = await scrape_urls(
            [candidate["url"]], settings.playwright_timeout_ms, settings.scrape_concurrency, browser=browser
        )
        scrape_result = scraped.get(candidate["url"])
        if not scrape_result or not scrape_result["success"]:
            logger.info("process_candidate drop reason=scrape_failed url=%s", candidate["url"])
            # final_url (when available) reflects the post-redirect real domain;
            # falls back to the pre-scrape candidate URL (e.g. a Google News
            # redirect) when navigation failed outright and never resolved one.
            failure_domain = _domain_of((scrape_result or {}).get("final_url") or candidate["url"])
            session.add(
                ScrapeFailure(
                    market_id=market.id,
                    url=candidate["url"],
                    source_domain=failure_domain,
                    reason="scrape_failed",
                    detail=(scrape_result or {}).get("error"),
                )
            )
            await resolve(session, "scrape_failed")
            return

        # Hash/domain must come from the post-redirect final_url, not the
        # pre-scrape search-result URL (Google News RSS links are redirects,
        # and DDG/Google News "source" fields are display names, not domains).
        final_hash = url_hash(scrape_result["final_url"])
        domain = _domain_of(scrape_result["final_url"])

        try:
            extracted = await extract_and_score(market.description, scrape_result["text"], domain)
        except Exception:
            # A flaky/malformed LLM call fails just this candidate, not a batch.
            # This is the path that silently swallowed *every* candidate when the
            # local LLM was timing out — log it so that failure mode is visible.
            logger.warning(
                "process_candidate drop reason=extraction_error url=%s", candidate["url"], exc_info=True
            )
            await resolve(session, "extraction_error")
            return
        if not extracted or not extracted.get("is_relevant"):
            logger.info("process_candidate drop reason=not_relevant url=%s", candidate["url"])
            await resolve(session, "not_relevant")
            return

        title = extracted["title"]
        title_hash = simhash(title)

        try:
            embedding = await embed_text(extracted["summary"])
        except Exception:
            logger.warning(
                "process_candidate drop reason=embed_error url=%s", candidate["url"], exc_info=True
            )
            await resolve(session, "embed_error")
            return

        # --- critical section: dedup-check-and-insert, serialized per market ---
        await session.execute(select(func.pg_advisory_xact_lock(_market_lock_key(market.id))))

        # Re-check status under the lock: `market` was read before a scrape +
        # multi-minute extraction, during which the market may have been paused
        # (unsubscribe). Re-read committed state so a candidate already in flight
        # when unsubscribe fired — which arq's abort can't stop once it's past
        # the entry guard — doesn't still store a NewsItem / trigger a delivery.
        current_status = (
            await session.execute(select(Market.status).where(Market.id == market.id))
        ).scalar_one_or_none()
        if current_status != MarketStatus.active:
            logger.info(
                "process_candidate drop reason=market_inactive url=%s", scrape_result["final_url"]
            )
            await resolve(session, "market_inactive")
            return

        stored = (
            await session.execute(
                select(NewsItem.canonical_url_hash, NewsItem.title_simhash).where(
                    NewsItem.market_id == market.id
                )
            )
        ).all()
        if any(h == final_hash for h, _ in stored):
            # exact URL already stored (also guarded by uq_news_market_url)
            logger.info("process_candidate drop reason=dup_url url=%s", scrape_result["final_url"])
            await resolve(session, "dup_url")
            return
        if any(
            s is not None
            and hamming_distance(title_hash, from_signed_64(s)) <= settings.simhash_hamming_threshold
            for _, s in stored
        ):
            # near-duplicate title already stored for this market
            logger.info("process_candidate drop reason=dup_title url=%s", scrape_result["final_url"])
            await resolve(session, "dup_title")
            return

        similar = await find_similar(session, market.id, embedding, settings.vector_dedup_threshold)
        if similar is not None:
            # semantic duplicate of a news item already stored for this market
            logger.info("process_candidate drop reason=dup_semantic url=%s", scrape_result["final_url"])
            await resolve(session, "dup_semantic")
            return

        credibility = await compute_credibility(session, domain, extracted["credibility_signal"])
        relevance = max(0.0, min(1.0, extracted["relevance_score"]))

        try:
            impact_hint = ImpactHint(extracted["impact_hint"])
        except ValueError:
            # LLM structured output is schema-constrained but not immune to
            # returning an unexpected value under load — fail soft, not hard.
            impact_hint = ImpactHint.ambiguous

        item = NewsItem(
            market_id=market.id,
            batch_id=batch_uuid,
            url=scrape_result["final_url"],
            canonical_url_hash=final_hash,
            title_simhash=to_signed_64(title_hash),
            title=title,
            summary=extracted["summary"],
            proofs=extracted.get("proofs", []),
            source_domain=domain,
            # Prefer the search-source date; fall back to the date scraped from
            # the article's own page metadata (see playwright_scraper/msn
            # extractor) when the source didn't supply a usable one.
            published_at=parse_published_at(candidate.get("published_at"))
            or parse_published_at(scrape_result.get("published_at")),
            credibility_score=credibility,
            relevance_score=relevance,
            impact_hint=impact_hint,
            embedding=embedding,
        )
        session.add(item)
        await session.flush()  # item.id assigned, still uncommitted
        item_id = str(item.id)

        # resolve()'s internal commit persists the item + its resolution (+ the
        # finalize DeliveryLog, if this call happens to close the batch) all
        # atomically — this replaces the old standalone
        # `session.commit()  # releases the advisory xact lock`.
        await resolve(session, "stored", news_item_id=item.id)

    logger.info("process_candidate stored news_item=%s url=%s", item_id, scrape_result["final_url"])


@serializable_job_errors
async def deliver_batch(ctx: dict, delivery_log_id: str) -> None:
    """Send (or resend) the webhook for one already-stored batch. Independent
    retry target — no re-search, no re-extraction, no risk of dedup silently
    swallowing the retry — but the retry itself does NOT come from arq: arq
    only retries a job that explicitly raises arq.worker.Retry (see
    app.worker.errors), and the RuntimeError raised below is a plain
    exception, so a failed attempt here ends this job permanently. The actual
    retry path is app.worker.scheduler.enqueue_stuck_deliveries, which sweeps
    DeliveryLogs left at `pending` *or* `failed` and re-enqueues them."""
    settings = get_settings()

    async with async_session_factory() as session:
        log = await session.get(DeliveryLog, uuid.UUID(delivery_log_id))
        if log is None or log.status == DeliveryStatus.success:
            return

        market = await session.get(Market, log.market_id)
        if market is None:
            return
        if market.status != MarketStatus.active:
            # Market was paused/resolved (unsubscribe) after this batch was
            # queued — don't emit a webhook for a market nobody's watching. Left
            # at `pending`; enqueue_stuck_deliveries filters to active markets, so
            # this is simply abandoned rather than re-enqueued in a loop.
            logger.info("deliver_batch skip reason=market_inactive delivery_log=%s", delivery_log_id)
            return

        # One IN query instead of a round trip per item; news_item_ids already
        # carries the delivery order (sorted at finalize time — see
        # app.worker.batching._finalize_batch_delivery), so reassemble in that
        # order rather than whatever the DB returns.
        item_ids = [uuid.UUID(item_id) for item_id in log.news_item_ids]
        fetched = (
            await session.execute(select(NewsItem).where(NewsItem.id.in_(item_ids)))
        ).scalars().all()
        by_id = {item.id: item for item in fetched}
        items = [by_id[item_id] for item_id in item_ids if item_id in by_id]

        payload = {
            "market_id": market.external_market_id,
            "batch_id": str(log.batch_id),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "news": [
                {
                    "news_id": str(i.id),
                    "title": i.title,
                    "summary": i.summary,
                    "url": i.url,
                    "source_domain": i.source_domain,
                    "published_at": i.published_at.isoformat() if i.published_at else None,
                    "credibility_score": i.credibility_score,
                    "relevance_score": i.relevance_score,
                    "impact_hint": i.impact_hint.value,
                    "proofs": i.proofs,
                }
                for i in items
            ],
        }
        callback_url = market.callback_url
        callback_secret = decrypt_secret(market.callback_secret_encrypted)

        status_code, error = await send_webhook(callback_url, callback_secret, payload)

        log.attempt += 1
        if status_code is not None and 200 <= status_code < 300:
            log.status = DeliveryStatus.success
            log.status_code = status_code
            log.delivered_at = datetime.now(timezone.utc)
            for item in items:
                item.delivered = True
            # Advance the watermark so future cycles hold back anything older
            # than the newest article just delivered — see
            # app.worker.tasks._filter_older_than_watermark. Undated items
            # don't move it (nothing to compare against).
            newest = max((i.published_at for i in items if i.published_at is not None), default=None)
            if newest is not None and (
                market.max_delivered_published_at is None or newest > market.max_delivered_published_at
            ):
                market.max_delivered_published_at = newest
            await session.commit()
            return

        log.status_code = status_code
        log.error = error
        if log.attempt >= settings.max_delivery_attempts:
            log.status = DeliveryStatus.dead_letter
            await session.commit()
            return  # give up silently here; TODO: surface dead-lettered batches via an admin endpoint/alert

        log.status = DeliveryStatus.failed
        await session.commit()
        raise RuntimeError(f"Webhook delivery failed (status={status_code}, error={error})")
