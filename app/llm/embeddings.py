import asyncio
from functools import lru_cache

from fastembed import TextEmbedding

from app.config import get_settings


@lru_cache
def _model() -> TextEmbedding:
    return TextEmbedding(model_name=get_settings().embedding_model)


async def embed_text(text: str) -> list[float]:
    return await asyncio.to_thread(_embed_sync, text[:4000])


def _embed_sync(text: str) -> list[float]:
    return next(_model().embed([text])).tolist()
