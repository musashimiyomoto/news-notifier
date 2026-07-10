from contextlib import asynccontextmanager
from pathlib import Path

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse

from app.api.routes.markets import router as markets_router
from app.api.routes.scrape_failures import router as scrape_failures_router
from app.config import get_settings

_STATIC_DIR = Path(__file__).parent / "static"


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
app.include_router(scrape_failures_router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/demo")
async def demo_page() -> FileResponse:
    return FileResponse(_STATIC_DIR / "demo.html")


@app.post("/demo/webhook")
async def demo_webhook(request: Request) -> dict:
    """Sink for the demo page's callback_url — accepts and discards, so batch
    delivery from the demo subscription succeeds instead of retrying against
    nothing. News items are still visible via GET /markets/{id}/news either way."""
    await request.body()
    return {"status": "ok"}
