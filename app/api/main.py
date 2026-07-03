from contextlib import asynccontextmanager

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import FastAPI

from app.api.routes.markets import router as markets_router
from app.config import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    try:
        yield
    finally:
        await app.state.redis.close()


app = FastAPI(
    title="Prediction Market News Agent",
    description="Subscribe a market once; receive reactive webhook batches of "
    "deduplicated, scored news relevant to its resolution.",
    lifespan=lifespan,
)
app.include_router(markets_router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
