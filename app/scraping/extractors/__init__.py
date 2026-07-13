"""Per-domain content extractors: a registry of site-specific fast paths tried
*before* the generic headless-browser scrape.

Some sites can't be scraped by the generic Chromium + trafilatura path — either
they render the article entirely client-side after our load event fires, or they
gate headless browsers outright (MSN is both). But many of those same sites are
backed by a public content API their own frontend calls; hitting it directly is
faster (a single HTTP request, no browser), more reliable, and yields the full
article text. This package holds one extractor per such site.

Contract (see DomainExtractor): an extractor declares which URLs it `matches`,
and `extract` returns a scrape-result dict identical in shape to what
playwright_scraper produces — {"final_url", "text", "success"}, plus an optional
"published_at" (an ISO date/datetime string pulled from the page's own metadata,
omitted or None if not found) — or None to signal "not me / couldn't do it, fall
through to the generic scraper". Extractors must never raise for an expected
miss; scrape_urls treats None (or a swallowed error) as "fall through", so one
flaky site can't break the batch.

Register a new site by appending its extractor to EXTRACTORS below.
"""

from typing import Protocol

import httpx

from app.scraping.extractors.msn import MsnExtractor


class DomainExtractor(Protocol):
    name: str

    def matches(self, url: str) -> bool:
        """True if this extractor knows how to handle `url`."""
        ...

    async def extract(self, url: str, http: httpx.AsyncClient) -> dict | None:
        """Return {"final_url", "text", "success"} or None to fall through to the
        generic browser scrape. Must not raise on an expected miss."""
        ...


# Ordered; first match wins. One entry per site.
EXTRACTORS: list[DomainExtractor] = [MsnExtractor()]


def find_extractor(url: str) -> DomainExtractor | None:
    for extractor in EXTRACTORS:
        if extractor.matches(url):
            return extractor
    return None
