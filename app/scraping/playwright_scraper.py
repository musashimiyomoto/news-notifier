import asyncio

import trafilatura
from playwright.async_api import async_playwright

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


async def scrape_urls(urls: list[str], timeout_ms: int, concurrency: int) -> dict[str, dict]:
    """Scrape a batch of URLs with a shared browser instance and a concurrency cap.

    Returns a dict keyed by the *original* candidate URL (so callers can match
    it back to the search-result metadata), with:
      - final_url: URL after following redirects (used for canonical hashing —
        important for Google News RSS links, which are redirects, not real URLs)
      - text: extracted main content (trafilatura), empty string on failure
      - success: bool
    """
    results: dict[str, dict] = {}
    if not urls:
        return results

    semaphore = asyncio.Semaphore(concurrency)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)

        async def _scrape_one(url: str) -> None:
            async with semaphore:
                context = None
                try:
                    context = await browser.new_context(user_agent=USER_AGENT)
                    page = await context.new_page()
                    await page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
                    final_url = page.url
                    html = await page.content()
                    extracted = trafilatura.extract(html, include_comments=False, favor_recall=True)
                    results[url] = {
                        "final_url": final_url,
                        "text": extracted or "",
                        "success": bool(extracted),
                    }
                except Exception as exc:  # noqa: BLE001 — any single-page failure must not abort the batch
                    results[url] = {"final_url": url, "text": "", "success": False, "error": str(exc)}
                finally:
                    if context is not None:
                        await context.close()

        try:
            # return_exceptions=True so one task's unhandled exception can't cancel
            # its siblings — each task already writes its own outcome into `results`.
            await asyncio.gather(*(_scrape_one(u) for u in urls), return_exceptions=True)
        finally:
            await browser.close()

    return results
