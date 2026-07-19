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
                                         HMAC webhook + optional Telegram bot
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
| LLM | Local CPU or NVIDIA GPU — llama.cpp serving Qwen3-4B-Instruct-2507 Q4_K_M by default (query generation + extraction/scoring). OpenAI-compatible, so pointing `LLM_BASE_URL` at OpenRouter/another provider needs no code changes — see `.env.example`. |
| Embeddings | Local, CPU — FastEmbed/ONNX (`BAAI/bge-small-en-v1.5`, 384 dims) |
| Search | GDELT DOC 2.0 API + Google News RSS + DuckDuckGo (best-effort) |

English-only markets assumed (see `app/llm/*` system prompts and
`sourcelang:english` filter in `app/search/gdelt.py`).

## Linux setup (Docker — recommended)

Install Docker Engine and the Compose plugin. CPU mode needs no additional
runtime. For GPU mode, also install NVIDIA Container Toolkit; the NVIDIA driver
must work inside Linux/WSL before Docker can use the GPU.

```bash
# 1. Config
cp .env.example .env
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# paste the output into SECRET_ENCRYPTION_KEY in .env
# no LLM API key needed — the `llm` service serves a local model by default

# 2a. Build and start with CPU inference
./compose.sh cpu up -d --build

# 2b. Or start with NVIDIA GPU inference
./compose.sh gpu up -d --build

# To start only the local LLM, add its service name:
./compose.sh cpu up -d llm  # or: ./compose.sh gpu up -d llm

# 3. Follow startup. The first model download can take several minutes.
./compose.sh cpu logs -f llm api worker  # use `gpu` here if started in GPU mode
```

API is on `localhost:8000`. **First boot downloads the LLM's ~2.5GB model
file** (cached in the `llm_models` volume after that, so later restarts are
fast) — the `worker` service waits on the `llm` healthcheck before starting,
so nothing will hit it before it's actually ready.

`compose.sh` forwards everything after the mode to Docker Compose, so the same
interface works for startup, logs, status, recreation, and shutdown. To switch
modes, run the target mode's `up` command; Compose recreates `llm` with the
selected image while retaining the shared model cache.

Useful Linux commands (replace `cpu` with `gpu` for the GPU configuration):

```bash
./compose.sh cpu ps                       # service status
./compose.sh cpu logs -f worker           # worker logs
./compose.sh cpu restart api worker       # restart after an app config change
./compose.sh cpu down                     # stop containers, keep data/models
./compose.sh cpu down -v                  # also delete DB and model volumes
./compose.sh cpu up -d --build            # rebuild after a code/dependency change
```

The API container applies Alembic migrations and seeds sources whenever it
starts. To run those steps manually:

```bash
./compose.sh cpu exec api alembic upgrade head
./compose.sh cpu exec api python -m app.sources_seed
```

### Changing the local LLM model

The llama.cpp Hugging Face model reference is configured in `.env`; there is
no need to edit either Compose file. Keep the model's thinking setting in sync
with the model you select:

```ini
# Default model for both CPU and GPU modes
LLM_HF_MODEL=bartowski/Qwen_Qwen3-4B-Instruct-2507-GGUF:Q4_K_M
LLM_DISABLE_THINKING=false
# Used only by the GPU configuration
LLM_GPU_LAYERS=99
```

For example, to trade quality for lower CPU/RAM/VRAM use and higher speed:

```ini
LLM_HF_MODEL=bartowski/Qwen_Qwen3-1.7B-GGUF:Q4_K_M
LLM_DISABLE_THINKING=true
```

Apply the change and watch the new model download/load:

```bash
./compose.sh cpu up -d --force-recreate llm worker  # or: ./compose.sh gpu ...
./compose.sh cpu logs -f llm                        # use the selected mode
curl --fail http://localhost:8080/health
```

The model is ready when the health request returns HTTP 200. The model files
remain in the `llm_models` volume, so switching back does not normally download
them again.

Some compatible model references and practical local-inference guidance:

| Model reference | `LLM_DISABLE_THINKING` | Guidance |
|---|---:|---|
| `bartowski/Qwen_Qwen3-4B-Instruct-2507-GGUF:Q4_K_M` | `false` | Default; best quality balance, but slower on CPU. Fits a 4 GB GPU. |
| `bartowski/Llama-3.2-3B-Instruct-GGUF:Q4_K_M` | `false` | Faster fallback with lower CPU and VRAM pressure. |
| `bartowski/Qwen_Qwen3-1.7B-GGUF:Q4_K_M` | `true` | Fastest and lightest CPU/GPU option, but weaker scoring. |
| `bartowski/Qwen_Qwen3-8B-GGUF:Q4_K_M` | `true` | Not recommended locally: too large for 4 GB VRAM and very slow on CPU. |

For another GGUF model, use llama.cpp's `owner/repository:quantization`
syntax. Prefer an Instruct model with a chat template and reliable structured
JSON output. `LLM_DISABLE_THINKING=true` is only for reasoning models whose
template supports `enable_thinking`; use `false` for ordinary Instruct models.
The worker reads this value at startup, which is why it must be restarted with
the LLM service.

Local inference tuning also lives in `.env`:

```ini
# Optional: defaults to 4 in CPU mode and 2 in GPU mode
# LLM_THREADS=4           # physical CPU cores assigned to one inference
LLM_PARALLEL=1            # simultaneous llama.cpp request slots
LLM_CONTEXT_SIZE=4096     # total context shared by all slots
LLM_GPU_LAYERS=99         # GPU mode only: offload all layers to CUDA
EXTRACTION_MAX_CHARS=4000 # article text sent to the extraction prompt
```

In CPU mode, set `LLM_THREADS` near the number of physical cores you want to
dedicate to inference. In GPU mode, keep `LLM_PARALLEL=1` and
`LLM_CONTEXT_SIZE=4096` on a 4 GB card: another slot or a larger context
increases KV-cache use and may cause an out-of-memory error.
`LLM_GPU_LAYERS=99` asks llama.cpp to offload every available layer. After
changing the first four values, recreate `llm` in the selected mode; after
changing `EXTRACTION_MAX_CHARS`, restart `worker`. Confirm GPU offload with
`./compose.sh gpu logs llm` (look for `offloaded ... layers to GPU`) and
`nvidia-smi` (llama.cpp should occupy roughly 3–4 GB VRAM).

## Linux setup (native api/worker — faster local iteration)

```bash
cp .env.example .env   # localhost URLs are correct for native processes
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# paste the output into SECRET_ENCRYPTION_KEY in .env
./compose.sh cpu up -d postgres redis llm  # use `gpu` for CUDA inference
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

### Telegram delivery

To send every new article to a Telegram user, group, or channel in addition to
the webhook, create a bot with `@BotFather` and set both values in `.env`:

```ini
TELEGRAM_BOT_TOKEN=123456789:replace-with-the-token-from-botfather
TELEGRAM_CHAT_ID=-1001234567890
```

For a private chat, message the bot first and use that chat's numeric ID. For a
channel, add the bot as an administrator and use either `@channel_name` or its
numeric `-100...` ID. Restart the worker after changing these values:

```bash
./compose.sh cpu up -d --build api worker
```

Each article is sent as a separate message. Delivery progress is stored per
channel, so retrying a Telegram failure does not resend the webhook or earlier
messages from the same batch.

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
