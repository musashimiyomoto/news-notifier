from unittest import mock

import httpx

from app.delivery.telegram import TELEGRAM_MESSAGE_LIMIT, format_telegram_news, send_telegram_message

_RealAsyncClient = httpx.AsyncClient


def _patched_client(transport: httpx.MockTransport):
    return mock.patch(
        "app.delivery.telegram.httpx.AsyncClient",
        lambda **kwargs: _RealAsyncClient(transport=transport, timeout=kwargs.get("timeout")),
    )


def test_format_message_contains_news_fields_and_respects_limit():
    message = format_telegram_news(
        "market-1",
        {
            "title": "Important update",
            "summary": "x" * 6000,
            "url": "https://example.com/news",
            "source_domain": "example.com",
            "published_at": "2026-07-20T12:00:00+00:00",
            "credibility_score": 0.9,
            "relevance_score": 0.8,
            "impact_hint": "supports_yes",
        },
    )

    assert len(message) <= TELEGRAM_MESSAGE_LIMIT
    assert "market-1" in message
    assert "Important update" in message
    assert "https://example.com/news" in message


async def test_send_message_posts_bot_api_payload():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    with _patched_client(httpx.MockTransport(handler)):
        result = await send_telegram_message("secret-token", "-100123", "hello")

    assert result == (200, None)
    assert seen["request"].url.path == "/botsecret-token/sendMessage"
    assert seen["request"].read()
    assert b'"chat_id":"-100123"' in seen["request"].content
    assert b'"text":"hello"' in seen["request"].content


async def test_send_message_returns_telegram_error_description():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(400, json={"ok": False, "description": "chat not found"})
    )
    with _patched_client(transport):
        result = await send_telegram_message("token", "missing", "hello")

    assert result == (400, "chat not found")


async def test_send_message_rejects_invalid_success_response():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": False}))
    with _patched_client(transport):
        result = await send_telegram_message("token", "123", "hello")

    assert result == (200, "Invalid Telegram API response")


async def test_network_error_does_not_expose_bot_token():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("failed", request=request)

    with _patched_client(httpx.MockTransport(handler)):
        status, error = await send_telegram_message("super-secret-token", "1", "hello")

    assert status is None
    assert "super-secret-token" not in error
