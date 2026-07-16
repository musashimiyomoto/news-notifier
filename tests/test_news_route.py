import uuid
from datetime import datetime, timezone

from app.api.routes.news import list_all_news
from app.db.models import ImpactHint, NewsItem

NOW = datetime(2026, 7, 15, tzinfo=timezone.utc)


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class FakeSession:
    def __init__(self, rows):
        self.rows = rows

    async def execute(self, _stmt):
        return FakeResult(self.rows)


def _item(title: str) -> NewsItem:
    return NewsItem(
        id=uuid.uuid4(),
        market_id=uuid.uuid4(),
        url=f"https://example.com/{title}",
        canonical_url_hash="h" * 64,
        title=title,
        summary=f"summary of {title}",
        proofs=[{"quote": "q"}],
        source_domain="example.com",
        published_at=NOW,
        credibility_score=0.8,
        relevance_score=0.9,
        impact_hint=ImpactHint.supports_yes,
    )


async def test_global_feed_tags_each_item_with_its_market():
    rows = [(_item("a"), "market-one"), (_item("b"), "market-two")]

    response = await list_all_news(limit=100, session=FakeSession(rows))

    assert [r.market_id for r in response] == ["market-one", "market-two"]
    assert response[0].title == "a"
    assert response[0].impact_hint == "supports_yes"
    assert response[0].proofs == [{"quote": "q"}]


async def test_global_feed_empty_db_returns_empty_list():
    assert await list_all_news(limit=100, session=FakeSession([])) == []
