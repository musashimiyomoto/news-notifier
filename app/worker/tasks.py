import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from sqlalchemy import select

from app.config import get_settings
from app.db.models import DeliveryLog, DeliveryStatus, ImpactHint, Market, MarketStatus, NewsItem
from app.db.session import async_session_factory
from app.dedup.simhash import hamming_distance, simhash, to_signed_64
from app.dedup.vector_dedup import find_similar
from app.delivery.webhook import send_webhook
from app.llm.embeddings import embed_text
from app.llm.extraction import extract_and_score
from app.llm.query_gen import generate_queries
from app.scoring.credibility import compute_credibility
from app.scraping.playwright_scraper import scrape_urls
from app.search.aggregator import parse_published_at, search_all_sources, url_hash
from app.security import decrypt_secret


def _domain_of(url: str) -> str:
    return urlsplit(url).netloc.removeprefix("www.")

# Adaptive polling: the closer resolution_date is, the more often we look.
# Tiers are (max_time_remaining, poll_step); default_minutes is the fallback
# for markets far from resolution / with no resolution_date at all.
_ADAPTIVE_TIERS = [
    (timedelta(hours=24), timedelta(hours=1)),
    (timedelta(days=7), timedelta(hours=6)),
    (timedelta(days=30), timedelta(hours=24)),
]


def _next_poll_at(resolution_date: datetime | None, now: datetime, default_minutes: int) -> datetime:
    if resolution_date is None:
        return now + timedelta(minutes=default_minutes)

    remaining = resolution_date - now
    if remaining <= timedelta(0):
        # Past resolution_date but not yet marked resolved by the client — keep
        # checking at the tightest cadence rather than silently going quiet.
        return now + timedelta(hours=1)

    for max_remaining, step in _ADAPTIVE_TIERS:
        if remaining <= max_remaining:
            return now + step
    return now + timedelta(minutes=default_minutes)


async def process_market(ctx: dict, market_id: str) -> None:
    """Search -> scrape -> extract/score -> dedup -> store. Enqueues a separate
    deliver_batch job for the webhook instead of sending inline, so a delivery
    failure retries just the send — not the whole (expensive, non-idempotent-once-
    stored) search+extraction pipeline."""
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

        new_items: list[NewsItem] = []

        if fresh_candidates:
            scraped = await scrape_urls(
                [c["url"] for c in fresh_candidates], settings.playwright_timeout_ms, settings.scrape_concurrency
            )
            batch_title_hashes: list[int] = []

            for candidate in fresh_candidates:
                scrape_result = scraped.get(candidate["url"])
                if not scrape_result or not scrape_result["success"]:
                    continue

                # Hash/domain must come from the post-redirect final_url, not the
                # pre-scrape search-result URL (Google News RSS links are redirects,
                # and DDG/Google News "source" fields are display names, not domains).
                final_hash = url_hash(scrape_result["final_url"])
                if final_hash in existing_hashes:
                    continue
                domain = _domain_of(scrape_result["final_url"])

                try:
                    extracted = await extract_and_score(market.description, scrape_result["text"], domain)
                except Exception:
                    # One flaky/malformed LLM call must not abort the whole batch.
                    continue
                if not extracted or not extracted.get("is_relevant"):
                    continue

                title = extracted["title"]
                title_hash = simhash(title)
                if any(
                    hamming_distance(title_hash, seen) <= settings.simhash_hamming_threshold
                    for seen in batch_title_hashes
                ):
                    continue  # near-duplicate title already accepted in this same batch

                try:
                    embedding = await embed_text(extracted["summary"])
                except Exception:
                    continue
                similar = await find_similar(session, market.id, embedding, settings.vector_dedup_threshold)
                if similar is not None:
                    continue  # semantic duplicate of a news item already stored for this market

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
                    published_at=parse_published_at(candidate.get("published_at")),
                    credibility_score=credibility,
                    relevance_score=relevance,
                    impact_hint=impact_hint,
                    embedding=embedding,
                )
                session.add(item)
                new_items.append(item)
                batch_title_hashes.append(title_hash)
                existing_hashes.add(final_hash)

        market.last_polled_at = datetime.now(timezone.utc)
        market.next_poll_at = _next_poll_at(
            market.resolution_date, market.last_polled_at, market.poll_interval_minutes
        )

        if not new_items:
            await session.commit()
            return

        await session.flush()
        batch_id = uuid.uuid4()
        delivery_log = DeliveryLog(
            market_id=market.id, batch_id=batch_id, news_item_ids=[str(i.id) for i in new_items]
        )
        session.add(delivery_log)
        await session.commit()
        await session.refresh(delivery_log)
        log_id = str(delivery_log.id)

    redis = ctx["redis"]
    await redis.enqueue_job("deliver_batch", log_id)


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
