import asyncio
import json

import httpx
import trafilatura
from playwright.async_api import Browser, async_playwright

from app.scraping.extractors import find_extractor

# A real Chrome UA, not a self-identifying bot string. Many news sites gate or
# degrade content for obvious bots (the old NewsNotifierBot/1.0 UA measurably cut
# extraction — e.g. a control page yielded ~2x more text under a real UA), and
# trafilatura needs the full article markup to extract anything. This does mean
# we no longer announce ourselves as a bot; sites that hard-block headless
# browsers (e.g. MSN's JS-heavy SPA) still fail and fall through to scrape_failed.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


async def scrape_urls(
    urls: list[str], timeout_ms: int, concurrency: int, browser: Browser | None = None
) -> dict[str, dict]:
    """Scrape a batch of URLs with a concurrency cap.

    `browser`: an already-running Playwright Browser to reuse (see the worker's
    long-lived instance in app.worker.settings — process_candidate scrapes one
    URL per job, and launching a fresh Chromium per job costs seconds plus a
    RAM spike). When None, an ephemeral browser is launched and closed around
    the batch — the original behavior, still used as the fallback when the
    shared instance has crashed. Each URL gets its own incognito context
    either way, so nothing leaks between scrapes sharing a browser.

    Returns a dict keyed by the *original* candidate URL (so callers can match
    it back to the search-result metadata), with:
      - final_url: URL after following redirects (used for canonical hashing —
        important for Google News RSS links, which are redirects, not real URLs)
      - text: extracted main content (trafilatura), empty string on failure
      - success: bool
      - published_at: publish date pulled from the page's own metadata
        (trafilatura/htmldate reading <meta>/JSON-LD/etc.), or None if the page
        doesn't carry one. Backfills candidates whose search-source metadata
        lacked a usable date.
    """
    results: dict[str, dict] = {}
    if not urls:
        return results

    # Shared client for the domain-specific extractors (see app.scraping.extractors)
    # — they hit a site's content API directly instead of driving the browser.
    http = httpx.AsyncClient(timeout=timeout_ms / 1000, headers={"User-Agent": USER_AGENT})
    try:
        if browser is not None:
            await _scrape_batch(browser, urls, results, timeout_ms, concurrency, http)
        else:
            async with async_playwright() as playwright:
                ephemeral = await playwright.chromium.launch(headless=True)
                try:
                    await _scrape_batch(ephemeral, urls, results, timeout_ms, concurrency, http)
                finally:
                    await ephemeral.close()
    finally:
        await http.aclose()

    return results


async def _scrape_batch(
    browser: Browser,
    urls: list[str],
    results: dict[str, dict],
    timeout_ms: int,
    concurrency: int,
    http: httpx.AsyncClient,
) -> None:
    semaphore = asyncio.Semaphore(concurrency)

    async def _scrape_via_browser(url: str) -> None:
        context = None
        try:
            context = await browser.new_context(user_agent=USER_AGENT)
            page = await context.new_page()
            await page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
            # Wait for the article container so client-rendered pages have their
            # body in the DOM before we snapshot it. networkidle is unreliable
            # here — tracker-heavy SPAs never go idle — so key off the content,
            # not the network, and fall through on timeout (some valid pages
            # use none of these tags; trafilatura still extracts from the HTML).
            try:
                await page.wait_for_selector("article, main, [role=main]", timeout=timeout_ms)
            except Exception:  # noqa: BLE001 — best-effort; extract whatever loaded
                pass
            final_url = page.url
            html = await page.content()
            extracted_json = trafilatura.extract(
                html,
                include_comments=False,
                favor_recall=True,
                output_format="json",
                with_metadata=True,
            )
            doc = json.loads(extracted_json) if extracted_json else {}
            results[url] = {
                "final_url": final_url,
                "text": doc.get("text") or "",
                "success": bool(doc.get("text")),
                "published_at": doc.get("date"),
            }
        except Exception as exc:  # noqa: BLE001 — any single-page failure must not abort the batch
            results[url] = {"final_url": url, "text": "", "success": False, "error": str(exc)}
        finally:
            if context is not None:
                await context.close()

    async def _scrape_one(url: str) -> None:
        async with semaphore:
            # Try a domain-specific fast path first (e.g. MSN's content API);
            # only drive the browser if there's no extractor or it declined.
            extractor = find_extractor(url)
            if extractor is not None:
                try:
                    result = await extractor.extract(url, http)
                except Exception:  # noqa: BLE001 — a broken extractor must fall through, not fail the URL
                    result = None
                if result is not None:
                    results[url] = result
                    return
            await _scrape_via_browser(url)

    # return_exceptions=True so one task's unhandled exception can't cancel
    # its siblings — each task already writes its own outcome into `results`.
    await asyncio.gather(*(_scrape_one(u) for u in urls), return_exceptions=True)
