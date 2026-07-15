import json
from unittest import mock

import httpx

from app.llm.client import LLMClient, _retry_delay
from app.llm.schemas import QUERY_GEN_SCHEMA


# Captured before patching: the patch target app.llm.client.httpx IS the httpx
# module itself, so referring to httpx.AsyncClient inside the replacement would
# hit the patched attribute and recurse.
_RealAsyncClient = httpx.AsyncClient


def _patched_client(transport: httpx.MockTransport):
    """LLMClient builds its own AsyncClient; swap in one backed by a
    MockTransport, preserving base_url so relative paths keep working."""
    return mock.patch(
        "app.llm.client.httpx.AsyncClient",
        lambda **kwargs: _RealAsyncClient(
            transport=transport, base_url=kwargs["base_url"], timeout=kwargs.get("timeout")
        ),
    )


def _ok_response(content: dict) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(content)}}]})


async def test_generate_json_sends_schema_temperature_and_thinking_suppression():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["json"] = json.loads(request.content)
        seen["headers"] = request.headers
        return _ok_response({"queries": ["a", "b"]})

    with _patched_client(httpx.MockTransport(handler)):
        result = await LLMClient().generate_json(
            model="local", system="sys", prompt="user prompt",
            schema=QUERY_GEN_SCHEMA, name="query_gen", temperature=0.7,
        )

    assert result == {"queries": ["a", "b"]}
    payload = seen["json"]
    assert payload["temperature"] == 0.7
    assert payload["response_format"]["json_schema"]["schema"] == QUERY_GEN_SCHEMA
    assert payload["messages"][0] == {"role": "system", "content": "sys"}
    # Default settings: llm_disable_thinking=True -> suppression kwarg present,
    # and no API key -> no Authorization header.
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}
    assert "authorization" not in seen["headers"]


async def test_generate_json_retries_transient_status_then_succeeds():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            # Retry-After: 0 keeps the test instant while exercising the
            # header-driven delay path.
            return httpx.Response(429, headers={"Retry-After": "0"})
        return _ok_response({"queries": ["x"]})

    with _patched_client(httpx.MockTransport(handler)):
        result = await LLMClient().generate_json(
            model="local", system="s", prompt="p", schema=QUERY_GEN_SCHEMA, name="query_gen"
        )

    assert calls["n"] == 2
    assert result == {"queries": ["x"]}


async def test_generate_json_raises_on_wrong_shape_despite_200():
    # llama.cpp's documented fail-open: 200 + valid JSON that ignores the
    # schema must raise, not propagate a wrong-shaped dict downstream.
    transport = httpx.MockTransport(lambda request: _ok_response({"answer": "prose"}))
    with _patched_client(transport):
        try:
            await LLMClient().generate_json(
                model="local", system="s", prompt="p", schema=QUERY_GEN_SCHEMA, name="query_gen"
            )
        except ValueError as exc:
            assert "queries" in str(exc)
        else:
            raise AssertionError("expected ValueError")


async def test_non_retryable_status_raises_immediately():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400)

    with _patched_client(httpx.MockTransport(handler)):
        try:
            await LLMClient().generate_json(
                model="local", system="s", prompt="p", schema=QUERY_GEN_SCHEMA, name="query_gen"
            )
        except httpx.HTTPStatusError:
            pass
        else:
            raise AssertionError("expected HTTPStatusError")
    assert calls["n"] == 1  # 400 is not in _RETRYABLE_STATUSES


def test_retry_delay_prefers_retry_after_header():
    resp = httpx.Response(429, headers={"Retry-After": "7"})
    assert _retry_delay(resp, attempt=0) == 7.0


def test_retry_delay_falls_back_to_exponential_backoff():
    resp = httpx.Response(500)
    delay = _retry_delay(resp, attempt=2)
    assert 4.0 <= delay <= 5.0  # 2**2 + random jitter in [0, 1)
