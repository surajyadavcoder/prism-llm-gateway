# Prism — LLM Gateway with Smart Routing and Semantic Caching

A production-shaped LLM gateway: one OpenAI-compatible endpoint in front of
multiple model providers, with the gateway itself making decisions in the
hot path — how hard is this prompt, which model tier should handle it, has
something like this been asked before, is this tenant still within budget.

Built for the Airtribe AI-first Software Engineering capstone.

---

## Quick start

```bash
git clone <this-repo>
cd prism
pip install -r requirements.txt

# Run with mock providers (no API key needed, fully offline-testable)
PYTHONPATH=. python3 app/main.py
```

On startup, if `ADMIN_API_KEY` isn't set, the gateway generates one and
prints it once to the console — copy it, you'll need it for `/admin/*`
endpoints (see [Admin access](#admin-access) below).

Open **http://localhost:8000/console/** for the ops dashboard, or hit the
API directly:

```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer sk-prism-team-alpha-0001" \
  -H "Content-Type: application/json" \
  -d '{"model": "fast", "messages": [{"role": "user", "content": "hello"}]}'
```

Demo keys are seeded automatically from `data/seed_keys.json` on first run
(see [Demo credentials](#demo-credentials) below).

### Using real models (OpenRouter)

By default Prism runs on deterministic mock providers (allowed by spec:
"mocks count"). To route through real models via OpenRouter:

```bash
cp .env.example .env
# edit .env: set OPENROUTER_API_KEY to your key
export $(cat .env | xargs)
export PRISM_USE_REAL_PROVIDERS=true
PYTHONPATH=. python3 app/main.py
```

**Never commit `.env` or paste your key anywhere public** — `.gitignore`
already excludes it.

### Docker

```bash
docker compose up --build
```

---

## Why Flask, not FastAPI

The original plan was FastAPI + uvicorn (async). The environment this was
built in had no outbound network access to install packages, so the whole
stack was rebuilt on Flask + Werkzeug's threaded dev server, which ships
with Python and needed zero installs. Concurrency comes from OS threads
(`threaded=True` / Gunicorn workers+threads) instead of an event loop. If
you have network access, swapping back to FastAPI is a mechanical rewrite —
none of the core logic (`app/core/*`) depends on the web framework.

For production, run behind Gunicorn (see `Dockerfile`) rather than the Flask
dev server.

---

## Architecture

```
Client
  │  POST /v1/chat/completions  (Authorization: Bearer <virtual key>)
  ▼
┌─────────────────────────────────────────────────────────┐
│ app/routers/chat.py                                       │
│  1. authenticate(key)          -- app/core/auth.py         │
│  2. check_model_allowed()                                  │
│  3. admit_rate_limit()         -- SQLite BEGIN IMMEDIATE   │
│  4. admit_budget()             -- SQLite BEGIN IMMEDIATE   │
│  5. resolve alias -> candidates -- app/core/router.py      │
│  6. semantic cache lookup      -- app/core/cache.py        │
│  7. run_with_failover()        -- retries + fallback chain │
│     └─> ProviderAdapter.complete() / .stream()             │
│         (mock or OpenRouter)   -- app/providers/*          │
│  8. compute_cost_usd()         -- app/core/cost.py          │
│  9. record spend, store cache, log request                 │
└─────────────────────────────────────────────────────────┘
  │
  ▼
SQLite (WAL mode): virtual_keys, rate_windows, request_logs,
                    cache_entries, provider_health
```

### Provider adapter interface

Every provider (mock or real) implements one interface
(`app/providers/base.py`): `complete()` and `stream()`. The gateway core
never imports a provider SDK directly. `app/providers/mock.py` and
`app/providers/openrouter.py` are interchangeable — see
`PRISM_USE_REAL_PROVIDERS` in `app/providers/mock.py`'s `PROVIDERS` dict.
Dropping in a direct OpenAI/Anthropic SDK adapter later means writing one
new file, not touching routing/retries/budgets/caching.

### Routing, retries, failover

`app/core/router.py`:
- **Alias resolution**: `fast` → `gpt-4o-mini` (fallback: `claude-haiku`),
  `smart` → `gpt-4o` (fallback: `claude-sonnet`). A concrete model name
  (e.g. `claude-sonnet`) is used as-is with no fallback chain.
- **`auto`**: classifies prompt difficulty (`app/core/difficulty.py`), then
  resolves through the `fast`/`smart` alias as above.
- **Retries**: each candidate model gets up to `max_attempts` (config:
  `gateway_config.sample.json`) tries with exponential backoff + jitter, for
  *retryable* errors only (outage, rate limit, timeout — not auth errors).
- **Failover**: if a candidate is exhausted, the next model in the fallback
  chain is tried. `x-prism-fallback: true` on the response tells the caller
  a fallback happened.
- **Streaming failover** is more limited by necessity: once a stream has
  emitted its first token to the client, a mid-stream provider failure is
  surfaced as an SSE error event rather than silently swapped — retrying
  would mean either duplicating already-sent content or the client seeing
  a confusing content jump mid-sentence. Pre-first-token failures still
  retry/fail over normally.

### Smart routing: beating the length-only baseline

`app/core/difficulty.py` scores prompts on **signals**, not length: verbs
like "prove", "optimize", "debug"; code fences; multi-constraint structure
— versus greeting/lookup/definition patterns for "easy". Raw length
contributes a small, capped bonus specifically so it can never dominate.

Run `python3 scripts/routing_eval.py --offline` to see the comparison
against a naive length-only baseline. On the 22-case eval set
(`scripts/routing_eval_set.json`, which deliberately includes
short-but-hard and long-but-trivial traps):

| Classifier | Overall accuracy | Trap-case accuracy |
|---|---|---|
| Prism auto-routing | **100%** (22/22) | **100%** (6/6) |
| Length-only baseline | 50% (11/22) | 0% (0/6) |

### Semantic cache

`app/core/cache.py` + `app/core/semantic.py`. No external embeddings API —
uses a hashed bag-of-words vector (feature hashing, à la Vowpal Wabbit):
tokenize → hash each token into one of 256 buckets → signed count → L2
normalize → cosine similarity. This keeps the whole gateway
offline-testable with zero dependencies, while still catching paraphrases
("What is the capital of France?" ↔ "Tell me France's capital city") that
exact-string matching would miss.

- **Scoped per tenant**: lookup only searches that tenant's own cache
  entries — one tenant can never read another's cached response, even for
  an identical prompt.
- **Similarity threshold** is tunable in `gateway_config.sample.json`
  (`cache.similarity_threshold`, default `0.55`) — tuned against a small
  set of paraphrase/unrelated pairs (see `tests/test_cache.py`) to catch
  real paraphrases while keeping unrelated-prompt similarity nearly zero.
- Swapping in a real embeddings endpoint later only touches
  `app/core/semantic.py`.

### Virtual keys, rate limits, budgets

`app/core/auth.py`. Keys are stored as **SHA-256 hashes**, never plaintext
— `app/core/security.py`. The plaintext key exists only in the client's
`Authorization` header and in `data/seed_keys.json` (the demo credentials
handed to users).

Concurrency correctness: rate-limit and budget admission are both
"check-then-consume" operations. Under concurrent requests, naive
read-then-write is a classic TOCTOU race that lets a key over-admit past
its limit. This is closed by running each admission check as a single
SQLite transaction opened with `BEGIN IMMEDIATE`, which grabs the write
lock *before* reading — serializing admission decisions for a given key.
Verified directly in `tests/test_auth_and_limits.py`
(`test_no_overadmission_under_concurrency`: 20 threads race for a
5-request limit; exactly 5 are admitted) and via `scripts/load_test.py`.

Distinct, documented rejection reasons: `invalid_key` (401),
`model_not_allowed` (403), `rate_limited` (429), `budget_exhausted` (402).

Every response also carries standard `X-RateLimit-Limit` /
`X-RateLimit-Remaining` / `X-RateLimit-Reset` headers (same shape as
GitHub's/Stripe's APIs), and a 429 additionally carries `Retry-After`.

### Admin access

`/admin/*` (provider health toggle, key inventory) requires a **separate**
credential from tenant virtual keys — `ADMIN_API_KEY` — sent the same way:
`Authorization: Bearer <ADMIN_API_KEY>`. This is deliberately a distinct
auth system: a leaked tenant virtual key must never be usable to take a
provider down for every other tenant. If `ADMIN_API_KEY` isn't set in the
environment, one is generated randomly at process startup and printed once
to the logs — the gateway never silently runs with an open admin surface.

```bash
curl -H "Authorization: Bearer $ADMIN_API_KEY" http://localhost:8000/admin/provider_health
```

The ops console (`/console/`) will prompt for this key on first load and
hold it in `sessionStorage` for the tab's lifetime.

### Metering, logs, usage API

Every request is logged to `request_logs` (key prefix, tenant, requested
vs. resolved model, provider, status, tokens, cost, cache hit, fallback
used, latency, stream). `GET /v1/usage/summary` aggregates by key;
`GET /v1/usage/requests` returns the raw recent log.

### Observability

Structured JSON logs (`app/core/observability.py`) with a `request_id` on
every line, carried end-to-end from admission through provider call to
completion — and returned to the client as `x-prism-request-id` — so a
single request can be traced across the whole pipeline, including in
multi-worker production logs.

---

## Header contract

Every `/v1/chat/completions` response carries:

| Header | Meaning |
|---|---|
| `x-prism-provider` | Which upstream provider actually served this (`openai` / `anthropic`) |
| `x-prism-cache` | `hit` or `miss` |
| `x-prism-fallback` | `true` if the fallback chain was used |
| `x-prism-cost-usd` | Computed cost for this request |
| `x-prism-request-id` | Trace ID for this request, also in structured logs |

Streaming responses carry `x-prism-cache` and `x-prism-request-id` as
response headers; per-chunk provider/fallback metadata isn't knowable until
the first token arrives, so it's not in the initial header set (a real
deployment could add a leading SSE `event: meta` line if a client needs it
before the first content chunk).

---

## Design decisions & known limitations

- **Budget admission checks the *existing* spend before the call, not a
  pre-estimate of this call's cost.** True cost is only known after the
  provider responds (provider-reported output tokens). This means a key
  can go slightly over budget on the one request that crosses the line,
  but it can never start a *new* request once already over — which is the
  guarantee that matters (no unbounded overspend).
- **Semantic cache is O(n) per tenant** (scans that tenant's cache entries
  and takes the best cosine match). Fine at demo/capstone scale; a real
  deployment would swap in an ANN index (faiss/pgvector) behind the same
  `lookup()`/`store()` interface.
- **Streaming failover stops at first token**, by design (see above) —
  not a limitation to fix, a deliberate trade-off to avoid duplicating or
  corrupting content the client has already rendered.
- **Provider health is DB-backed** (`provider_health` table), specifically
  so that toggling a provider down/degraded from the ops console is visible
  across all Gunicorn worker processes in production, not just the process
  that handled the admin request.
- **Difficulty classifier is rule/signal-based, not ML.** It beats the
  length-only baseline by a wide margin on the eval set (see above) without
  needing a trained model or labeled training data beyond the eval set
  itself — appropriate for a gateway-internal routing decision where
  latency matters more than marginal accuracy gains from a heavier model.

---

## Product-grade hardening

Beyond the core capstone spec, this includes what a real product team would
expect before calling a gateway shippable:

- **Admin/tenant auth separation** — see [Admin access](#admin-access).
- **API keys hashed at rest** (SHA-256), never stored or logged in plaintext.
- **Standard rate-limit headers** (`X-RateLimit-*`, `Retry-After`) on every
  response, not just an opaque 429.
- **CI pipeline** (`.github/workflows/ci.yml`) — every push/PR to `main`
  runs the full unit test suite, smoke test, routing eval, and load test.
- **Structured JSON logs with request tracing** — see
  [Observability](#observability) above.
- **DB-backed health checks** — `/healthz` actually queries the database,
  not just "process is alive," so a load balancer can tell the difference
  between "slow" and "actually broken."
- **CORS + basic security headers** on every response.
- **MIT LICENSE** for open-source clarity.

---

## Running the verification suite

```bash
# Unit tests (cost, auth/rate-limit/budget concurrency, cache, routing)
PYTHONPATH=. python3 -m unittest discover tests -v

# Start the gateway first, then:
PYTHONPATH=. python3 app/main.py &

python3 scripts/smoke_test.py
python3 scripts/load_test.py --workers 30 --requests 150
python3 scripts/routing_eval.py --offline          # or --base-url http://localhost:8000
```

## Demo credentials

Seeded from `data/seed_keys.json` on first run (do not use these in a real
deployment — they're for the local demo only):

| Key | Tenant | Allowed models | RPM | Monthly budget |
|---|---|---|---|---|
| `sk-prism-team-alpha-0001` | team-alpha | all + auto | 60 | $25 |
| `sk-prism-team-beta-0002` | team-beta | fast tier only | 20 | $5 |
| `sk-prism-team-gamma-0003` | team-gamma | all + auto | 5 | $0.05 (for testing budget exhaustion) |

## Project layout

```
app/
  main.py                Flask app entrypoint
  core/
    db.py                 SQLite schema + connection management
    config.py              Loads model_pricing / gateway_config / seed_keys
    seed.py                 Seeds virtual keys (hashed) on startup
    security.py              Key hashing
    auth.py                    Authn, allowlist, rate limit, budget (concurrency-safe)
    router.py                    Alias resolution, retries, failover
    difficulty.py                  Prompt difficulty classifier (auto routing)
    semantic.py                     Hashed bag-of-words embeddings
    cache.py                         Semantic cache, tenant-scoped
    cost.py                           Cost computation from price table
    logs.py                            Request logging to SQLite
    observability.py                    Structured JSON app logs, request IDs
  providers/
    base.py                Provider adapter interface
    mock.py                 Mock OpenAI/Anthropic providers + DB-backed health switch
    openrouter.py             Real provider adapter via OpenRouter
  routers/
    chat.py                 POST /v1/chat/completions (+ streaming)
    usage.py                  GET /v1/usage/summary, /v1/usage/requests
    admin.py                    Provider health toggle, key listing
static/
  index.html              Ops console (vanilla HTML/JS)
data/
  model_pricing.json       Price table
  seed_keys.json             Demo virtual keys
  gateway_config.sample.json  Aliases, retries, timeouts, cache config
scripts/
  smoke_test.py            Fast sanity check of core flows
  load_test.py               Concurrency correctness + latency profile
  routing_eval.py              Auto-routing accuracy vs. length baseline
  routing_eval_set.json          Labeled eval set (with traps)
tests/
  test_cost.py             Unit tests: cost computation
  test_auth_and_limits.py    Unit tests: auth, rate limit, budget (+ concurrency)
  test_cache.py                Unit tests: semantic cache, tenant isolation
  test_routing.py                Unit tests: alias resolution, difficulty classifier
Dockerfile / docker-compose.yml / requirements.txt / .env.example
```
