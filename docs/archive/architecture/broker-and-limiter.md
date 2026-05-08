# Broker (SQS) and Limiter (Redis) — why both, what each does

**Status:** Canonical
**Owner:** Platform
**Last updated:** 2026-05-08 (Step 29.y close)
**Drift token:** `D-redis-url-centralize-via-settings-2026-05-08`

## TL;DR

Luciel prod runs **two distinct datastores** for two distinct concerns:

| Component | Backend | Used for | Why this backend |
|---|---|---|---|
| Celery broker | **SQS** (`luciel-memory-tasks`, `luciel-memory-dlq`) | Async task queue for `memory_extraction` and any future background work | At-least-once delivery, durable visibility timeouts, native DLQ, no MULTI/EXEC cluster constraints |
| Rate-limit storage | **ElastiCache Redis** (`luciel-redis-0001-001`) | SlowAPI shared token-bucket counters across backend container instances | Sub-ms atomic INCR with TTL; correct semantics under N replicas |

These are not duplicates. Removing either breaks a specific guarantee the product makes.

## Why we don't merge them

### Could we use Redis as the broker too?

We considered it. **No**, for two reasons:

1. **Cluster-mode incompatibility.** Celery's kombu Redis transport uses multi-key MULTI/EXEC pipelines. ElastiCache in cluster mode (which we'd want at scale) enforces ClusterCrossSlot and rejects those pipelines. Worker would crash on enqueue.
2. **Wrong durability semantics.** Redis is a cache; SQS is a queue. A failed Redis node loses in-flight messages. SQS persists across AZ failures by design and provides DLQ for poison messages — both critical for the audit-trail-bearing memory_extraction pipeline.

### Could we use SQS as the limiter store?

**No.**

1. **Latency.** SQS read latency is ~50–100ms baseline. Per-request rate limiting needs <5ms or it dominates request budget.
2. **Wrong primitive.** SQS has no INCR-with-TTL. Building a token bucket on SQS would mean polling, which inverts the semantics.
3. **Cost.** Per-request SQS calls at limiter volume would cost orders of magnitude more than a `cache.t4g.micro` flat fee.

### Could we drop Redis and accept per-container limits?

In theory. **In practice, no.**

- Backend currently runs N≥2 containers behind the ALB. Without shared state, a tenant who hits container A then container B doubles their effective rate limit per round-trip.
- Per-container limits make pricing tiers unenforceable. A "100 req/min" limit becomes "100×N req/min" depending on autoscaling decisions the customer cannot see.
- In-memory fallback in `app/middleware/rate_limit.py` is the **degraded** mode that activates when Redis is unreachable, not the design target. The fallback middleware returns 503 on writes during this state to preserve quota integrity (see `WRITE_METHODS` in `rate_limit.py`).

## The split, code-side

### Settings (single source of truth)

`app/core/config.py` defines `Settings.redis_url` (default `redis://localhost:6379/0` for dev). Prod overrides via the `REDIS_URL` env var injected from SSM `/luciel/production/REDIS_URL` by both backend and worker ECS task definitions.

All four code locations read this through `app.core.config.settings`:

| File | Reads | Purpose |
|---|---|---|
| `app/middleware/rate_limit.py` | `settings.redis_url` | SlowAPI storage URI |
| `app/worker/celery_app.py` | `settings.redis_url` (as fallback) | Celery broker URL when `CELERY_BROKER_URL` is unset |
| `app/verification/_infra_probes.py` | `settings.redis_url` (as fallback) | Mode-gate broker probe |
| `app/core/config.py` | the field itself | Definition |

`CELERY_BROKER_URL` (broker-selection state, e.g. `sqs://`) stays a direct env read because it controls which transport Celery loads, which is **not** the same concern as which Redis instance to talk to.

### Precedence at runtime

```
Celery broker URL =
  CELERY_BROKER_URL          # prod: "sqs://"
  > settings.redis_url       # dev fallback / unit tests
  > "redis://localhost:6379/0"  # baked-in default

Rate limiter storage URI =
  settings.redis_url         # always
  fallback to "memory://"    # only when settings.redis_url is empty (test override)
```

### What prod actually does

| Service | `CELERY_BROKER_URL` | `REDIS_URL` (from SSM) | Effective broker | Effective limiter |
|---|---|---|---|---|
| `luciel-backend` | unset | `rediss://luciel-redis-0001-001.../...` | n/a (no Celery on backend) | ElastiCache Redis |
| `luciel-worker` | `sqs://` | `rediss://luciel-redis-0001-001.../...` | SQS | n/a (worker doesn't rate-limit) |

The worker has `REDIS_URL` injected primarily for symmetry and for the `_infra_probes` mode-gate logic, not because it consumes Redis at runtime.

## Failure modes

### Redis unreachable
- **Backend rate limiter:** falls back to `memory://` per-container counters (degraded fidelity). Writes return 503; reads served fail-open. See `app/middleware/rate_limit.py` `create_rate_limit_middleware()`.
- **Worker:** unaffected — uses SQS.
- **Recovery:** transparent on Redis return; no manual intervention.

### SQS unreachable
- **Backend chat turns:** `MEMORY_EXTRACTION_ASYNC=true` enqueue fails. Fallback path is inline extraction (slower turn but correct).
- **Worker:** sits idle until SQS returns.
- **Recovery:** transparent on SQS return; visibility timeout ensures in-flight messages reappear.

### Both unreachable
- **Backend:** rate limits become per-container, async memory becomes inline. Degraded but functional.
- **Worker:** idle. No memory writes happen until at least SQS recovers.
- **Recovery:** order-independent. Each path recovers transparently.

## Cost / scale envelope

At current scale (testing, low single-digit concurrent users):

| Resource | Monthly cost (approx) | Headroom before next size up |
|---|---|---|
| ElastiCache `cache.t4g.micro` | ~$13 | ~5k req/sec sustained — orders of magnitude beyond current load |
| SQS (`luciel-memory-tasks` + DLQ) | <$1 (free tier covers test volume) | 1M requests/month free; per-message $0.40/M after |

We do not see a near-term reason to change either size. **Decision review trigger:** ElastiCache CPU >50% sustained, or SQS spend crosses $20/mo.

## What this is NOT

- **Not a session store.** Luciel does not use Redis for HTTP sessions; auth is API-key based and stateless on the request path.
- **Not a primary cache.** No application data is cached in Redis today. If we add a cache layer later (e.g. for retention rule lookups), it goes here too — but be careful of TTL bugs that could leak cross-tenant data. See `docs/STEP_29_AUDIT.md` Cluster 2 for prior discussion.

## Related

- `app/worker/celery_app.py` — broker selection logic, comments cite this doc
- `app/middleware/rate_limit.py` — SlowAPI configuration
- `app/verification/_infra_probes.py` — mode-gate probes used by P11/P13/P25
- `docs/runbooks/step-29y-prod-cleanup-2026-05-08.md` — cost/keep posture for both
- `docs/DRIFT_REGISTER.md` — `D-redis-url-centralize-via-settings-2026-05-08`, `D-historical-rate-limit-typo-disclosure-2026-05-07`
- `docs/DISCLOSURES.md` — DISC entries related to rate-limit history
