import asyncio
import json
import random

import httpx

from app.config import get_settings

# arq only retries a job if it explicitly raises arq.worker.Retry — any other
# exception (e.g. a plain HTTPStatusError) just fails the job outright, so
# transient/rate-limit errors must be retried here, not left to the queue.
_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
_MAX_RETRIES = 3


class LLMClient:
    """Thin async wrapper over any OpenAI-compatible /chat/completions API —
    defaults to a local llama.cpp server (see Settings.llm_base_url), but
    works unmodified against OpenRouter or any other OpenAI-compatible
    provider too, since it's just an HTTP client with a configurable base_url."""

    async def generate_json(
        self, model: str, system: str, prompt: str, schema: dict, name: str, temperature: float = 0.1
    ) -> dict:
        """`temperature` defaults low for deterministic judgement tasks
        (extraction/scoring); query generation passes a higher value for
        lexical variety across queries — see app.llm.query_gen."""
        settings = get_settings()
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": name, "strict": True, "schema": schema},
            },
            "temperature": temperature,
        }
        if settings.llm_disable_thinking:
            # Suppress the <think> block after switching to a reasoning model
            # such as Qwen3-1.7B/8B. Keep this disabled for backends whose chat
            # template rejects the kwarg.
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        headers = {}
        if settings.llm_api_key:
            headers["Authorization"] = f"Bearer {settings.llm_api_key}"
        async with httpx.AsyncClient(
            base_url=settings.llm_base_url, timeout=settings.llm_request_timeout_seconds
        ) as client:
            resp = await self._post_with_retry(client, "/chat/completions", payload, headers)
        data = json.loads(resp.json()["choices"][0]["message"]["content"])
        _ensure_required_keys(data, schema, name)
        return data

    async def _post_with_retry(
        self, client: httpx.AsyncClient, path: str, payload: dict, headers: dict
    ) -> httpx.Response:
        for attempt in range(_MAX_RETRIES + 1):
            resp = await client.post(path, json=payload, headers=headers)
            if resp.status_code not in _RETRYABLE_STATUSES or attempt == _MAX_RETRIES:
                resp.raise_for_status()
                return resp
            await asyncio.sleep(_retry_delay(resp, attempt))


def _ensure_required_keys(data: dict, schema: dict, name: str) -> None:
    """llama.cpp's grammar-constrained decoding has a documented failure mode:
    if a schema feature its GBNF converter can't handle slips through, it can
    silently fall back to *unconstrained* generation instead of erroring — the
    request still returns 200 with something that may or may not be the JSON
    we asked for. `json.loads` above already catches "not JSON at all"; this
    catches "valid JSON, wrong shape" (e.g. a chatty response wrapped in a
    plausible-looking object) before it causes a confusing KeyError several
    calls away from the actual cause."""
    missing = [key for key in schema.get("required", []) if key not in data]
    if missing:
        raise ValueError(
            f"LLM response for '{name}' is missing required field(s) {missing} — "
            "the model likely didn't honor the JSON schema (see _ensure_required_keys)."
        )


def _retry_delay(resp: httpx.Response, attempt: int) -> float:
    retry_after = resp.headers.get("retry-after")
    if retry_after is not None:
        try:
            return float(retry_after)
        except ValueError:
            pass
    return (2**attempt) + random.random()
