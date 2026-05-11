# Widget-surface E2E harness

Step 30d, Deliverable C.

## What this is

A small live-backend test harness that the GitHub Actions workflow
`.github/workflows/widget-e2e.yml` runs to prove the widget chat
surface (Step 30b) and its two Step 30d guardrails (Deliverable A
issuance preflight + Deliverable B content-safety gate) wire together
correctly under a real HTTP request, not just under AST/inline-fake
unit tests.

## Three files

| File | Purpose |
| --- | --- |
| `run_widget_e2e.sh` | Bash orchestrator. Takes a platform-admin key and a base URL; provisions tenant + domain + embed key via the real admin HTTP endpoints; invokes the assertion script twice. |
| `assert_widget_stream.py` | Python SSE assertion. POSTs to `/api/v1/chat/widget`, parses the stream of `data: {...}` frames, asserts frame contract for `--mode happy` or `--mode refusal`. |
| (this) `README.md` | Doc. |

The harness depends on one CI-only helper at the repo root:

| File | Purpose |
| --- | --- |
| `scripts/bootstrap_platform_admin_ci.py` | CI-only platform-admin key mint. Prints raw key to stdout under a guardrail env var. NOT for production -- production uses `scripts/mint_platform_admin_ssm.py`. |

## v1 trigger state (this branch)

The workflow is **dispatch-only**:

```yaml
on:
  workflow_dispatch: {}
```

No path filter, no PR trigger. Reason: the harness depends on
several runtime invariants (Postgres service health, uvicorn startup
time, sentinel-term parity between the workflow env and the bash
script) that have NOT been observed running cleanly in a GitHub
Actions runner. Path-triggering the gate on the same PR that
introduces the harness would mean discovering any such issue on the
PR you're trying to land, which is a poor-debuggability state.

The follow-up commit (next branch off `step-30d` or `main`, depending
on merge timing) ADDS the path-trigger block next to the
`workflow_dispatch` block, preserving Pattern E:

```yaml
on:
  workflow_dispatch: {}
  pull_request:
    paths:
      - app/api/v1/chat_widget.py
      - app/api/v1/admin.py
      - app/api/widget_deps.py
      - app/middleware/auth.py
      - app/policy/moderation.py
      - app/core/config.py
      - app/services/scope_prompt_preflight.py
      - ci/e2e/**
    paths-ignore:
      - docs/**
      - widget/**
```

That follow-up flip is also the natural moment to mark the Step 30d
row in `docs/CANONICAL_RECAP.md` from 🔧 to ✅, matching the Step 30b
precedent that ✅ means "observed gating cleanly at least once."

## Sentinel-term contract

The two scenarios depend on a single sentinel string being agreed on
in two places:

1. The workflow env block sets:
   ```yaml
   env:
     MODERATION_PROVIDER: "keyword"
     MODERATION_KEYWORD_BLOCK_TERMS: '["E2E_REFUSE_SENTINEL"]'
   ```

2. The shell harness re-uses the same sentinel:
   ```bash
   REFUSAL_SENTINEL="E2E_REFUSE_SENTINEL"
   ```

If those two values drift apart, the refusal-mode assertion will
fail. That is the intended misconfig signal: the harness is
contractually pinning the wiring.

## Provisioning chain at a glance

```
+-------------------------+   +-------------------------+
| 1. POST /admin/tenants  |   |  system_prompt_         |
|    (tenant_id)          |---|  additions = "...".     |
+-------------------------+   +-------------------------+
              |
              v
+-------------------------+   +---------------------------+
| 2. POST /admin/domains  |   |  system_prompt_additions  |
|    (tenant_id,          |---|  is what the Deliverable  |
|     domain_id)          |   |  A preflight checks.      |
+-------------------------+   +---------------------------+
              |
              v
+--------------------------+
| 3. POST /admin/embed-keys|
|    (returns raw_key)     |
+--------------------------+
              |
              v
+-------------------------+   +-------------------------+
| 4. POST /api/v1/chat/   |   | benign message;         |
|    widget               |---| assert SSE happy path.  |
+-------------------------+   +-------------------------+
              |
              v
+-------------------------+   +--------------------------+
| 5. POST /api/v1/chat/   |   | message contains the     |
|    widget               |---| sentinel; assert SSE     |
+-------------------------+   | refusal path.            |
                              +--------------------------+
```

## Running locally

Pre-reqs: Postgres + Redis running on `localhost:5432` / `localhost:6379`
with the credentials matching `DATABASE_URL`/`REDIS_URL` below, and a
clean `luciel_e2e` database.

```bash
# 1. environment
export DATABASE_URL="postgresql+psycopg2://luciel:luciel@localhost:5432/luciel_e2e"
export REDIS_URL="redis://localhost:6379/0"
export MODERATION_PROVIDER="keyword"
export MODERATION_KEYWORD_BLOCK_TERMS='["E2E_REFUSE_SENTINEL"]'

# 2. migrate
alembic upgrade head

# 3. bootstrap an admin key (guardrail env var required)
LUCIEL_CI_ALLOW_RAW_KEY_STDOUT=yes \
    python -m scripts.bootstrap_platform_admin_ci > /tmp/admin_key.txt
export ADMIN_KEY="$(cat /tmp/admin_key.txt)"

# 4. boot the app (separate terminal)
uvicorn app.main:app --host 127.0.0.1 --port 8000

# 5. run the harness
export BASE_URL="http://127.0.0.1:8000"
export TEST_ORIGIN="https://e2e.luciel.test"
bash ci/e2e/run_widget_e2e.sh
```

Expected final line on success:

```
==> widget-e2e: all assertions passed
```

## What this harness is NOT

* Not a substitute for the pillar verification suite (which runs in
  `luciel-verify` ECS against production-or-staging). The pillar
  suite is the verify gate of record for cross-pillar invariants.
  This harness is the widget-surface-only smoke gate.
* Not a load test.
* Not a substitute for the widget bundle build/size gate in
  `.github/workflows/ci.yml`.
* Not authoritative for the production moderation provider behaviour.
  The hermetic `keyword` provider is the same `ModerationProvider`
  interface as the production `openai+failclosed` wiring, but its
  block decisions are intentionally trivial. Real-provider behaviour
  is exercised by the unit tests in `tests/api/test_content_safety_gate.py`.
