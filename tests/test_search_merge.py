from unittest import mock

from app.search import aggregator


def _sources(gdelt=None, google=None, ddg=None):
    """Patch all three source searchers at once; each is an async callable or
    a list to return."""
    async def as_coro(value, *_args):
        if isinstance(value, Exception):
            raise value
        return value or []

    return (
        mock.patch.object(aggregator.gdelt, "search_gdelt", lambda q, n: as_coro(gdelt)),
        mock.patch.object(aggregator.google_news_rss, "search_google_news", lambda q, n: as_coro(google)),
        mock.patch.object(aggregator.duckduckgo, "search_duckduckgo", lambda q, n: as_coro(ddg)),
    )


async def test_same_article_across_sources_is_merged_once():
    # Same URL modulo case and tracking params — one canonical candidate.
    p1, p2, p3 = _sources(
        gdelt=[{"url": "https://example.com/story?utm_source=gdelt", "title": "A"}],
        google=[{"url": "https://Example.com/story", "title": "A (Google)"}],
    )
    with p1, p2, p3:
        merged = await aggregator.search_all_sources(["q"], per_source=5)
    assert len(merged) == 1
    assert merged[0]["canonical_url_hash"] == aggregator.url_hash("https://example.com/story")


async def test_failing_source_is_swallowed_others_survive():
    # DDG is explicitly best-effort (unofficial scraper) — its failure must
    # never break the overall search fan-out.
    p1, p2, p3 = _sources(
        gdelt=[{"url": "https://example.com/a", "title": "A"}],
        ddg=RuntimeError("ddg upstream broke"),
    )
    with p1, p2, p3:
        merged = await aggregator.search_all_sources(["q"], per_source=5)
    assert [c["url"] for c in merged] == ["https://example.com/a"]


async def test_items_without_url_are_skipped():
    p1, p2, p3 = _sources(gdelt=[{"title": "no url"}, {"url": "", "title": "empty"}])
    with p1, p2, p3:
        merged = await aggregator.search_all_sources(["q"], per_source=5)
    assert merged == []


async def test_every_candidate_gets_a_canonical_hash():
    p1, p2, p3 = _sources(
        gdelt=[{"url": "https://a.example/1", "title": "1"}],
        google=[{"url": "https://b.example/2", "title": "2"}],
    )
    with p1, p2, p3:
        merged = await aggregator.search_all_sources(["q"], per_source=5)
    assert len(merged) == 2
    assert all(c["canonical_url_hash"] for c in merged)
