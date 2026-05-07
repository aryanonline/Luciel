# Step 29 — `app/verification/` Read-Only Audit

**Date:** 2026-05-06
**Branch:** `step-28-hardening-impl` at `bf5fb36`
**Scope:** READ-ONLY mapping of the existing pillar suite as a precondition for the pytest harness migration. **No code changes are made by this commit.** The output of this doc is the design contract that the next commit (pytest deps + thin `tests/integration/test_pillars.py` + verify marker + `.github/workflows/verify.yml`) must satisfy 1:1.

**Authority:** User said "I will leave the judgment onto you based on what is the best and most honest approach for our business." Honest approach is: audit before authoring, so the harness replicates the FINAL STEP 26 MATRIX exactly and we don't lose green coverage in the migration.

---

## 1. Module layout

```
app/verification/
  __init__.py        (1.7 KB)  -- subpackage docstring + __version__ = "0.26.0"
  __main__.py        (11 KB)   -- CLI entry point, pillar registration, teardown orchestration
  http_client.py     (3 KB)    -- BASE_URL, REQUEST_TIMEOUT, h(), pooled_client(), call()
  fixtures.py        (8 KB)    -- RunState, sentinels, sample docs, sweep_residue_tenants()
  runner.py          (5 KB)    -- Pillar ABC, PillarResult, MatrixReport, SuiteRunner
  tests/
    __init__.py      (empty)
    pillar_01_onboarding.py                          (74 lines)
    pillar_02_scope_hierarchy.py                     (large)
    pillar_03_ingestion.py                           (large)
    pillar_04_chat_key_binding.py                    (large)
    pillar_05_chat_resolution.py                     (large)
    pillar_06_retention.py                           (large)
    pillar_07_cascade.py                             (large)
    pillar_08_scope_negatives.py                     (large)
    pillar_09_migration_integrity.py                 (172 lines)
    pillar_10_teardown_integrity.py                  (87 lines)
    pillar_11_async_memory.py                        (large; SessionLocal x1)
    pillar_12_identity_stability.py                  (large; SessionLocal x3)
    pillar_13_cross_tenant_identity.py               (large; SessionLocal x7)
    pillar_14_departure_semantics.py                 (large; SessionLocal x1)
    pillar_15_consent_route_no_double_prefix.py
    pillar_16_memory_items_actor_user_id_not_null.py
    pillar_17_api_key_deactivate_audit.py
    pillar_18_tenant_cascade.py
    pillar_19_audit_log_api_mount.py
    pillar_20_onboarding_audit.py
    pillar_21_cross_tenant_scope_leak.py             (largest, 582+ lines)
    pillar_22_db_grants_audit_log_append_only.py
    pillar_23_audit_log_hash_chain.py                (307 lines)
```

Total: 23 pillar modules + 5 framework modules.

---

## 2. Pillar contract (the surface pytest must wrap)

Every pillar exports a single module-level instance:

```python
# at end of every pillar_*.py
PILLAR = SomePillar()
```

Where `SomePillar(Pillar)` declares:

- `number: int` — 1-indexed for stable matrix ordering
- `name: str` — short human label (rendered in the matrix banner)
- `def run(self, state: RunState) -> str` — returns a single-line success-detail string, OR raises an exception on failure

`runner.SuiteRunner.run(state)` is the orchestrator:
- Iterates pillars in registration order (NO parallelism, NO dependency resolution — order is the explicit contract).
- Wraps each `pillar.run(state)` in try/except.
- On success: appends `PillarResult(passed=True, detail=<returned string>, elapsed_s=...)`.
- On failure: appends `PillarResult(passed=False, detail="<ExcType>: <msg>", traceback_text=<8-frame tb>)` and continues unless `stop_on_fail=True` (the suite uses default = continue, "run-all-then-report" invariant from `__init__.py`).

`MatrixReport.exit_code()` returns 0 iff all pillars passed.

**Pytest harness implication:** Each pillar maps to one `def test_pillarNN_<name>(state)` function. State is a session-scoped fixture. The pytest report ordering by node-id naturally preserves pillar order. `pytest --tb=short` matches the runner's 8-frame truncation reasonably.

---

## 3. RunState — the shared mutable contract

Defined in `fixtures.py`. Single object passed to every pillar's `run()`. Fields:

