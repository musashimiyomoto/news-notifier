from functools import lru_cache

from cryptography.fernet import Fernet
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Database / queue
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/news_notifier"
    redis_url: str = "redis://localhost:6379/0"

    # LLM — defaults to a local llama.cpp server (see the `llm` service in
    # docker-compose.yml), no API key needed. llm_base_url/llm_api_key point at
    # any OpenAI-compatible /chat/completions endpoint though, so switching to
    # a cloud provider (OpenRouter, etc.) is just an .env change — see the
    # commented example in .env.example. Model names are ignored by llama.cpp
    # in single-model mode; they only matter again if you point this at a
    # multi-model/router backend or a cloud provider.
    llm_base_url: str = "http://localhost:8080/v1"
    llm_api_key: str = ""
    llm_query_gen_model: str = "local"
    llm_extraction_model: str = "local"
    # Per-request HTTP timeout for LLM calls. CPU inference can be slow and the
    # first request can include model warm-up. Keep enough headroom to avoid
    # dropping a candidate on a transiently slow call; worker job_timeout must
    # exceed this value.
    llm_request_timeout_seconds: int = 600
    # Upper bound on article characters fed to the extraction prompt. This bounds
    # prompt-evaluation latency and context/KV-cache use; the lead of a news
    # article carries almost all the resolution-relevant signal.
    extraction_max_chars: int = 4000
    # The default Qwen3-4B-Instruct model is non-reasoning. When using a reasoning
    # model such as Qwen3-1.7B/8B, set this true so the client passes
    # chat_template_kwargs={"enable_thinking": false} and suppresses its long
    # <think> block. Keep false for templates that do not accept that kwarg.
    llm_disable_thinking: bool = False

    # Embeddings — computed locally via FastEmbed (ONNX), no external API call.
    # Changing the model changes embedding_dim, which requires a new migration
    # to alter app.db.models.EMBEDDING_DIM / the news_items.embedding column.
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dim: int = 384

    # Security — Fernet key used to encrypt callback_secret at rest.
    # We need it retrievable (not just hashed) to compute the HMAC signature
    # on outgoing webhooks, so it's symmetrically encrypted, not one-way hashed.
    # This default is a valid Fernet key so the app runs out of the box for
    # local dev/tests, but it's public (checked into source) — never use it
    # in anything reachable from outside your machine. Generate a real one
    # for staging/prod (see the validator below for the command).
    secret_encryption_key: str = "oMzYJ7UzyUqHOowktrH_dHkBBwMbW9QS_QGcoJMeHbU="

    # API key required on every /markets/* and /scrape-failures request (see
    # app.security.require_api_key). subscribe/patch/delete aren't just reads —
    # subscribe kicks off a recurring search+scrape+LLM pipeline, and patch can
    # hijack an existing market's callback_url/secret — so this can't be left
    # open on a public deployment. None disables the check (local dev only).
    api_key: str | None = None

    @field_validator("secret_encryption_key")
    @classmethod
    def _validate_fernet_key(cls, value: str) -> str:
        try:
            Fernet(value.encode())
        except Exception as exc:
            raise ValueError(
                "SECRET_ENCRYPTION_KEY is not a valid Fernet key. Generate one with: "
                'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
            ) from exc
        return value

    # Scheduling
    default_poll_interval_minutes: int = 24 * 60
    # Randomizes each computed next_poll_at by +/- this fraction, so markets that
    # happen to land on the same tier (e.g. a burst of subscriptions all polling
    # hourly) don't all wake up in the same worker tick — see
    # app.worker.tasks._next_poll_at. 0 disables jitter (deterministic timing,
    # useful for tests). 0.1 = up to 10% earlier or later.
    poll_jitter_fraction: float = 0.15

    # Dedup thresholds
    vector_dedup_threshold: float = 0.90
    simhash_hamming_threshold: int = 3

    # Delivery
    max_delivery_attempts: int = 6

    # Scraping
    playwright_timeout_ms: int = 15_000
    scrape_concurrency: int = 5

    # Search
    search_results_per_source: int = 8
    # Semantic candidate pre-filter (see app.llm.prefilter). Search fans every
    # query out to every source, so a market's first cycle can surface dozens of
    # fresh URLs — most only loosely related. Before paying for a scrape + a slow
    # local-LLM extraction per URL, we cheaply rank candidates by the cosine
    # similarity of their title to the market description (same FastEmbed model
    # used for dedup, milliseconds per title) and only process the strongest.
    # top_k caps how many survive; min_similarity drops obvious junk outright.
    # Set top_k to 0 to disable the pre-filter (process every fresh candidate).
    candidate_prefilter_top_k: int = 15
    candidate_prefilter_min_similarity: float = 0.30
    # Recency floor applied before the semantic pre-filter: drop candidates
    # published longer ago than this. Cheap way to skip stale coverage a broad
    # search inevitably drags in. Candidates whose published_at is missing or
    # unparseable are KEPT (sources report dates in inconsistent formats — losing
    # a good article to a bad date string is worse than processing one extra).
    # Set to 0 to disable the recency filter.
    candidate_max_age_days: int = 30


@lru_cache
def get_settings() -> Settings:
    return Settings()
