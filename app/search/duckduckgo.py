"""DuckDuckGo news search — best-effort only.

There is no official DDG search API (their Instant Answer API only returns
short infobox-style answers, not article listings). This uses the unofficial
`ddgs` package, which scrapes DDG's HTML result pages. It has no ToS-backed
guarantee and can start returning empty results / rate-limit without notice —
callers must treat this source as optional and never let its failure break
the overall search pipeline.
"""

import asyncio

from ddgs import DDGS


def _sync_search(query: str, max_results: int) -> list[dict]:
    with DDGS() as ddgs:
        return list(ddgs.news(query, max_results=max_results, region="us-en"))


async def search_duckduckgo(query: str, max_results: int = 20) -> list[dict]:
    try:
        raw = await asyncio.to_thread(_sync_search, query, max_results)
    except Exception:
        # Best-effort source: any failure (rate limit, layout change, network) is swallowed.
        return []

    results = []
    for item in raw:
        url = item.get("url")
        if not url:
            continue
        results.append(
            {
                "url": url,
                "title": item.get("title") or "",
                "source_domain": item.get("source") or "",
                "published_at": item.get("date"),
                "source_name": "duckduckgo",
            }
        )
    return results
