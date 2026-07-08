from functools import lru_cache

from cryptography.fernet import Fernet
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Database / queue
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/news_notifier"
    redis_url: str = "redis://localhost:6379/0"

    # LLM (OpenRouter — https://openrouter.ai)
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_api_key: str = ""
    llm_query_gen_model: str = "openai/gpt-4o-mini"
    llm_extraction_model: str = "openai/gpt-4o-mini"

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
