from datetime import datetime

from pydantic import BaseModel, Field, HttpUrl


class MarketSubscribeRequest(BaseModel):
    market_id: str = Field(..., description="External market ID on the client platform")
    market_description: str = Field(
        ..., description="Market question + resolution criteria (as detailed as possible — "
        "this is what relevance scoring is judged against)"
    )
    resolution_date: datetime | None = Field(
        None, description="When the market closes/resolves — drives adaptive poll frequency"
    )
    callback_url: HttpUrl
    callback_secret: str = Field(..., min_length=16, description="Used to HMAC-sign outgoing webhooks")
    poll_interval_minutes: int | None = Field(
        None, description="Override default poll interval while resolution_date is far away"
    )


class MarketUpdateRequest(BaseModel):
    market_description: str | None = None
    resolution_date: datetime | None = None
    callback_url: HttpUrl | None = None
    callback_secret: str | None = Field(None, min_length=16)
    status: str | None = Field(None, pattern="^(active|paused|resolved)$")


class MarketResponse(BaseModel):
    market_id: str
    status: str
    next_poll_at: datetime
    created_at: datetime


class NewsItemResponse(BaseModel):
    id: str
    title: str
    summary: str
    url: str
    source_domain: str
    published_at: datetime | None = None
    credibility_score: float
    relevance_score: float
    impact_hint: str
    proofs: list[dict]


class DomainFailureResponse(BaseModel):
    domain: str
    failure_count: int
    last_occurred_at: datetime
    sample_urls: list[str]