| Field | Set by | Read by |
| --- | --- | --- |
| `tenant_id` | factory (uuid8) on construction | every pillar |
| `platform_admin_key` | factory (env `LUCIEL_PLATFORM_ADMIN_KEY`) | most pillars |
| `tenant_admin_key` | P1 | P2, P4, P6, P14, P17, P20 |
| `domain_id` | P2 | P3, P4, P5, P7, P8 |
| `agent_id` | P2 | P3, P4, P5, P7, P8, P12, P13, P14 |
| `instance_tenant`, `instance_domain`, `instance_agent` | P2 | P3, P4, P5, P6, P7, P8 |
| `agent_admin_key`, `agent_admin_key_id` | P2 (gap-5 pre-cascade fix) | P8 (above-scope negative test) |
| `source_id_pdf`, `source_id_md`, `source_id_csv` | P3 | P5, P6 |
| `chat_keys: list[dict]` | P4 | P5, P8 |
| `keys_to_deactivate: list[int]` | P1, P2, P4 | `_thorough_teardown` |

**Inter-pillar dependency graph (read direction):**
```
P1 -> P2 -> P3 -> P5
                P4 -> P5
                       P6 -> P7
                       P8 (above-scope negative)
                       P11..P23 (assorted, mostly platform_admin only)
```

P9 and P10 are **post-state** pillars: P9 is read-only forensics (subprocess raw-SQL), P10 runs after `_thorough_teardown`.

**Pytest harness implication:** RunState MUST be a single session-scoped fixture, NOT `function` scoped. Re-creating state per test would re-mint a new tenant per pillar, breaking the entire dependency graph and exploding the verify minute count from ~2-3 minutes to >20 minutes. The pytest collection order MUST also match the registration order in `__main__.py` (alphabetical by `pillar_NN_*.py` matches registration order today — keep that invariant).

---

## 4. Pillar inventory (23 pillars)

Mapping below shows: number → file → class → reads from RunState → writes to RunState → notes.

