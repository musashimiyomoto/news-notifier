import json

import httpx

from app.config import get_settings


class OllamaClient:
    """Thin async wrapper over the local Ollama HTTP API."""

    async def generate_json(self, model: str, system: str, prompt: str, schema: dict) -> dict:
        settings = get_settings()
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "format": schema,
            "stream": False,
            "options": {"temperature": 0.1},
        }
        async with httpx.AsyncClient(base_url=settings.ollama_host, timeout=120) as client:
            resp = await client.post("/api/chat", json=payload)
            resp.raise_for_status()
            data = resp.json()
        return json.loads(data["message"]["content"])

    async def embed(self, model: str, text: str) -> list[float]:
        settings = get_settings()
        payload = {"model": model, "input": text}
        async with httpx.AsyncClient(base_url=settings.ollama_host, timeout=60) as client:
            resp = await client.post("/api/embed", json=payload)
            resp.raise_for_status()
            data = resp.json()
        return data["embeddings"][0]
