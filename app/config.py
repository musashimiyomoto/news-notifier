from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Database / queue
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/murderer"
    redis_url: str = "redis://localhost:6379/0"

    # LLM (Ollama, self-hosted)
    ollama_host: str = "http://localhost:11434"
    llm_query_gen_model: str = "qwen3:4b"
    llm_extraction_model: str = "qwen3:8b"
    embedding_model: str = "nomic-embed-text"
    embedding_dim: int = 768

    # Security — Fernet key used to encrypt callback_secret at rest.
    # We need it retrievable (not just hashed) to compute the HMAC signature
    # on outgoing webhooks, so it's symmetrically encrypted, not one-way hashed.
    secret_encryption_key: str = "change-me-generate-a-fernet-key"

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
