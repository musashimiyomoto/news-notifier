import httpx

TELEGRAM_MESSAGE_LIMIT = 4096


def _shorten(value: object, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def format_telegram_news(market_id: str, news: dict) -> str:
    """Build one plain-text Telegram message while staying under its 4096-char limit."""
    title = _shorten(news.get("title") or "Untitled news", 700)
    market = _shorten(market_id, 300)
    source = _shorten(news.get("source_domain") or "unknown", 200)
    published = _shorten(news.get("published_at") or "unknown", 80)
    impact = _shorten(news.get("impact_hint") or "unknown", 80)
    url = _shorten(news.get("url"), 1000)

    relevance = news.get("relevance_score")
    credibility = news.get("credibility_score")
    if isinstance(relevance, (int, float)) and isinstance(credibility, (int, float)):
        scores = f"Relevance: {relevance:.2f} | Credibility: {credibility:.2f}"
    else:
        scores = f"Relevance: {relevance or 'unknown'} | Credibility: {credibility or 'unknown'}"

    head = f"New market news\nMarket: {market}\n\n{title}"
    tail = (
        f"Source: {source} | Published: {published}\n"
        f"Impact: {impact} | {scores}\n{url}"
    )
    summary_limit = max(0, TELEGRAM_MESSAGE_LIMIT - len(head) - len(tail) - 4)
    summary = _shorten(news.get("summary"), summary_limit)
    message = f"{head}\n\n{summary}\n\n{tail}"
    return message[:TELEGRAM_MESSAGE_LIMIT]


async def send_telegram_message(
    bot_token: str,
    chat_id: str,
    text: str,
    timeout: float = 15.0,
) -> tuple[int | None, str | None]:
    """Send a Bot API message and return (status_code, error) without raising."""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                url,
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "disable_web_page_preview": False,
                },
            )
    except httpx.HTTPError as exc:
        # Avoid persisting/logging the request URL because it contains the bot token.
        return None, f"{type(exc).__name__}: Telegram request failed"

    try:
        body = response.json()
    except ValueError:
        body = None

    if 200 <= response.status_code < 300 and isinstance(body, dict) and body.get("ok") is True:
        return response.status_code, None

    if isinstance(body, dict) and body.get("description"):
        error = str(body["description"])
    else:
        error = "Invalid Telegram API response" if 200 <= response.status_code < 300 else "Telegram API error"
    return response.status_code, error
