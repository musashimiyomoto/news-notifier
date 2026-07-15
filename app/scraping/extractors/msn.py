"""MSN content extractor.

MSN article pages are a client-rendered SPA that our headless Chromium can't
extract (the body is injected by JS after load, and networkidle never settles on
its tracker traffic). But every MSN article is backed by a content API that MSN's
own frontend calls, keyed by the id embedded in the URL:

    https://www.msn.com/en-us/news/technology/<slug>/ar-AA24wgWc
                                                       ^^^^^^^^  content id

    GET https://assets.msn.com/content/view/v2/Detail/en-us/AA24wgWc?apikey=<key>

The response is JSON with the article title and an HTML `body`; we run that HTML
through the same trafilatura extraction the generic scraper uses. Measured: full
article text (~2-4k chars) where the browser path returned nothing.

About the apikey: it is NOT a credential we own or were issued. It's a value
MSN's own web frontend sends with every one of these calls (visible to anyone in
the browser's network tab) — effectively a "this request came from the MSN web
client" marker, not authentication. This means:
  * Legal/ToS: this is an undocumented internal endpoint. Using it to pull full
    article text may run against MSN's terms, same grey area as scraping their
    pages generally — fine for an internal/research prototype, a deliberate call
    for anything outward-facing.
  * Rotation: MSN can change the key at any time. Rather than hardcode-and-hope
    (which would need a redeploy when it rotates), we treat the key as a *cache*:
    seeded with a last-known-good value, and when the API rejects it we discover
    a fresh one at runtime by sniffing MSN's own browser traffic (see
    _refresh_key). So a rotation self-heals; if discovery also fails, extract()
    returns None and the caller falls through to the generic scraper (which fails
    for MSN — i.e. no worse than before this extractor existed).
"""

import asyncio
import json
import re
import time

import httpx
import trafilatura

# Last-known-good key: only a *seed* for the runtime cache below, refreshed
# automatically if MSN rotates it. Sniffed from MSN's own frontend traffic.
_KEY_SEED = "0QfOX3Vn51YCzitbLaRkTTBadtWpgTN8NZLW0C1SEM"
_DETAIL_ENDPOINT = "https://assets.msn.com/content/view/v2/Detail/en-us/{cid}"
# A page whose frontend fires the keyed content API, used to rediscover the key.
_SEED_ARTICLE = (
    "https://www.msn.com/en-us/news/technology/"
    "apple-smart-glasses-set-to-arrive-in-2027/ar-AA24wgWc"
)
_REAL_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
# MSN article urls end in /ar-<id>; the id is alphanumeric.
_CONTENT_ID_RE = re.compile(r"/ar-([A-Za-z0-9]+)")
_APIKEY_RE = re.compile(r"[?&]apikey=([A-Za-z0-9]{20,})")

# Runtime key cache, shared across the process. Seeded, then self-updated on
# rotation. The lock serializes refreshes so a batch of MSN urls that all see a
# rejected key triggers exactly one browser sniff, not one per url.
_cached_key: str = _KEY_SEED
_key_lock = asyncio.Lock()
# None = never refreshed. NOT 0.0: time.monotonic() counts from an arbitrary
# reference (boot time on Linux/Windows), so on a freshly booted machine —
# uptime under the cooldown, e.g. a CI runner — `monotonic() - 0.0 < cooldown`
# would silently veto the very first refresh.
_last_refresh_monotonic: float | None = None
# Don't re-sniff more than this often — bounds cost if MSN is simply down.
_REFRESH_COOLDOWN_SECONDS = 300.0


async def _sniff_key_via_browser() -> str | None:
    """Open an MSN article in our headless browser and capture the apikey it
    sends to assets.msn.com. Isolated import so this module doesn't pull in
    Playwright unless a refresh is actually needed."""
    from playwright.async_api import async_playwright

    keys: set[str] = set()

    def on_request(req) -> None:
        if "assets.msn.com" in req.url:
            m = _APIKEY_RE.search(req.url)
            if m:
                keys.add(m.group(1))

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            ctx = await browser.new_context(user_agent=_REAL_UA)
            page = await ctx.new_page()
            page.on("request", on_request)
            await page.goto(_SEED_ARTICLE, timeout=20000, wait_until="domcontentloaded")
            await page.wait_for_timeout(4000)  # let the content XHRs fire
        except Exception:  # noqa: BLE001 — discovery is best-effort
            return None
        finally:
            await browser.close()

    return next(iter(keys), None)


async def _refresh_key() -> str | None:
    """Rediscover the apikey, updating the shared cache. Cooldown-guarded and
    serialized so concurrent callers share one sniff."""
    global _cached_key, _last_refresh_monotonic
    async with _key_lock:
        # Another coroutine may have refreshed while we waited on the lock.
        if (
            _last_refresh_monotonic is not None
            and time.monotonic() - _last_refresh_monotonic < _REFRESH_COOLDOWN_SECONDS
        ):
            return None
        _last_refresh_monotonic = time.monotonic()
        new_key = await _sniff_key_via_browser()
        if new_key:
            _cached_key = new_key
        return new_key


async def _fetch_detail(http: httpx.AsyncClient, content_id: str, key: str) -> httpx.Response | None:
    try:
        return await http.get(_DETAIL_ENDPOINT.format(cid=content_id), params={"apikey": key})
    except httpx.HTTPError:
        return None  # network hiccup -> caller falls through to generic scraper


class MsnExtractor:
    name = "msn"

    def matches(self, url: str) -> bool:
        return "msn.com/" in url and _CONTENT_ID_RE.search(url) is not None

    async def extract(self, url: str, http: httpx.AsyncClient) -> dict | None:
        match = _CONTENT_ID_RE.search(url)
        if match is None:
            return None
        content_id = match.group(1)

        resp = await _fetch_detail(http, content_id, _cached_key)
        # A rejected key (rotation) shows up as a non-200; try one runtime refresh
        # and retry before giving up. 200 with an empty body is a genuine miss,
        # not a key problem, so we don't refresh on that.
        if resp is not None and resp.status_code != 200:
            new_key = await _refresh_key()
            if new_key:
                resp = await _fetch_detail(http, content_id, new_key)

        if resp is None or resp.status_code != 200:
            return None
        try:
            data = resp.json()
        except ValueError:
            return None

        body_html = data.get("body") or ""
        if not body_html:
            return None
        extracted_json = trafilatura.extract(
            body_html,
            include_comments=False,
            favor_recall=True,
            output_format="json",
            with_metadata=True,
        )
        doc = json.loads(extracted_json) if extracted_json else {}
        text = doc.get("text")
        if not text:
            return None

        return {"final_url": url, "text": text, "success": True, "published_at": doc.get("date")}
