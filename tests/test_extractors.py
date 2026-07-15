import asyncio
import time

import httpx

from app.scraping.extractors import find_extractor
from app.scraping.extractors import msn as msn_mod
from app.scraping.extractors.msn import MsnExtractor

MSN_URL = "https://www.msn.com/en-us/news/technology/some-slug/ar-AA24wgWc"

_ARTICLE_BODY = (
    "<html><body><article>"
    "<h1>Apple smart glasses</h1>"
    "<p>Apple will ship AR glasses in 2027 according to multiple supply-chain "
    "sources familiar with the company's roadmap and internal timelines.</p>"
    "<p>The device is said to use a lightweight titanium frame and dual "
    "micro-OLED displays, with mass-production trials beginning next year.</p>"
    "<p>Analysts caution that display yield could still push the launch later.</p>"
    "</article></body></html>"
)


def test_find_extractor_matches_msn_article_urls():
    assert find_extractor(MSN_URL).name == "msn"


def test_find_extractor_returns_none_for_regular_sites():
    assert find_extractor("https://www.cnet.com/tech/some-article/") is None


def test_msn_matches_requires_content_id():
    ex = MsnExtractor()
    assert ex.matches(MSN_URL) is True
    # An msn.com url without the /ar-<id> article segment isn't an article page.
    assert ex.matches("https://www.msn.com/en-us/news") is False


def _extract(handler) -> dict | None:
    """Run MsnExtractor.extract against a mocked transport, synchronously (the
    suite's pytest-asyncio plugin isn't active, so drive the coroutine directly)."""

    async def run() -> dict | None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await MsnExtractor().extract(MSN_URL, http)

    return asyncio.run(run())


def test_msn_extract_returns_clean_text_from_content_api():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "AA24wgWc" in str(request.url)  # id from the url reaches the API path
        assert request.url.params.get("apikey")
        return httpx.Response(200, json={"title": "Apple smart glasses", "body": _ARTICLE_BODY})

    result = _extract(handler)
    assert result is not None
    assert result["success"] is True
    assert result["final_url"] == MSN_URL
    assert "AR glasses" in result["text"]


def test_msn_extract_falls_through_on_non_200():
    # A non-200 triggers one key-refresh attempt; with the sniff stubbed to find
    # nothing, extract still gives up cleanly (no real browser launch).
    result, sniffs = _extract_with_sniff(lambda request: httpx.Response(404), None)
    assert sniffs == 1
    assert result is None


def test_msn_extract_falls_through_on_empty_body():
    assert _extract(lambda request: httpx.Response(200, json={"title": "t", "body": ""})) is None


def test_msn_extract_falls_through_on_network_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    assert _extract(handler) is None


def _extract_with_sniff(handler, sniff_result, *, cooldown_ok=True):
    """Run extract with the browser key-sniff stubbed out, from a clean key-cache
    state (so cooldown/cache carryover between tests can't mask the behavior)."""
    calls = {"sniff": 0}

    async def fake_sniff():
        calls["sniff"] += 1
        return sniff_result

    # Reset shared cache/cooldown, seed a known-bad current key. None means
    # "never refreshed" — 0.0 would silently arm the cooldown on any machine
    # whose monotonic clock (uptime) is still below the cooldown window.
    msn_mod._cached_key = "STALE_KEY"
    msn_mod._last_refresh_monotonic = None if cooldown_ok else time.monotonic()
    original = msn_mod._sniff_key_via_browser
    msn_mod._sniff_key_via_browser = fake_sniff
    try:
        result = _extract(handler)
    finally:
        msn_mod._sniff_key_via_browser = original
        msn_mod._cached_key = msn_mod._KEY_SEED
        msn_mod._last_refresh_monotonic = None
    return result, calls["sniff"]


def test_msn_rotation_self_heals_by_sniffing_a_fresh_key():
    # First call (stale key) 403s; after a refresh, the fresh key returns 200.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("apikey") == "FRESH_KEY":
            return httpx.Response(200, json={"title": "t", "body": _ARTICLE_BODY})
        return httpx.Response(403)

    result, sniffs = _extract_with_sniff(handler, "FRESH_KEY")
    assert sniffs == 1
    assert result is not None and result["success"] is True


def test_msn_rotation_gives_up_when_sniff_finds_nothing():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    result, sniffs = _extract_with_sniff(handler, None)
    assert sniffs == 1
    assert result is None


def test_msn_empty_body_does_not_trigger_a_key_refresh():
    # 200 with empty body is a genuine miss, not a key problem — must not sniff.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"title": "t", "body": ""})

    result, sniffs = _extract_with_sniff(handler, "FRESH_KEY")
    assert sniffs == 0
    assert result is None
