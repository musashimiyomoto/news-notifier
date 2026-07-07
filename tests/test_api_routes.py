from datetime import datetime, timezone

import pytest

from app.api.routes.markets import get_market
from app.db.models import Market, MarketStatus


class FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class FakeSession:
    def __init__(self, market):
        self.market = market

    async def execute(self, *_args, **_kwargs):
        return FakeResult(self.market)


@pytest.mark.asyncio
async def test_get_market_returns_existing_market_details():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    market = Market(
        external_market_id="market-1",
        description="Will it happen?",
        resolution_date=now,
        callback_url="https://example.com/webhook",
        callback_secret_encrypted="secret",
        next_poll_at=now,
        status=MarketStatus.active,
        created_at=now,
    )

    response = await get_market("market-1", session=FakeSession(market))

    assert response.market_id == "market-1"
    assert response.status == MarketStatus.active.value
    assert response.next_poll_at == now
    assert response.created_at == now
