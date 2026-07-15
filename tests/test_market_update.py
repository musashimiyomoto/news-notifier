"""PATCH /markets/{id} side effects: reactivation must kick an immediate poll,
pausing must abort in-flight jobs, and plain field edits must do neither."""

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from app.api.routes.markets import update_market
from app.api.schemas import MarketUpdateRequest
from app.db.models import Market, MarketStatus

NOW = datetime(2026, 7, 15, tzinfo=timezone.utc)


class FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class FakeSession:
    def __init__(self, market):
        self.market = market
        self.commits = 0

    async def execute(self, _stmt):
        return FakeResult(self.market)

    async def commit(self):
        self.commits += 1

    async def refresh(self, _obj):
        pass


class FakeRedis:
    """Covers both the enqueue path (reactivation) and the abort path
    (pause), which goes through smembers/zadd/delete in app.worker.abort."""

    def __init__(self):
        self.enqueued = []
        self.deleted_keys = []

    async def enqueue_job(self, name, *args, **kwargs):
        self.enqueued.append((name, args, kwargs))
        return object()

    async def smembers(self, _key):
        return set()

    async def zadd(self, _key, _mapping):
        return 0

    async def delete(self, key):
        self.deleted_keys.append(key)
        return 1


def _market(status: MarketStatus) -> Market:
    return Market(
        id=uuid.uuid4(),
        external_market_id="m1",
        description="Will it happen?",
        callback_url="https://example.com/hook",
        callback_secret_encrypted="enc",
        status=status,
        next_poll_at=NOW,
        created_at=NOW,
    )


def _request(redis: FakeRedis):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(redis=redis)))


async def test_reactivation_enqueues_an_immediate_poll():
    redis = FakeRedis()
    market = _market(MarketStatus.paused)

    response = await update_market(
        "m1", MarketUpdateRequest(status="active"), _request(redis), FakeSession(market)
    )

    assert response.status == "active"
    assert len(redis.enqueued) == 1
    name, args, kwargs = redis.enqueued[0]
    assert name == "process_market"
    assert args == (str(market.id),)
    assert kwargs["_job_id"].startswith(f"process_market:{market.id}:")


async def test_pausing_aborts_jobs_and_does_not_enqueue():
    redis = FakeRedis()
    market = _market(MarketStatus.active)

    response = await update_market(
        "m1", MarketUpdateRequest(status="paused"), _request(redis), FakeSession(market)
    )

    assert response.status == "paused"
    assert redis.enqueued == []
    # abort_market_jobs drops the per-market tracking set as its final step.
    assert redis.deleted_keys == [f"arq:market-jobs:{market.id}"]


async def test_patching_active_market_to_active_does_not_double_enqueue():
    # status="active" on an already-active market is a no-op transition:
    # its self-scheduling chain is intact, an extra run would be pure waste.
    redis = FakeRedis()
    market = _market(MarketStatus.active)

    await update_market("m1", MarketUpdateRequest(status="active"), _request(redis), FakeSession(market))

    assert redis.enqueued == []
    assert redis.deleted_keys == []


async def test_plain_field_update_triggers_no_queue_activity():
    redis = FakeRedis()
    market = _market(MarketStatus.active)
    session = FakeSession(market)

    await update_market(
        "m1", MarketUpdateRequest(market_description="new criteria"), _request(redis), session
    )

    assert market.description == "new criteria"
    assert session.commits == 1
    assert redis.enqueued == []
    assert redis.deleted_keys == []
