import hashlib
import hmac
import json
from unittest import mock

import httpx

from app.delivery.webhook import send_webhook, sign_payload

PAYLOAD = {"market_id": "m1", "batch_id": "batch-123", "news": [{"title": "t"}]}


# Captured before patching: the patch target app.delivery.webhook.httpx IS the
# httpx module itself, so referring to httpx.AsyncClient inside the replacement
# would hit the patched attribute and recurse.
_RealAsyncClient = httpx.AsyncClient


def _patched_client(transport: httpx.MockTransport):
    """send_webhook builds its own AsyncClient internally; swap in one backed
    by a MockTransport while preserving whatever kwargs it passed."""
    return mock.patch(
        "app.delivery.webhook.httpx.AsyncClient",
        lambda **kwargs: _RealAsyncClient(transport=transport, timeout=kwargs.get("timeout")),
    )


def test_sign_payload_is_hmac_sha256_hex():
    body = b'{"a": 1}'
    expected = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert sign_payload("secret", body) == expected


async def test_success_returns_status_and_sends_signed_headers():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = request.headers
        seen["body"] = request.content
        return httpx.Response(200)

    with _patched_client(httpx.MockTransport(handler)):
        status, error = await send_webhook("https://client.example/hook", "s3cret", PAYLOAD)

    assert (status, error) == (200, None)
    # The signature must be over the exact bytes sent, keyed by the secret.
    expected_sig = hmac.new(b"s3cret", seen["body"], hashlib.sha256).hexdigest()
    assert seen["headers"]["X-Signature"] == f"sha256={expected_sig}"
    assert seen["headers"]["Idempotency-Key"] == "batch-123"
    assert seen["headers"]["Content-Type"] == "application/json"
    assert json.loads(seen["body"])["market_id"] == "m1"


async def test_non_2xx_status_is_returned_not_raised():
    with _patched_client(httpx.MockTransport(lambda request: httpx.Response(503))):
        status, error = await send_webhook("https://client.example/hook", "s", PAYLOAD)
    assert (status, error) == (503, None)


async def test_network_error_returns_none_status_with_error_text():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with _patched_client(httpx.MockTransport(handler)):
        status, error = await send_webhook("https://client.example/hook", "s", PAYLOAD)
    assert status is None
    assert "connection refused" in error
