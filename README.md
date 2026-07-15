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
                  process_market self-schedules its own
                  next run via arq _defer_until=next_poll_at
                               │
                      process_market job
                               │
        ┌───────────┬──────────┴──────────┬────────────┐
   query-gen     search (GDELT +      Playwright      extraction+
   (local LLM)   Google News RSS +    scrape          scoring (local LLM)
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

**Scheduling:** each `process_market` run re-enqueues its own next run at the
end (`_defer_until=next_poll_at`, jittered by `POLL_JITTER_FRACTION` — default
±15% — so markets sharing a cadence tier don't all wake up in the same worker
tick). A low-frequency cron (`enqueue_due_markets`, every 10 minutes) is a
safety net that re-enqueues any market whose `next_poll_at` is more than 5
minutes overdue — i.e. one whose self-scheduled job was lost to a worker
crash or Redis eviction — rather than the primary driver of cadence.

## Stack

| Layer | Choice |
|---|---|
| API | FastAPI |
| Queue / scheduler | arq + Redis |
| DB + vector store | PostgreSQL + pgvector |
| Scraping | Playwright (Chromium) |
| LLM | Local, CPU — llama.cpp serving Qwen3-4B-Instruct-2507 (query generation + extraction/scoring). OpenAI-compatible, so pointing `LLM_BASE_URL` at OpenRouter/another provider needs no code changes — see `.env.example`. |
| Embeddings | Local, CPU — FastEmbed/ONNX (`BAAI/bge-small-en-v1.5`, 384 dims) |
| Search | GDELT DOC 2.0 API + Google News RSS + DuckDuckGo (best-effort) |

English-only markets assumed (see `app/llm/*` system prompts and
`sourcelang:english` filter in `app/search/gdelt.py`).

## Setup (Docker — recommended)

```bash
# 1. Config
cp .env.example .env
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# paste the output into SECRET_ENCRYPTION_KEY in .env
# no LLM API key needed — the `llm` service serves a local model by default

# 2. Everything
docker compose up --build     # postgres, redis, llm, api, worker
```

API is on `localhost:8000`. **First boot downloads the LLM's ~2.5GB model
file** (cached in the `llm_models` volume after that, so later restarts are
fast) — the `worker` service waits on the `llm` healthcheck before starting,
so nothing will hit it before it's actually ready.

To re-run migrations/seed after a schema change: `docker compose run --rm migrate`.

## Setup (Windows, no Docker, NVIDIA GPU)

See [`windows/README.md`](windows/README.md) — native Postgres/Memurai/llama.cpp
(CUDA) plus PowerShell scripts (`windows/*.ps1`) with switchable LLM model
presets. No code changes; the GPU serves the same OpenAI-compatible endpoint
the worker already talks to.

## Setup (without Docker for api/worker — faster local iteration)

```bash
docker compose up -d postgres redis llm
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium
cp .env.example .env   # DATABASE_URL/REDIS_URL/LLM_BASE_URL default to localhost, already correct here
alembic upgrade head
python -m app.sources_seed
uvicorn app.api.main:app --reload            # API
arq app.worker.settings.WorkerSettings        # worker (separate terminal)
```

### Using a cloud LLM instead

The worker doesn't care whether `LLM_BASE_URL` points at the local `llm`
service or a cloud provider — `LLMClient` (`app/llm/client.py`) is a plain
OpenAI-compatible HTTP client. To go back to OpenRouter (or any other
OpenAI-compatible API): comment out the local `LLM_*` block in `.env` and
uncomment the OpenRouter example right below it — no code changes, and you
can leave the `llm` container stopped since nothing else depends on it.

## API

Every `/markets/*` and `/scrape-failures` request requires
`Authorization: Bearer <API_KEY>` once `API_KEY` is set in `.env` (see
`.env.example`) — subscribe triggers a recurring search+scrape+LLM pipeline
per market, and PATCH can rewrite an existing market's `callback_url`, so an
open endpoint on anything reachable outside your network is a resource-
exhaustion / webhook-hijack risk. `API_KEY` unset disables the check, for
local dev and the `/demo` page only.

```bash
curl -X POST localhost:8000/markets/subscribe \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $API_KEY" \
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

Only covers pure logic (dedup, adaptive scheduling) — no live DB/LLM/
network calls, so it runs without any of the infra above.

## Known MVP scope cuts (intentional, not oversights)

- No conflict-flagging when two sources disagree — both are delivered as
  separate items; reconciling is left to the consumer for now.
- DuckDuckGo search has no official API — it's wrapped best-effort and can
  silently return nothing if the unofficial scraper breaks upstream.
- Dead-lettered deliveries (`delivery_log.status = dead_letter`) are not
  surfaced anywhere yet — needs an admin endpoint or alert.
- `published_at` parsing is best-effort per source and may be `null`.
