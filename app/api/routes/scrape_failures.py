"""Global visibility into scrape failures: which domains are blocking/failing
scraping, aggregated across all markets (see app.db.models.ScrapeFailure and the
write site in app.worker.tasks.process_candidate)."""

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import DomainFailureResponse
from app.db.models import ScrapeFailure
from app.db.session import get_session

router = APIRouter(tags=["scrape-failures"])

SAMPLE_URLS_PER_DOMAIN = 5


@router.get("/scrape-failures", response_model=list[DomainFailureResponse])
async def list_scrape_failures(session: AsyncSession = Depends(get_session)) -> list[DomainFailureResponse]:
    domain_rows = (
        await session.execute(
            select(
                ScrapeFailure.source_domain,
                func.count(ScrapeFailure.id).label("failure_count"),
                func.max(ScrapeFailure.occurred_at).label("last_occurred_at"),
            )
            .group_by(ScrapeFailure.source_domain)
            .order_by(func.count(ScrapeFailure.id).desc())
        )
    ).all()

    # Windowed subquery instead of one query per domain: rank each domain's
    # failures by recency and keep only the top N, so sample URLs come back in
    # a single round trip regardless of how many domains have failures.
    ranked = (
        select(
            ScrapeFailure.source_domain,
            ScrapeFailure.url,
            func.row_number()
            .over(partition_by=ScrapeFailure.source_domain, order_by=ScrapeFailure.occurred_at.desc())
            .label("rn"),
        )
    ).subquery()
    sample_rows = (
        await session.execute(
            select(ranked.c.source_domain, ranked.c.url).where(ranked.c.rn <= SAMPLE_URLS_PER_DOMAIN)
        )
    ).all()
    samples_by_domain: dict[str, list[str]] = {}
    for domain, url in sample_rows:
        samples_by_domain.setdefault(domain, []).append(url)

    return [
        DomainFailureResponse(
            domain=row.source_domain,
            failure_count=row.failure_count,
            last_occurred_at=row.last_occurred_at,
            sample_urls=samples_by_domain.get(row.source_domain, []),
        )
        for row in domain_rows
    ]
