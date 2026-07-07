import json

import httpx

from app.config import get_settings


class OpenRouterClient:
    """Thin async wrapper over OpenRouter's OpenAI-compatible HTTP API."""

    async def generate_json(self, model: str, system: str, prompt: str, schema: dict, name: str) -> dict:
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
            "temperature": 0.1,
        }
        headers = {"Authorization": f"Bearer {settings.openrouter_api_key}"}
        async with httpx.AsyncClient(base_url=settings.openrouter_base_url, timeout=120) as client:
            resp = await client.post("/chat/completions", json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        return json.loads(data["choices"][0]["message"]["content"])
