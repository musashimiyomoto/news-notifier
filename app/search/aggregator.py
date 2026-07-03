"""Merge results from all search sources and drop within-batch URL duplicates
before anything gets scraped. Cross-run duplicates (URLs already stored for a
market) are filtered separately in app.worker.tasks against the DB."""

import asyncio
import hashlib
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.search import duckduckgo, gdelt, google_news_rss

TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "gclid", "fbclid", "ref"}


def parse_published_at(value: object) -> datetime | None:
    """Each source hands back published-date in a different shape (GDELT already
    parses to datetime, Google News RSS gives RFC822 strings, DDG gives loose
    ISO-ish strings) — normalize them all to an aware UTC datetime, or None if
    unparseable (better to lose the field than crash the whole batch on it)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            return parsedate_to_datetime(value)
        except (TypeError, ValueError):
            pass
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def normalize_url(url: str) -> str:
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k.lower() not in TRACKING_PARAMS]
    normalized = parts._replace(query=urlencode(query), fragment="")
    return urlunsplit(normalized).rstrip("/")


def url_hash(url: str) -> str:
    return hashlib.sha256(normalize_url(url).encode()).hexdigest()


async def search_all_sources(queries: list[str], per_source: int) -> list[dict]:
    """Fan out every query to every source concurrently, merge, dedup by URL hash."""
    tasks = []
    for query in queries:
        tasks.append(gdelt.search_gdelt(query, per_source))
        tasks.append(google_news_rss.search_google_news(query, per_source))
        tasks.append(duckduckgo.search_duckduckgo(query, per_source))

    results_lists = await asyncio.gather(*tasks, return_exceptions=True)

    seen_hashes: set[str] = set()
    merged: list[dict] = []
    for results in results_lists:
        if isinstance(results, BaseException) or not results:
            continue
        for item in results:
            url = item.get("url")
            if not url:
                continue
            h = url_hash(url)
            if h in seen_hashes:
                continue
            seen_hashes.add(h)
            item["canonical_url_hash"] = h
            merged.append(item)
    return merged
