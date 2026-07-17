# news-notifier — reactive news agent for prediction markets

[![CI](https://github.com/musashimiyomoto/news-notifier/actions/workflows/ci.yml/badge.svg)](https://github.com/musashimiyomoto/news-notifier/actions/workflows/ci.yml)

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
| LLM | Local, NVIDIA GPU — llama.cpp CUDA serving Qwen3-4B-Instruct-2507 Q4_K_M by default (query generation + extraction/scoring). OpenAI-compatible, so pointing `LLM_BASE_URL` at OpenRouter/another provider needs no code changes — see `.env.example`. |
| Embeddings | Local, CPU — FastEmbed/ONNX (`BAAI/bge-small-en-v1.5`, 384 dims) |
| Search | GDELT DOC 2.0 API + Google News RSS + DuckDuckGo (best-effort) |

English-only markets assumed (see `app/llm/*` system prompts and
`sourcelang:english` filter in `app/search/gdelt.py`).

## Linux setup (Docker — recommended)

Install Docker Engine, the Compose plugin, and NVIDIA Container Toolkit. The
NVIDIA driver must work inside Linux/WSL before Docker can use the GPU. Verify
the host and Docker runtime:

```bash
docker --version
docker compose version
nvidia-smi
docker run --rm --gpus all ubuntu nvidia-smi
```

The default is tuned for a GTX 1050 with 4 GB VRAM and about 10 GB system RAM.
Leave at least 8 GB of free disk space for Docker images, the CUDA runtime,
model cache, and application data.

```bash
# 1. Config
cp .env.example .env
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# paste the output into SECRET_ENCRYPTION_KEY in .env
# no LLM API key needed — the `llm` service serves a local model by default

# 2. Build and start postgres, redis, llm, api, and worker
docker compose up -d --build

# 3. Follow startup. The first model download can take several minutes.
docker compose logs -f llm api worker
```

API is on `localhost:8000`. **First boot downloads the LLM's ~2.5GB model
file** (cached in the `llm_models` volume after that, so later restarts are
fast) — the `worker` service waits on the `llm` healthcheck before starting,
so nothing will hit it before it's actually ready.

Useful Linux commands:

```bash
docker compose ps                       # service status
docker compose logs -f worker           # worker logs
docker compose restart api worker       # restart after an app config change
docker compose down                     # stop containers, keep data/models
docker compose down -v                  # also delete DB and model volumes
docker compose up -d --build            # rebuild after a code/dependency change
```

The API container applies Alembic migrations and seeds sources whenever it
starts. To run those steps manually:

```bash
docker compose exec api alembic upgrade head
docker compose exec api python -m app.sources_seed
```

### Changing the local LLM model

The llama.cpp Hugging Face model reference is configured in `.env`; there is
no need to edit `docker-compose.yml`. Change these two values together:

```ini
# Default for a GTX 1050 with 4GB VRAM
LLM_HF_MODEL=bartowski/Qwen_Qwen3-4B-Instruct-2507-GGUF:Q4_K_M
LLM_DISABLE_THINKING=false
LLM_GPU_LAYERS=99
```

For example, to trade quality for lower VRAM use and higher speed:

```ini
LLM_HF_MODEL=bartowski/Qwen_Qwen3-1.7B-GGUF:Q4_K_M
LLM_DISABLE_THINKING=true
```

Apply the change and watch the new model download/load:

```bash
docker compose up -d --force-recreate llm worker
docker compose logs -f llm
curl --fail http://localhost:8080/health
```

The model is ready when the health request returns HTTP 200. The model files
remain in the `llm_models` volume, so switching back does not normally download
them again.

Some compatible model references and practical guidance for this GPU:

| Model reference | `LLM_DISABLE_THINKING` | Guidance |
|---|---:|---|
| `bartowski/Qwen_Qwen3-4B-Instruct-2507-GGUF:Q4_K_M` | `false` | Default; best quality/speed balance for 4 GB VRAM. |
| `bartowski/Llama-3.2-3B-Instruct-GGUF:Q4_K_M` | `false` | Faster fallback with more VRAM headroom. |
| `bartowski/Qwen_Qwen3-1.7B-GGUF:Q4_K_M` | `true` | Fastest and lightest, but weaker scoring. |
| `bartowski/Qwen_Qwen3-8B-GGUF:Q4_K_M` | `true` | Not recommended: it cannot fit in 4 GB VRAM and CPU spill is very slow. |

For another GGUF model, use llama.cpp's `owner/repository:quantization`
syntax. Prefer an Instruct model with a chat template and reliable structured
JSON output. `LLM_DISABLE_THINKING=true` is only for reasoning models whose
template supports `enable_thinking`; use `false` for ordinary Instruct models.
The worker reads this value at startup, which is why it must be restarted with
the LLM service.

Local inference tuning also lives in `.env`:

```ini
LLM_THREADS=2             # physical CPU cores assigned to one inference
LLM_PARALLEL=1            # simultaneous llama.cpp request slots
LLM_CONTEXT_SIZE=4096     # total context shared by all slots
LLM_GPU_LAYERS=99         # offload all model layers to NVIDIA CUDA
EXTRACTION_MAX_CHARS=4000 # article text sent to the extraction prompt
```

Keep `LLM_PARALLEL=1` and `LLM_CONTEXT_SIZE=4096` on a 4 GB card: another slot
or a larger context increases KV-cache use and may cause an out-of-memory error.
`LLM_GPU_LAYERS=99` asks llama.cpp to offload every available layer. After
changing the first four values, recreate `llm`; after changing
`EXTRACTION_MAX_CHARS`, restart `worker`. Confirm GPU offload with
`docker compose logs llm` (look for `offloaded ... layers to GPU`) and
`nvidia-smi` (llama.cpp should occupy roughly 3–4 GB VRAM).

## Linux setup (native api/worker — faster local iteration)

```bash
cp .env.example .env   # localhost URLs are correct for native processes
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# paste the output into SECRET_ENCRYPTION_KEY in .env
docker compose up -d postgres redis llm
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium
alembic upgrade head
python -m app.sources_seed
uvicorn app.api.main:app --reload            # API
arq app.worker.settings.WorkerSettings        # worker (separate terminal)
```

### Using a cloud LLM instead

The worker doesn't care whether `LLM_BASE_URL` points at the local `llm`
service or a cloud provider — `LLMClient` (`app/llm/client.py`) is a plain
OpenAI-compatible HTTP client. For native Linux development, comment out the
local API settings in `.env` and uncomment the OpenRouter example below them.
No code changes are needed, and the local `llm` container can remain stopped.

## API

Every `/markets/*` and `/scrape-failures` request requires
`Authorization: Bearer <API_KEY>` once `API_KEY` is set in `.env` (see
`.env.example`) — subscribe triggers a recurring search+scrape+LLM pipeline
per market, and PATCH can rewrite an existing market's `callback_url`, so an
open endpoint on anything reachable outside your network is a resource-
exhaustion / webhook-hijack risk. `API_KEY` unset disables the check, for
local dev and the `/ui` page only.

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
updates description/resolution_date/callback/status. `GET /news?limit=N`
returns the latest stored news across **all** markets (each item tagged with
its `market_id`) — this backs the "All news" tab of the built-in UI at `/ui`.

## Tests

```bash
pytest
```

Covers pure logic and mocked I/O boundaries — dedup, adaptive scheduling,
search-source merging, credibility scoring, proof verification, webhook
signing/delivery and the LLM client (both via `httpx.MockTransport`), and
market PATCH side effects — with no live DB/LLM/network calls, so it runs
without any of the infra above. CI (`.github/workflows/ci.yml`) runs the
suite on Python 3.11 and 3.12 for every push and pull request.

## Known MVP scope cuts (intentional, not oversights)

- No conflict-flagging when two sources disagree — both are delivered as
  separate items; reconciling is left to the consumer for now.
- DuckDuckGo search has no official API — it's wrapped best-effort and can
  silently return nothing if the unofficial scraper breaks upstream.
- Dead-lettered deliveries (`delivery_log.status = dead_letter`) are not
  surfaced anywhere yet — needs an admin endpoint or alert.
- `published_at` parsing is best-effort per source and may be `null`.
