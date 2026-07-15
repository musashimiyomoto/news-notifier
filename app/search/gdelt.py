"""GDELT DOC 2.0 API — free, no key required.
https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/
"""

from datetime import datetime, timezone

import httpx

GDELT_ENDPOINT = "https://api.gdeltproject.org/api/v2/doc/doc"


def _parse_seendate(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


async def search_gdelt(query: str, max_records: int = 20) -> list[dict]:
    params = {
        "query": f"{query} sourcelang:english",
        "mode": "artlist",
        "maxrecords": str(max_records),
        "format": "json",
        "sort": "hybridrel",
        # Bound results to the last month. Without a timespan GDELT searches its
        # full 3-month window, so with hybridrel sorting a chunk of maxrecords is
        # routinely spent on months-old coverage that the worker's recency filter
        # (Settings.candidate_max_age_days, default 30d) then throws away —
        # wasted slots that fresher articles could have filled.
        "timespan": "1m",
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(GDELT_ENDPOINT, params=params)
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, ValueError):
        return []

    articles = data.get("articles", []) if isinstance(data, dict) else []
    results = []
    for article in articles:
        url = article.get("url")
        if not url:
            continue
        results.append(
            {
                "url": url,
                "title": article.get("title") or "",
                "source_domain": article.get("domain") or "",
                "published_at": _parse_seendate(article.get("seendate")),
                "source_name": "gdelt",
            }
        )
    return results
