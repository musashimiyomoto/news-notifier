"""Google News RSS search — unofficial but stable and free, no key/quota.

Note: `link` in these feeds is a news.google.com redirect, not the final
article URL. We don't try to resolve/decode it here — the Playwright scraper
follows the redirect and we use `page.url` after navigation as the canonical
URL for hashing/dedup instead.
"""

from urllib.parse import quote_plus

import feedparser
import httpx

RSS_ENDPOINT = "https://news.google.com/rss/search"


async def search_google_news(query: str, max_results: int = 20) -> list[dict]:
    url = f"{RSS_ENDPOINT}?q={quote_plus(query)}&hl=en-US&gl=US&ceid=US:en"
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
    except httpx.HTTPError:
        return []

    feed = feedparser.parse(resp.text)
    results = []
    for entry in feed.entries[:max_results]:
        link = entry.get("link")
        if not link:
            continue
        source = entry.get("source")
        source_title = source.get("title") if isinstance(source, dict) else None
        results.append(
            {
                "url": link,
                "title": entry.get("title") or "",
                "source_domain": source_title or "",
                "published_at": entry.get("published"),  # left as raw string; parsed downstream if needed
                "source_name": "google_news_rss",
            }
        )
    return results
