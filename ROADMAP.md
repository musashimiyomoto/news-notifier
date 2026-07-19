# News Notifier Roadmap

This document is based on a full review of the API, worker pipeline, delivery
logic, search and scraping integrations, LLM layer, database schema, migrations,
Docker configuration, and tests.

Current baseline:

- 99 tests pass in an isolated environment;
- Alembic has one current head: `0005`;
- the lockfile and installed dependencies are consistent;
- the largest risks are in the PostgreSQL + Redis/arq + worker integration,
  which is not currently covered by integration tests.

## Priority Definitions

- **P0** - Risk of host, data, or internal-service compromise. Must be fixed
  before the application is deployed on a network reachable by other machines.
- **P1** - Risk of lost news, incorrect delivery, infinite scheduling loops, or
  broken retry/idempotency guarantees. Must be fixed before production traffic.
- **P2** - Scalability, observability, maintainability, and processing-cost work.

## Product Decisions Required

- [ ] Decide whether the application is strictly local or must support public/VPS
  deployments.
- [ ] Decide whether Telegram supplements the webhook or is an independent
  delivery channel with its own success and failure state.
- [ ] Decide whether strict webhook chronology or complete late-arriving news
  coverage is more important.
- [ ] Decide whether markets should stop automatically after `resolution_date`
  and, if so, define the grace period.

---

## Phase 0: Security Hotfixes (P0)

### P0-01: Close Infrastructure Ports

PostgreSQL, Redis, llama.cpp, and arq-ui are currently published on every host
interface. PostgreSQL uses default credentials, while Redis and arq-ui have no
authentication.

Related code:

