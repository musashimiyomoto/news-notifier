import logging
import random
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from sqlalchemy import func, select

from app.config import get_settings
from app.db.models import DeliveryLog, DeliveryStatus, ImpactHint, Market, MarketStatus, NewsItem, ScrapeFailure
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


def _market_lock_key(market_id: uuid.UUID) -> int:
    """Stable signed-int8 key for pg_advisory_xact_lock, derived from the market
    UUID. Serializes the dedup-check-and-insert of concurrent process_candidate
    jobs for the same market so parallel workers can't slip in near-duplicates."""
    return to_signed_64(market_id.int & ((1 << 64) - 1))

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
    here means this job stays fast, and each article is generated (and its webhook
    fired) independently rather than waiting on the whole batch.

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
        selected_candidates = await select_relevant_candidates(
            market.description,
            recent_candidates,
            settings.candidate_prefilter_top_k,
            settings.candidate_prefilter_min_similarity,
        )
        logger.info(
            "process_market market=%s candidates: found=%d fresh=%d recent=%d selected=%d",
            market_id,
            len(candidates),
            len(fresh_candidates),
            len(recent_candidates),
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
        await redis.enqueue_job("process_candidate", market_id, candidate, _job_id=candidate_job_id)
        # Track it so unsubscribe can abort this market's in-flight/queued
        # candidates instead of letting each one dequeue and no-op — see
        # app.worker.abort and the DELETE /markets route.
        await register_market_job(redis, market_id, candidate_job_id)


@serializable_job_errors
async def process_candidate(ctx: dict, market_id: str, candidate: dict) -> None:
    """Heavy per-URL job: scrape -> extract/score -> embed -> dedup -> store one
    NewsItem, then enqueue its own single-item deliver_batch. Runs independently
    per article so one slow/flaky LLM call can't stall (or time out) the others.

    Cross-job dedup that the old serial loop got for free (in-batch title simhash,
    incremental vector dedup) is done here against the DB under a per-market
    advisory lock, so two concurrent candidate jobs for the same market can't
    both slip in a near-duplicate."""
    settings = get_settings()

    async with async_session_factory() as session:
        market = await session.get(Market, uuid.UUID(market_id))
        if market is None or market.status != MarketStatus.active:
            return

        scraped = await scrape_urls(
            [candidate["url"]], settings.playwright_timeout_ms, settings.scrape_concurrency
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
            await session.commit()
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
            return
        if not extracted or not extracted.get("is_relevant"):
            logger.info("process_candidate drop reason=not_relevant url=%s", candidate["url"])
            return

        title = extracted["title"]
        title_hash = simhash(title)

        try:
            embedding = await embed_text(extracted["summary"])
        except Exception:
            logger.warning(
                "process_candidate drop reason=embed_error url=%s", candidate["url"], exc_info=True
            )
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
            return
        if any(
            s is not None
            and hamming_distance(title_hash, from_signed_64(s)) <= settings.simhash_hamming_threshold
            for _, s in stored
        ):
            # near-duplicate title already stored for this market
            logger.info("process_candidate drop reason=dup_title url=%s", scrape_result["final_url"])
            return

        similar = await find_similar(session, market.id, embedding, settings.vector_dedup_threshold)
        if similar is not None:
            # semantic duplicate of a news item already stored for this market
            logger.info("process_candidate drop reason=dup_semantic url=%s", scrape_result["final_url"])
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
        await session.flush()

        delivery_log = DeliveryLog(
            market_id=market.id, batch_id=uuid.uuid4(), news_item_ids=[str(item.id)]
        )
        session.add(delivery_log)
        await session.commit()  # releases the advisory xact lock
        log_id = str(delivery_log.id)
        item_id = str(item.id)

    logger.info(
        "process_candidate stored news_item=%s delivery_log=%s url=%s", item_id, log_id, scrape_result["final_url"]
    )
    redis = ctx["redis"]
    await redis.enqueue_job("deliver_batch", log_id)


@serializable_job_errors
async def deliver_batch(ctx: dict, delivery_log_id: str) -> None:
    """Send (or resend) the webhook for one already-stored batch. Independent
    retry target: arq re-runs *only this* job on failure — no re-search, no
    re-extraction, no risk of dedup silently swallowing the retry."""
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

        items = [
            await session.get(NewsItem, uuid.UUID(item_id))
            for item_id in log.news_item_ids
        ]
        items = [i for i in items if i is not None]

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
