from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import MarketResponse, MarketSubscribeRequest, MarketUpdateRequest
from app.config import get_settings
from app.db.models import Market, MarketStatus
from app.db.session import get_session
from app.security import encrypt_secret

router = APIRouter(prefix="/markets", tags=["markets"])


@router.post("/subscribe", response_model=MarketResponse, status_code=201)
async def subscribe(
    body: MarketSubscribeRequest, request: Request, session: AsyncSession = Depends(get_session)
) -> MarketResponse:
    existing = await session.execute(select(Market).where(Market.external_market_id == body.market_id))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(409, "Market already subscribed")

    now = datetime.now(timezone.utc)
    market = Market(
        external_market_id=body.market_id,
        description=body.market_description,
        resolution_date=body.resolution_date,
        callback_url=str(body.callback_url),
        callback_secret_encrypted=encrypt_secret(body.callback_secret),
        poll_interval_minutes=body.poll_interval_minutes or get_settings().default_poll_interval_minutes,
        next_poll_at=now,
    )
    session.add(market)
    await session.commit()
    await session.refresh(market)

    # Trigger a first run right away instead of waiting for the scheduler tick,
    # so a fresh subscription gets a backfill batch on day one, not a day later.
    await request.app.state.redis.enqueue_job("process_market", str(market.id))

    return MarketResponse(
        market_id=market.external_market_id,
        status=market.status.value,
        next_poll_at=market.next_poll_at,
        created_at=market.created_at,
    )


@router.delete("/{market_id}", status_code=204)
async def unsubscribe(market_id: str, session: AsyncSession = Depends(get_session)) -> None:
    market = (
        await session.execute(select(Market).where(Market.external_market_id == market_id))
    ).scalar_one_or_none()
    if market is None:
        raise HTTPException(404, "Market not found")
    market.status = MarketStatus.paused
    await session.commit()


@router.patch("/{market_id}", response_model=MarketResponse)
async def update_market(
    market_id: str, body: MarketUpdateRequest, session: AsyncSession = Depends(get_session)
) -> MarketResponse:
    market = (
        await session.execute(select(Market).where(Market.external_market_id == market_id))
    ).scalar_one_or_none()
    if market is None:
        raise HTTPException(404, "Market not found")

    if body.market_description is not None:
        market.description = body.market_description
    if body.resolution_date is not None:
        market.resolution_date = body.resolution_date
    if body.callback_url is not None:
        market.callback_url = str(body.callback_url)
    if body.callback_secret is not None:
        market.callback_secret_encrypted = encrypt_secret(body.callback_secret)
    if body.status is not None:
        market.status = MarketStatus(body.status)

    await session.commit()
    await session.refresh(market)
    return MarketResponse(
        market_id=market.external_market_id,
        status=market.status.value,
        next_poll_at=market.next_poll_at,
        created_at=market.created_at,
    )