- [`docker-compose.yml`](docker-compose.yml#L8)
- [`docker-compose.yml`](docker-compose.yml#L20)
- [`docker-compose.yml`](docker-compose.yml#L72)
- [`docker-compose.yml`](docker-compose.yml#L109)

Tasks:

- [ ] Remove external `ports` from services that only need the internal Compose
  network.
- [ ] Bind diagnostic ports to `127.0.0.1` when local host access is required.
- [ ] Remove the default PostgreSQL password from Compose and load it from `.env`
  or a secret store.
- [ ] Put arq-ui behind authenticated reverse proxy access or enable it only in a
  separate debug profile.
- [ ] Add a production override without debug ports.

Acceptance criteria:

- `ss -lnt` does not show Redis, PostgreSQL, llama.cpp, or arq-ui on `0.0.0.0`;
- API and worker services still use internal Compose DNS names;
- production startup fails with a clear error when required secrets are missing.

### P0-02: Prevent SSRF in Webhook Delivery and Scraping

`callback_url` can point to loopback, private, or cloud metadata addresses.
Playwright also opens URLs from search results without validating the destination
or redirect chain.

Related code:

- [`app/api/schemas.py`](app/api/schemas.py#L15)
- [`app/worker/tasks.py`](app/worker/tasks.py#L516)
- [`app/scraping/playwright_scraper.py`](app/scraping/playwright_scraper.py#L79)

Tasks:

- [ ] Allow only HTTPS callback URLs, except for an explicit local-development
  allowlist.
- [ ] Reject loopback, private, link-local, multicast, and unspecified IP ranges.
- [ ] Validate every DNS result for a hostname, not only the original URL string.
- [ ] Repeat validation after every redirect and protect against DNS rebinding.
- [ ] Apply the same policy through Playwright request interception.
- [ ] Add tests for IPv4, IPv6, encoded IP forms, and redirects to private IPs.

Acceptance criteria:

- callback delivery and scraping reject `localhost`, `127.0.0.1`, `::1`, RFC1918
  ranges, and `169.254.169.254`;
- ordinary public HTTPS URLs and valid public redirects continue to work.

### P0-03: Enforce Safe Production Configuration

Related code:

- [`app/config.py`](app/config.py#L47)
- [`app/config.py`](app/config.py#L56)
- [`.env.example`](.env.example)

Tasks:

- [ ] Normalize an empty `API_KEY=` to `None` in local mode.
- [ ] Add `APP_ENV=development|production`.
- [ ] Require a non-empty `API_KEY` and a unique Fernet key in production.
- [ ] Reject the public development Fernet key in production.
- [ ] Limit request body sizes, including the `/ui/webhook` sink.
- [ ] Document Fernet key and callback-secret rotation.

---

## Phase 1: Scheduling and Input Correctness (P1)

### P1-01: Validate API Inputs and Settings

A negative `poll_interval_minutes` schedules the next job in the past, a naive
`resolution_date` fails when subtracted from a UTC datetime, and threshold and
jitter settings currently accept out-of-range values.

Related code:

- [`app/api/schemas.py`](app/api/schemas.py#L6)
- [`app/config.py`](app/config.py#L75)
- [`app/worker/tasks.py`](app/worker/tasks.py#L80)

Tasks:

- [ ] Restrict `poll_interval_minutes` to a reasonable range, such as 5-43200.
- [ ] Require timezone-aware `resolution_date` values and normalize them to UTC.
- [ ] Restrict `poll_jitter_fraction` to 0-1.
- [ ] Restrict credibility, relevance, and dedup thresholds to 0-1.
- [ ] Bound concurrency, timeout, result-limit, and text-size settings.
- [ ] Add maximum lengths for market ID, description, and callback URL at the
  Pydantic boundary, before database or LLM access.
- [ ] Handle concurrent subscriptions as `409` by catching the unique violation.
- [ ] Support explicitly clearing `resolution_date` through PATCH.

Acceptance criteria:

- invalid values return `422` instead of creating a job or producing a `500`;
- `_next_poll_at` never returns a past timestamp for an active market;
- naive datetimes have a dedicated regression test.

### P1-02: Define Market Completion Policy

A market currently continues polling every hour after `resolution_date` until a
client explicitly changes its status.

Tasks:

- [ ] Add a configurable post-resolution grace period.
- [ ] Mark the market resolved or reduce its cadence after the grace period.
- [ ] Do not schedule new cycles for paused or resolved markets.
- [ ] Record automatic status transitions in an audit trail.

---

## Phase 2: Reliable Delivery (P1)

### P1-03: Fix the Effective Retry Interval

The scheduler reuses the same `_job_id`, while arq retains failed results for
3600 seconds by default. The intended five-minute retry is therefore blocked for
approximately one hour.

Related code:

- [`app/worker/scheduler.py`](app/worker/scheduler.py#L64)
- [`app/worker/settings.py`](app/worker/settings.py#L55)
- [`app/worker/tasks.py`](app/worker/tasks.py#L441)

Tasks:

- [ ] Register `deliver_batch` as an arq function with `keep_result=0`, or use a
  unique job ID for every attempt.
- [ ] Add `last_attempt_at` and `next_attempt_at` to delivery storage.
- [ ] Determine retry eligibility from `next_attempt_at`, not `created_at`.
- [ ] Add exponential backoff with jitter and explicit HTTP 429 handling.
- [ ] Refuse to process dead-letter rows even if they are manually enqueued.

Acceptance criteria:

- an integration test confirms retries happen after the configured delay;
- no more than one active attempt exists for one delivery at a time;
- six failed attempts move the delivery to dead-letter deterministically.

### P1-04: Separate Webhook and Telegram State

Webhook and Telegram delivery currently share one `attempt` counter and one final
status. Webhook failures can consume the entire retry budget before Telegram gets
a meaningful retry opportunity.

Related code:

- [`app/db/models.py`](app/db/models.py#L195)
- [`app/worker/tasks.py`](app/worker/tasks.py#L500)
- [`migrations/versions/0005_telegram_delivery.py`](migrations/versions/0005_telegram_delivery.py)

Tasks:

- [ ] Introduce `delivery_channels` or outbox rows containing channel, status,
  attempt, last error, and next attempt time.
- [ ] Track webhook and Telegram idempotency and progress independently.
- [ ] Define the final semantics of `NewsItem.delivered`.
- [ ] Move delivery to a separate queue/worker so LLM jobs cannot delay alerts.
- [ ] Add an admin API for pending, failed, and dead-letter deliveries and manual
  replay.
- [ ] Handle Telegram `retry_after`, chat migration, and bot-blocked responses.

### P1-05: Record Unexpected Delivery Failures

A callback-secret decryption failure, malformed UUID, or unexpected exception
before `record_failure` leaves a delivery pending without increasing its attempt
counter.

Tasks:

- [ ] Wrap the complete delivery attempt in a single state transition.
- [ ] Classify errors as retryable or terminal.
- [ ] Store safe error codes without leaking URL credentials or bot tokens.
- [ ] Add metrics and alerts for dead-letter transitions.

---

## Phase 3: Batching and Data Completeness (P1)

### P1-06: Rework Time-Based Batch Closure

A batch is force-closed 30 minutes after creation. With `max_jobs=2`, up to 15
candidates for one market, and candidates from other markets, normal jobs can
still be waiting to start when the batch is closed.

Related code:

- [`app/worker/scheduler.py`](app/worker/scheduler.py#L18)
- [`app/worker/settings.py`](app/worker/settings.py#L78)
- [`app/worker/batching.py`](app/worker/batching.py#L69)

Tasks:

- [ ] Distinguish queued, running, lost, and terminal candidate jobs.
- [ ] Start a candidate timeout when execution begins, not when its batch is
  created.
- [ ] Close a batch when all candidates reach terminal outcomes.
- [ ] Recover or requeue lost jobs before force-closing the batch.
- [ ] Add concurrency tests for normal closure, force closure, and late
  stragglers.

Acceptance criteria:

- ordinary queue pressure does not split one batch into many solo deliveries;
- a candidate cannot close the same batch twice;
- a crash between database commit and Redis enqueue is recovered automatically.

### P1-07: Preserve Late-Arriving News

The current publication watermark permanently drops articles published before
the newest delivered article.

Related code:

- [`app/worker/tasks.py`](app/worker/tasks.py#L51)
- [`app/db/models.py`](app/db/models.py#L77)

Options:

- [ ] Recommended: store and deliver late arrivals with `out_of_order=true`.
- [ ] Alternative: store but do not deliver them, while exposing them through the
  API and recording the delivery decision.
- [ ] Do not use a publication watermark as a deduplication mechanism.

### P1-08: Avoid Reprocessing Google News Redirects

Search hashing uses the `news.google.com` redirect URL, while `NewsItem` stores
the final article URL hash. The redirect therefore appears fresh again in later
cycles.

Related code:

- [`app/search/google_news_rss.py`](app/search/google_news_rss.py#L3)
- [`app/search/aggregator.py`](app/search/aggregator.py#L47)
- [`app/worker/tasks.py`](app/worker/tasks.py#L142)

Tasks:

- [ ] Store source URL alias hashes for each `NewsItem`.
- [ ] Alternatively, resolve Google redirects with a cheap HTTP request before
  candidate fan-out.
- [ ] Check candidate hash, final hash, and aliases during deduplication.
- [ ] Add a regression test proving that one RSS redirect does not trigger a
  second scrape and LLM call after the final URL is stored.

---

## Phase 4: Search, Scraper, and LLM Resilience (P1/P2)

### P1-09: Complete LLM Retry and Response Validation

The LLM client retries selected HTTP statuses but not connection or read
timeouts. Structured output validation checks required keys only, not types,
enums, item counts, or numeric ranges.

Related code:

- [`app/llm/client.py`](app/llm/client.py#L57)
- [`app/llm/client.py`](app/llm/client.py#L68)
- [`app/worker/tasks.py`](app/worker/tasks.py#L323)

Tasks:

- [ ] Retry eligible transport errors within a bounded total time budget.
- [ ] Bound and parse `Retry-After`, including HTTP-date values.
- [ ] Validate the complete JSON Schema, including types, enums, item counts, and
  numeric ranges.
- [ ] Do not classify a temporary LLM failure as terminal `extraction_error`.
- [ ] Align worker `job_timeout` with the worst-case retry budget.
- [ ] Add circuit-breaker or health-state behavior for an unavailable LLM.

### P2-01: Add Search-Source Observability

`search_all_sources` and individual adapters return empty lists on failure without
logs or metrics. A completely broken source is indistinguishable from a valid
zero-result response.

Tasks:

- [ ] Log source, query, latency, and error category without sensitive data.
- [ ] Add success, error, latency, and result-count metrics per source.
- [ ] Distinguish zero results from upstream failure.
- [ ] Isolate the blocking DuckDuckGo client in a process executor or dedicated
  worker so a stuck thread cannot occupy the pipeline indefinitely.

### P2-02: Bound Scraping Cost

Tasks:

- [ ] Block images, video, fonts, and trackers through Playwright routes.
- [ ] Limit HTML and domain-extractor response sizes.
- [ ] Move `trafilatura.extract` out of the event loop into a thread or process
  executor.
- [ ] Reuse HTTP clients and connection pools.
- [ ] Match normalized hostnames instead of searching for the `"msn.com/"`
  substring.
- [ ] Record a legal/product decision about the undocumented MSN content API.

---

## Phase 5: Database and Scalability (P2)

### P2-03: Shorten Database Transactions

`process_market` holds an AsyncSession during query generation, HTTP search, and
embedding prefilter work.

Related code:

- [`app/worker/tasks.py`](app/worker/tasks.py#L134)
- [`app/worker/tasks.py`](app/worker/tasks.py#L197)

Tasks:

- [ ] Read a market snapshot and close the session before external calls.
- [ ] Open a short transaction to recheck status and create the batch.
- [ ] Add optimistic locking or a version column to mutable Market state.
- [ ] Do not overwrite scheduling state after a concurrent pause or update.

### P2-04: Add Indexes, Pagination, and Retention

Tasks:

- [ ] Add cursor pagination to `/markets/{id}/news`.
- [ ] Add composite indexes for delivery status/time and batch status/time.
- [ ] Remove the full scan of all title simhash values under the advisory lock.
- [ ] Add retention or archival policies for scrape failures, closed batches,
  candidate ledgers, and delivery logs.
- [ ] Add a time range and pagination to `/scrape-failures`.
- [ ] Verify filtered HNSW behavior and exclude `NULL` embeddings from the vector
  dedup query.

### P2-05: Use the Source Reliability Score

`Source.reliability_score` is seeded, but `compute_credibility` uses only the
fixed score associated with the source tier.

Related code:

- [`app/sources_seed.py`](app/sources_seed.py#L10)
- [`app/scoring/credibility.py`](app/scoring/credibility.py#L32)

Tasks:

- [ ] Use `source.reliability_score` as the base score.
- [ ] Keep the tier score as a fallback for sources without an individual value.
- [ ] Normalize hostnames for lowercase, port removal, IDNA, and public suffixes.
- [ ] Add a test proving that two sources in the same tier can receive different
  scores.

### P2-06: Align Embedding Configuration with the Schema

Tasks:

- [ ] Either remove configurable `EMBEDDING_DIM` and explicitly fix the model at
  384 dimensions.
- [ ] Or validate model dimensions and provide a managed vector column/index
  migration when the model changes.
- [ ] Prevent worker startup when model and database dimensions are incompatible.

---

## Phase 6: Deployment, Health, and CI (P1/P2)

### P1-10: Remove the Migration/Worker Startup Race

`depends_on: condition: service_started` does not guarantee that `alembic upgrade
head` in the API entrypoint has completed.

Related code:

- [`docker-compose.yml`](docker-compose.yml#L53)
- [`docker-entrypoint.sh`](docker-entrypoint.sh#L8)

Tasks:

- [ ] Add a one-shot `migrate` service with
  `condition: service_completed_successfully`.
- [ ] Start API and worker services only after migrations succeed.
- [ ] Add an API readiness probe for PostgreSQL and Redis.
- [ ] Keep a simple liveness probe that does not depend on external services.

### P2-07: Add Integration Tests for the Critical Pipeline

The current CI suite primarily covers pure logic and mocked HTTP calls. It does
not exercise real PostgreSQL, Redis/arq, advisory locks, migrations, or delivery
state transitions.

Related code:

- [`.github/workflows/ci.yml`](.github/workflows/ci.yml#L1)
- [`tests`](tests)

Minimum integration suite:

- [ ] Upgrade an empty database to the latest Alembic head and check schema drift.
- [ ] Prove two concurrent candidate jobs cannot store the same duplicate.
- [ ] Prove one batch creates exactly one DeliveryLog.
- [ ] Prove force closure and late stragglers do not lose NewsItems.
- [ ] Prove a failed webhook is retried after the configured backoff.
- [ ] Prove Telegram retry does not resend a successful webhook.
- [ ] Prove pause/resolve cancels jobs and suppresses delivery.
- [ ] Recover a crash between database commit and Redis enqueue.
- [ ] Verify SSRF policy for private IPs and redirects.

### P2-08: Add Quality Gates and Observability

Tasks:

- [ ] Add Ruff and static type checking.
- [ ] Add a coverage report with thresholds for critical modules.
- [ ] Add dependency and security auditing.
- [ ] Pin production dependencies and container images through lockfiles or
  digests.
- [ ] Add metrics for queue depth, job latency, source failures, LLM latency,
  candidate outcomes, delivery retries, and dead-letter count.
- [ ] Add correlation IDs for market, batch, candidate, and delivery attempt.
- [ ] Use structured JSON logging and redact secrets from errors.

---

## Recommended Release Sequence

### Release 1: Safe Deployment

- P0-01, P0-02, P0-03;
- P1-01;
- P1-10.

Outcome: the service can be deployed more safely on a VPS without exposing its
database and queue or allowing worker requests into the internal network.

### Release 2: Delivery Guarantees

- P1-03, P1-04, P1-05;
- P1-06;
- core delivery integration tests.

Outcome: retry timing matches configuration, webhook and Telegram delivery are
independent, and normal queue pressure does not prematurely close batches.

### Release 3: Pipeline Completeness and Cost

- P1-07, P1-08, P1-09;
- P2-01, P2-02, P2-03.

Outcome: late-arriving articles are preserved, Google redirects do not trigger
repeat LLM analysis, and temporary upstream failures recover automatically.

### Release 4: Operations and Scalability

- P2-04, P2-05, P2-06;
- P2-07, P2-08;
- P1-02 after the product policy is chosen.

Outcome: controlled table growth, meaningful CI coverage, and observable
production operation.

## Definition of Done

A roadmap item is complete when:

- [ ] the change has a unit or integration regression test;
- [ ] schema changes include verified upgrade and downgrade migrations;
- [ ] README and `.env.example` are updated for new configuration;
- [ ] errors are observable through logs or metrics without exposing credentials;
- [ ] behavior after process, Redis, and PostgreSQL restarts is defined and tested;
- [ ] delivery and candidate-processing idempotency remain intact.
