from app.config import get_settings
from app.llm.client import OllamaClient


async def embed_text(text: str) -> list[float]:
    settings = get_settings()
    client = OllamaClient()
    return await client.embed(settings.embedding_model, text[:4000])
