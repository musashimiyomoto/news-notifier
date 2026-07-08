# news-notifier — reactive news agent for prediction markets

Subscribe a prediction market once (question + resolution criteria + a
callback URL). The agent then, on its own schedule, searches the web,
scrapes candidate articles, scores each for relevance/credibility against
the market's resolution criteria, deduplicates against everything already
seen for that market, and pushes new, deduplicated news as a signed webhook.

## Architecture

```
POST /markets/subscribe ─▶ Postgres (markets)
                               │
                     arq cron (every minute, checks next_poll_at)
                               │
                      process_market job
                               │
        ┌───────────┬──────────┴──────────┬────────────┐
   query-gen     search (GDELT +      Playwright      extraction+
   (OpenRouter)  Google News RSS +    scrape          scoring (OpenRouter)
                 DuckDuckGo best-effort)                    │
                               │                       dedup (URL-hash /
                               └──────────────────────▶ simhash / pgvector)
                                                              │
                                                     news_items (Postgres)
                                                              │
                                                     deliver_batch job
                                                              │
                                              HMAC-signed webhook ─▶ callback_url
```

Two arq jobs, not one, on purpose: `process_market` does the (expensive,
non-repeatable-without-dedup-side-effects) search→scrape→extract→store
pipeline; `deliver_batch` only sends an already-stored batch. If a webhook
delivery fails, **only** `deliver_batch` retries — re-running the whole
pipeline on delivery failure would have the dedup logic silently swallow
the retry (the news is already in the DB) and the webhook would never
go out again.

## Stack

| Layer | Choice |
|---|---|
| API | FastAPI |
| Queue / scheduler | arq + Redis |
| DB + vector store | PostgreSQL + pgvector |
| Scraping | Playwright (Chromium) |
| LLM | OpenRouter — `openai/gpt-4o-mini` (query generation + extraction/scoring) |
| Embeddings | OpenRouter — `openai/text-embedding-3-small` (truncated to 768 dims) |
| Search | GDELT DOC 2.0 API + Google News RSS + DuckDuckGo (best-effort) |

English-only markets assumed (see `app/llm/*` system prompts and
`sourcelang:english` filter in `app/search/gdelt.py`).

## Setup (Docker — recommended)

```bash
# 1. Config
cp .env.example .env
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# paste the output into SECRET_ENCRYPTION_KEY in .env
# also paste an OpenRouter API key (https://openrouter.ai/keys) into OPENROUTER_API_KEY in .env

# 2. Everything
docker compose up --build     # postgres, redis, migrate+seed, api, worker
```

API is on `localhost:8000`.

To re-run migrations/seed after a schema change: `docker compose run --rm migrate`.

## Setup (without Docker for api/worker — faster local iteration)

```bash
docker compose up -d postgres redis
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium
cp .env.example .env   # DATABASE_URL/REDIS_URL default to localhost, already correct here
# paste an OpenRouter API key (https://openrouter.ai/keys) into OPENROUTER_API_KEY in .env
alembic upgrade head
python -m app.sources_seed
uvicorn app.api.main:app --reload            # API
arq app.worker.settings.WorkerSettings        # worker (separate terminal)
```

## API

```bash
curl -X POST localhost:8000/markets/subscribe \
  -H "Content-Type: application/json" \
  -d '{
    "market_id": "will-fed-cut-rates-sep-2026",
    "market_description": "Resolves YES if the Federal Reserve cuts the federal funds rate at its September 2026 FOMC meeting.",
    "resolution_date": "2026-09-17T18:00:00Z",
    "callback_url": "https://your-service.example.com/webhooks/news",
    "callback_secret": "a-long-random-shared-secret"
  }'
```

Webhook payload sent to `callback_url` (also see `app/worker/tasks.py`):

```json
{
  "market_id": "will-fed-cut-rates-sep-2026",
  "batch_id": "…",
  "generated_at": "…",
  "news": [
    {
      "news_id": "…", "title": "…", "summary": "…", "url": "…",
      "source_domain": "reuters.com", "published_at": "…",
      "credibility_score": 0.9, "relevance_score": 0.82,
      "impact_hint": "supports_yes", "proofs": [{"quote": "…"}]
    }
  ]
}
```

Verify authenticity via the `X-Signature: sha256=<hmac>` header
(HMAC-SHA256 of the raw request body, keyed with your `callback_secret`),
and use the `Idempotency-Key` header (= `batch_id`) to dedupe retried
deliveries on your side.

`DELETE /markets/{market_id}` pauses polling; `PATCH /markets/{market_id}`
updates description/resolution_date/callback/status.

## Tests

```bash
pytest
```

Only covers pure logic (dedup, adaptive scheduling) — no live DB/OpenRouter/
network calls, so it runs without any of the infra above.

## Known MVP scope cuts (intentional, not oversights)

- No conflict-flagging when two sources disagree — both are delivered as
  separate items; reconciling is left to the consumer for now.
- DuckDuckGo search has no official API — it's wrapped best-effort and can
  silently return nothing if the unofficial scraper breaks upstream.
- Dead-lettered deliveries (`delivery_log.status = dead_letter`) are not
  surfaced anywhere yet — needs an admin endpoint or alert.
- `published_at` parsing is best-effort per source and may be `null`.