| # | File | Class | Reads RunState | Writes RunState | Network shape | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `pillar_01_onboarding.py` | `OnboardingPillar` | `tenant_id`, `platform_admin_key` | `tenant_admin_key`, `keys_to_deactivate` | Pure HTTP (POST `/admin/tenants/onboard`) | Option B invariant |
| 2 | `pillar_02_scope_hierarchy.py` | `ScopeHierarchyPillar` | `tenant_admin_key`, `tenant_id` | `domain_id`, `agent_id`, `instance_*`, `agent_admin_key*`, `keys_to_deactivate` | Pure HTTP | Mints agent-admin key BEFORE cascade (gap-5 fix) |
| 3 | `pillar_03_ingestion.py` | `IngestionPillar` | scope ids, `tenant_admin_key` | `source_id_pdf/md/csv` | Pure HTTP (multipart + json) | Both `/knowledge` and `/knowledge/text` (gap-2) |
| 4 | `pillar_04_chat_key_binding.py` | `ChatKeyBindingPillar` | scope ids, `tenant_admin_key` | `chat_keys`, `keys_to_deactivate` | Pure HTTP | Mints one chat key per scope level |
| 5 | `pillar_05_chat_resolution.py` | `ChatResolutionPillar` | `chat_keys`, source ids | (none) | Pure HTTP | Round-trips embedded sentinels through real chat (gap-1, gap-6) |
| 6 | `pillar_06_retention.py` | `RetentionPillar` | source ids, `tenant_admin_key` | (none) | Pure HTTP | Two purges with before/after assertions (gap-3) |
| 7 | `pillar_07_cascade.py` | `CascadePillar` | scope ids, `tenant_admin_key` | (none) | Pure HTTP | Re-fetches agent-Luciel post-cascade (gap-4) |
| 8 | `pillar_08_scope_negatives.py` | `ScopeNegativesPillar` | `agent_admin_key`, scope ids | (none) | Pure HTTP | Above-scope negative test (gap-5 — unconditional thanks to P2's pre-cascade key mint) |
| 9 | `pillar_09_migration_integrity.py` | `MigrationIntegrityPillar` | (none — uses env DATABASE_URL) | (none) | **Subprocess + raw SQL via SQLAlchemy.inspect** | Bidirectional table diff + per-column compare. Read-only DDL probe. Can be skipped via `--skip-migration`. |
| 10 | `pillar_10_teardown_integrity.py` | `TeardownIntegrityPillar` | `tenant_id`, `platform_admin_key` | (none) | Pure HTTP (Step 26b.2 rewrite — was subprocess) | Must run AFTER `_thorough_teardown`. Asserts zero residue. Skipped under `--keep`. |
| 11 | `pillar_11_async_memory.py` | `AsyncMemoryPillar` | scope ids, `tenant_admin_key` | (none) | HTTP + 1 SessionLocal() (read-only forensics on `memory_items`) | |
| 12 | `pillar_12_identity_stability.py` | `IdentityStabilityPillar` | agent_id, tenant ids | (none) | HTTP + 3 SessionLocal() (read-only forensics on `messages`/`api_keys`) | All WRITES migrated to HTTP in C13 (`eaa80b5`); SELECTs remain |
| 13 | `pillar_13_cross_tenant_identity.py` | `CrossTenantIdentityPillar` | platform_admin, agent_id | (none) | HTTP + 7 SessionLocal() (read-only forensics, multi-tenant assertions) | Largest forensics surface. Most candidates for D-verify-task-pure-http. |
| 14 | `pillar_14_departure_semantics.py` | `DepartureSemanticsPillar` | platform_admin, scope ids | (none) | HTTP + 1 SessionLocal() (read-only forensics) | Contains the `params=` workaround comment (line 347) for D-call-helper-missing-params-kwarg |
| 15 | `pillar_15_consent_route_no_double_prefix.py` | `ConsentRouteNoDoublePrefixPillar` | platform_admin | (none) | Pure HTTP | Asserts route is mounted exactly once |
| 16 | `pillar_16_memory_items_actor_user_id_not_null.py` | `MemoryItemsActorUserIdNotNullPillar` | platform_admin | (none) | Pure HTTP (admin readback endpoint) | Schema invariant |
| 17 | `pillar_17_api_key_deactivate_audit.py` | `ApiKeyDeactivateAuditPillar` | platform_admin, tenant_admin | (none) | Pure HTTP | Mints+deactivates a key, asserts audit row |
| 18 | `pillar_18_tenant_cascade.py` | `TenantCascadePillar` | platform_admin | (none) | Pure HTTP | End-to-end tenant deactivation cascade |
| 19 | `pillar_19_audit_log_api_mount.py` | `AuditLogApiMountPillar` | platform_admin | (none) | Pure HTTP | Asserts `/admin/audit-logs` is mounted (was 404 pre-`dddf8cb`) |
| 20 | `pillar_20_onboarding_audit.py` | `OnboardingAuditPillar` | platform_admin | (none) | Pure HTTP | Asserts every onboarding produces a SCOPE_ASSIGNMENT_PROMOTED audit row |
| 21 | `pillar_21_cross_tenant_scope_leak.py` | `CrossTenantScopeLeakPillar` | platform_admin | (none) | Pure HTTP | 582+ lines — the most exhaustive negative-test pillar |
| 22 | `pillar_22_db_grants_audit_log_append_only.py` | `DbGrantsAuditLogAppendOnlyPillar` | platform_admin | (none) | Pure HTTP (server-side DB-grant probe endpoint) | Worker-DSN can INSERT but NOT UPDATE/DELETE on admin_audit_logs |
| 23 | `pillar_23_audit_log_hash_chain.py` | `AuditLogHashChainPillar` | platform_admin | (none) | Pure HTTP (server-side hash-chain probe endpoint) | Tamper-evidence beyond DB-grant boundary |

**Registration order in `__main__.py`:**
- `PRE_TEARDOWN_PILLARS = [P1..P8, P11..P23]` (skips P9, P10)
- P9 registered conditionally (`--skip-migration`)
- P10 registered into a SECOND runner that fires AFTER `_thorough_teardown`
- Final matrix is `_merge_reports(pre, post, state)`

---

## 5. Network and DB-session footprint

**HTTP-only pillars (16):** P1, P2, P3, P4, P5, P6, P7, P8, P10, P15, P16, P17, P18, P19, P20, P21, P22, P23 — actually 18 of the 23.

Wait — let me re-tally for honesty:
- HTTP-only (clean): P1, P2, P3, P4, P5, P6, P7, P8, P10, P15, P16, P17, P18, P19, P20, P21, P22, P23 → **18 pillars**
- HTTP + read-only `SessionLocal()` forensics: P11, P12, P13, P14 → **4 pillars** (12 total session sites)
- Subprocess + raw SQL (read-only, DDL-introspection): P9 → **1 pillar**

**Total: 18 + 4 + 1 = 23.** ✓

**Implication for harness:**
- The pytest harness must run with `DATABASE_URL` exported (P9, P11-P14 need it) AND `LUCIEL_PLATFORM_ADMIN_KEY` exported (every pillar).
- The verify Docker image / CI runner must have `psycopg2`/SQLAlchemy installed (already required by app deps).
- P9's subprocess shell-out remains as-is — it's an isolation invariant (clean SQLAlchemy metadata), not debt.

---

## 6. Deferred drifts surfaced by this audit

These are the two drifts the Step 28 close-out flagged for Step 29. The audit confirms both are bounded and ready to resolve.

### D-verify-task-pure-http-2026-05-05

**Scope:** Eliminate all 12 `SessionLocal()` sites in P11/P12/P13/P14 by routing reads through new admin GET endpoints.

**Surface enumerated (from `grep -n SessionLocal`):**

| File | Sites | Purpose |
| --- | --- | --- |
| `pillar_11_async_memory.py` | 1 (line 191) | Read `memory_items` for the throwaway tenant |
| `pillar_12_identity_stability.py` | 3 (lines 200, 286, 347) | Read `messages` / `api_keys` for identity-stability assertions |
| `pillar_13_cross_tenant_identity.py` | 7 (lines 314, 447, 465, 524, 582, 600, 642) | Multi-tenant SELECTs on `agents`, `users`, `messages`, `api_keys`, `scope_assignments` |
| `pillar_14_departure_semantics.py` | 1 (line 374) | Departure semantics forensics |

**Honest scope assessment:** This is 12 SELECT statements, NOT 12 endpoints (some likely consolidate). Estimated 4-7 new admin GET routes. Each must be `platform_admin`-gated.

**Resolution path (NOT implemented in this commit, only documented):**
1. Enumerate every SELECT (column list, WHERE shape).
2. Design admin GET routes returning the projection each pillar needs.
3. Migrate one file at a time (P11 first — smallest), re-verify suite green after each.
4. Drop `from app.db.session import SessionLocal` from each pillar; do NOT delete the import from the verify image until all four are migrated, to avoid mid-migration breakage.

### D-call-helper-missing-params-kwarg-2026-05-05

**Surface:** `app/verification/http_client.py:call()` does not accept `params=`. P14 line 347 has the workaround comment. Only one site currently uses inlined query strings.

**Resolution path (NOT implemented in this commit, only documented):**
1. Extend `call()` signature: `def call(method, path, key, *, json=None, files=None, data=None, params=None, expect=200, client=None) -> httpx.Response`
2. Forward `params=params` to `httpx.Client.request(...)`.
3. Migrate P14's inlined `?audit_label=...` back to `params={"audit_label": ...}`.
4. Add a one-line unit test asserting `call(...., params={"k": "v"})` builds the right query string.

**Either drift can be the FIRST code commit after the harness lands**, or they can be folded into the same commit. The pytest harness commit should land FIRST so we have CI-gated verification of any subsequent change.

---

## 7. Pytest harness target shape (design contract; NOT yet authored)

Below is the contract the next commit must satisfy. Documented here so the design is reviewable BEFORE any code lands.

### 7.1 Dependencies (additions to `requirements.txt`)
- `pytest>=8.0`
- `pytest-asyncio>=0.23` (defensive — none of today's pillars are async, but harness modules may grow async fixtures)

### 7.2 New file: `tests/integration/test_pillars.py` (thin)

```python
"""Step 29 — pytest wrapper around app.verification pillars.

Each pillar from app.verification.tests.pillar_NN_* is exposed as one
pytest test function. RunState is a session-scoped fixture so the
inter-pillar dependency graph is preserved.

Run locally:
    LUCIEL_PLATFORM_ADMIN_KEY=... \\
    DATABASE_URL=... \\
    pytest -m verify tests/integration/test_pillars.py -v

Run in CI: see .github/workflows/verify.yml
"""
import pytest

from app.verification.fixtures import RunState
from app.verification.tests.pillar_01_onboarding import PILLAR as P1
# ... (P2..P23)


@pytest.fixture(scope="session")
def state():
    return RunState()


@pytest.mark.verify
@pytest.mark.order(1)
def test_pillar_01_onboarding(state):
    detail = P1.run(state)
    assert detail
```

(... 23 test functions ...)

**P9, P10 ordering invariants:**
- P10 is marked with `@pytest.mark.order(10)` AND requires a teardown step. Two options:
  - (a) wrap `_thorough_teardown` in a session-scoped autouse fixture finalizer that runs before P10
  - (b) test_pillar_10 calls `_thorough_teardown(state)` explicitly before `P10.run(state)`
- (a) is cleaner; (b) is more explicit. Decision deferred to the implementation commit.

### 7.3 Pytest marker registration (`pytest.ini` or `pyproject.toml`)
```ini
[pytest]
markers =
    verify: integration tests against a running backend (requires LUCIEL_PLATFORM_ADMIN_KEY + DATABASE_URL)
```

### 7.4 New file: `.github/workflows/verify.yml`

CI gate. Open question for the implementation commit: does CI run against ECS prod (`api.vantagemind.ai`), against a local docker-compose Postgres + uvicorn, or both? Honest recommendation: **start with docker-compose** (deterministic, no prod credential surface in CI), and treat prod-target verification as the FINAL STEP 26 MATRIX runner invocation that we already have, kept around as the manual gate. CI gate runs on every push to `step-28-hardening-impl` and any future feature branch.

---

## 8. Risks / open questions to resolve at implementation time

1. **P9 + P10 ordering in pytest.** Pytest does not natively guarantee test order. Use `pytest-ordering` OR `@pytest.mark.order(N)` from `pytest-order` OR rely on alphabetical filename ordering. Honest recommendation: name test functions `test_01_onboarding`, `test_02_scope_hierarchy`, etc., and rely on collection order. Still need to confirm pytest does not parallelize within a file by default (it does not unless `pytest-xdist` is installed).
2. **Teardown timing for P10.** If using session-scoped fixture finalizer, P10 cannot be a regular test (finalizer runs AFTER all tests). Cleanest solution: P10 lives in a SEPARATE test file `test_pillars_post_teardown.py` that pytest collects after `test_pillars.py`. Defer to implementation commit.
3. **CI target.** Local docker-compose is the honest CI choice. ECS-prod gate stays manual.
4. **`--skip-migration` / `--keep` flag equivalents.** In pytest these become `pytest -m "verify and not migration"` (using a sub-marker) or env var toggles. Defer to implementation commit; not required for first green CI run.
5. **`pytest-asyncio` necessity.** Today's pillars are sync. Including the dep is defensive; can drop if implementation reveals it's unused.

---

## 9. Acceptance criteria for the implementation commit

The next commit (which DOES change code) must:

1. ✅ All 23 pillars discoverable as pytest tests.
2. ✅ `pytest -m verify` against a running backend produces 23/23 green matching today's `python -m app.verification`.
3. ✅ FINAL STEP 26 MATRIX runner (`__main__.py`) remains intact and continues to work — the pytest harness is **additive**, not a replacement, until CI proves stable for ≥1 week.
4. ✅ `.github/workflows/verify.yml` runs on push to `step-28-hardening-impl` and produces the same 23/23 green result.
5. ✅ Closes drifts `D-verify-task-pure-http-2026-05-05` AND `D-call-helper-missing-params-kwarg-2026-05-05` within Step 29 — NOT deferred to a follow-up step. Per the 2026-05-06 ordering revision (see §10 below), both drift closures land BEFORE the pytest harness so the harness wraps the honest end-state, not the transitional one.

---

## 10. Audit closure (REVISED 2026-05-06 ~19:55 EDT — no-deferral order)

This audit is **read-only**. No source files in `app/` are modified. The next commits are the implementation commits.

**REVISED commit shape for the implementation (replaces the v1 ordering):**

The initial draft of this section recommended landing the pytest harness first and folding the two drifts into a follow-up commit "for smaller blast radius." On re-read against the user's standing principle — "we are designing so let us not defer errors, it could come back to bite us" / "honest long term fixes and not just taking shortcuts" / "I dont want to defer anything we need to be a little due dilligent with our business" — that recommendation was the avoiding-problems pattern. The drifts have already been deferred once (logged 2026-05-05, parked for Step 29). Step 29 IS the explicit window to close them. Landing the harness first and pushing the drifts forward a third time would be a textbook lazy-defer dressed up as engineering hygiene.

Revised order (all three commits land within Step 29):

- **Commit A (this audit):** docs-only, no tag. SHIPPED at `4212072`.
- **Commit B — `D-call-helper-missing-params-kwarg-2026-05-05` closure:** smallest of the three, lowest risk. Extend `app/verification/http_client.py:call()` to accept `params=` and forward to `httpx`. Migrate P14 line 347's inlined `?audit_label=...` back to `params={"audit_label": ...}`. Add a unit test for `call(..., params={"k": "v"})`. Verify FINAL STEP 26 MATRIX 23/23 green before moving on. Verify-after-every-commit doctrine RE-ENGAGES here. No tag. **SHIPPED at `17cd12b`.**
- **Commit B.1 — `D-pillar-13-mode-gate-broker-only-2026-05-06` closure (NEW, inserted 2026-05-06 ~20:30 EDT):** discovered when B's local verify gate ran 22/23 with P13 silently FAIL on Assertion A2 in the absence of a local Celery worker. Diagnosis: P13's mode-detection line `mode_full = _broker_reachable()` checks Redis ping only (proves enqueue capability, not consumer presence) while P11's mode-detection correctly uses `_broker_reachable() and _worker_reachable()`; without a worker subscribed to the queue, P13 enqueues the spoof payload, sleeps 60s, then asserts on an audit row no worker had any chance to write. The Gate 6 worker code at `app/worker/tasks/memory_extraction.py` is innocent. Fix: mirror P11's `_worker_reachable()` helper inline in P13 (verbatim copy, no shared module yet) and update the mode-detect line to require both probes True. Inline duplication is intentional and bounded — both copies will move into a shared `app/verification/_infra_probes.py` module in Commit D when the pytest harness lift already needs to touch the verification infra layer. Doing the consolidation in B.1 would expand the blast radius to P11 unnecessarily. Verify gate after B.1: P13 declares MODE=degraded under no-worker conditions and the suite returns 23/23 green honestly. Full A1/A2 spoof-guard verification is owned by the prod gate that runs against the deployed Celery worker. No tag. Forward-looking guard recorded in CANONICAL_RECAP §15: any future verification pillar declaring a `MODE=full` branch dependent on async infrastructure must verify the FULL chain (broker AND consumer), never the nearest-hop reachability alone.
- **Commit C — `D-verify-task-pure-http-2026-05-05` closure:** larger surface, 12 `SessionLocal()` SELECT sites across P11/P12/P13/P14 (enumerated in §6 above). Estimated 4-7 new platform-admin-gated GET endpoints. Migrate ONE pillar at a time, verify 23/23 green after each migration, drop `from app.db.session import SessionLocal` only when the pillar is fully HTTP. Sub-commit shape proposed to operator BEFORE authoring any of it. No tag until the suite is fully pure-HTTP.
- **Commit D — pytest harness:** lands AFTER C, on the pure-HTTP suite, so the harness wraps the honest end-state. Thin `tests/integration/test_pillars.py`, pytest+pytest-asyncio in deps, `verify` marker, `.github/workflows/verify.yml`. Tag candidate: `step-29-complete`.

Why this order matters: doing the harness first then refactoring under it would mean rewriting 12 pillar test bodies twice (once into pytest collection, once for the HTTP migration). Doing the HTTP migration first means the harness wraps a clean surface and Commit D becomes a thin wrapper rather than a deep rewrite.

**Verify-after-every-commit doctrine** is fully enforced from Commit B onward, including after each per-pillar sub-step inside Commit C. The B.1 insertion (2026-05-06) is itself an instance of this doctrine working as intended — the discovery of the P13 mode-gate honesty defect happened precisely because verify was re-engaged immediately after B; under the v1 "land harness, defer drifts" ordering it would have been masked indefinitely behind the harness's own test-collection layer.

End of audit.
