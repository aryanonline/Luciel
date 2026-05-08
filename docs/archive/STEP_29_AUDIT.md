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

**Scope:** Decouple the verify task from the application's in-process Python interfaces (`SessionLocal` for direct DB access, `MemoryService`/`MemoryRepository` for ORM mutations, `extract_memory_from_turn.delay()`/`apply_async()` for direct Celery enqueue) by routing as much of the surface as is honest through HTTP. The original framing of "12 SELECT sites" was an undercount and is corrected below; see also `D-step29-audit-undercounts-verify-debt-2026-05-06` (opened-and-closed in Commit B.3, the same commit that authored this re-audit).

**REVISED surface enumeration (B.3, 2026-05-06 ~20:55 EDT, against tree at `8a06652`):**

The 2026-05-05 v1 enumeration counted only `SessionLocal()` block opens (8 in total — itself an undercount of 12 because it missed several blocks). It did not separate read-only forensic SELECTs (which can move to admin GET endpoints, the easy case) from in-process **producer-side calls** that mutate state through the application's Python API rather than the HTTP layer (which require either admin POST endpoints or a deliberate scope decision to leave them as in-process producers because they ARE what the pillar is testing). The honest enumeration is below.

#### `pillar_11_async_memory.py` (508 lines)

DB-session blocks: 1 (line 191), spans through line 503.

Forensic READS (5 distinct `db.scalars` / `db.scalar` / `db.get` calls):
- L197 `select(ApiKey).where(id=...)` — fetch the agent chat key's `key_prefix`
- L238 `select(MemoryItem.message_id).where(tenant=..., message_id IS NOT NULL)` — idempotency probe target
- L274 `select(AdminAuditLog).where(action='worker_cross_tenant_reject', tenant=...)` — F3 rejection-row poll
- L312 `select(AdminAuditLog).where(action='worker_malformed_payload', tenant=...)` — F4 rejection-row poll
- L399 `select(func.count()).where(action IN [...], actor_label LIKE 'worker:%')` — F9 forbidden-action audit count
- L419 / L453 `db.get(LucielInstance, id)` — fetch instance for F10 active-flag toggle
- L476 `select(AdminAuditLog).where(action='worker_instance_deactivated', tenant=...)` — F10 rejection-row poll

In-process WRITES (3 distinct paths, ALL producer-side / state-mutating):
- L207 `MemoryService(...).enqueue_extraction(...)` — F1 *direct call into the application's service layer*; measures the enqueue path's latency. Cannot be replaced by an HTTP call without losing what F1 is asserting (the in-process latency budget).
- L246 `MemoryRepository(db).upsert_by_message_id(...)` — F2 *direct repository call*; tests the idempotent no-op return value, which is a Python-API contract not exposed over HTTP.
- L300 / L462 `extract_memory_from_turn.apply_async(kwargs={...})` — F4 / F10 *direct Celery enqueue*; tests Gate-1 (malformed payload) and Gate-4 (instance deactivated) on the **worker side**, requiring the harness to act as a Celery producer to inject specific malformed/edge-case payloads that the legitimate API path can never construct.
- L425-426 / L459-460 / L499-500 `inst.active = ...; db.commit()` — F10 *direct ORM write* against `luciel_instances.active`. Currently works because the verify role has SELECT+UPDATE on `luciel_instances` for cascade-deactivate flows; could move to a small admin POST `/admin/forensics/luciel_instances/{id}/active_p2c12_step29c` route.

#### `pillar_12_identity_stability.py` (461 lines)

DB-session blocks: 3 (lines 200, 286, 347).

Forensic READS (5 distinct calls):
- L202 `select(...) FROM messages WHERE tenant=..., user_id=...` — first chat-turn message lookup
- L288 `db.get(ApiKey, k1_id)` — post-rotation key state check
- L349 `select(...) FROM messages WHERE tenant=..., session=...` — second-turn message lookup after rotation
- L376 `select(...) FROM messages WHERE tenant=..., user_id=...` ORDER BY id — user-scoped message ordering assertion
- L395 `select(...) FROM agents WHERE tenant=..., user_id=...` — agent-binding stability check

In-process WRITES: 0. P12's mutations are all already routed through admin HTTP routes (Commit 12 of Phase 2 closed that surface).

#### `pillar_13_cross_tenant_identity.py` (765 lines)

DB-session blocks: 7 (lines 346, 479, 497, 556, 614, 632, 674).

Forensic READS (10 distinct calls):
- L348 `select(Message)` for T1's seed message id
- L481 `select(MemoryItem)` for spoof-rows leak check (T2 scope)
- L499 / L513 `select(AdminAuditLog).where(action='worker_identity_spoof_reject', ...)` — A2 spoof-reject audit poll (the assertion B.1 fixed the mode-gate around)
- L558 `select(MemoryItem)` for legitimate row presence in T1
- L616 `select(MemoryItem)` for T2 leak rows after spoof
- L634 `db.get(ApiKey, k1_id)` — post-spoof key state
- L676 `select(MemoryItem)` for legit row second-pass check
- L690 `select(MemoryItem)` for T2 leak second-pass check
- L697 `db.get(ApiKey, k1_id)` — post-spoof key state second-pass

In-process WRITES (1 path, producer-side):
- L448 `extract_memory_from_turn.delay(...)` — the spoof payload enqueue. SAME exemption rationale as P11 F4/F10: the harness MUST act as a Celery producer here because the spoof payload (mismatched `tenant_id` vs `agent_id` slug) is by construction one the legitimate HTTP path will never produce. This is what Gate 6 is FOR.

#### `pillar_14_departure_semantics.py` (570 lines)

DB-session blocks: 1 (line 378).

Forensic READS (4 distinct calls):
- L381 `db.get(ApiKey, k1_id)` — post-departure key state for user 1
- L397 `db.get(ApiKey, k2_id)` — post-departure key state for user 2
- L474 `select(MemoryItem)` for T1 memory rows after departure
- L490 `db.get(User, user_id)` — user row state after departure
- L510 `select(MemoryItem)` for T2 memory rows after departure (cross-tenant non-leak)

In-process WRITES: 0.

#### Honest totals

| Pillar | `SessionLocal` blocks | Forensic reads | In-process service calls | Direct Celery enqueues | Direct ORM writes |
| --- | ---:| ---:| ---:| ---:| ---:|
| P11 | 1 | 7 | 2 (`MemoryService.enqueue_extraction`, `MemoryRepository.upsert_by_message_id`) | 2 (F4 malformed, F10 deactivated) | 1 (`luciel_instances.active` toggle, restored after) |
| P12 | 3 | 5 | 0 | 0 | 0 |
| P13 | 7 | 10 | 0 | 1 (spoof-payload enqueue for Gate-6 test) | 0 |
| P14 | 1 | 5 | 0 | 0 | 0 |
| **total** | **12** | **27** | **2** | **3** | **1** |

The v1 audit's "12 SELECT statements" was a count of `SessionLocal` block opens; the actual read count inside those blocks is **27 distinct forensic queries**. The v1 audit's "4-7 new admin GET routes" estimate was based on the undercount; corrected estimate below.

#### Architectural decision: producer-side calls are SCOPED OUT of pure-HTTP

The in-process service calls (P11 F1 latency probe via `MemoryService.enqueue_extraction`, P11 F2 idempotency probe via `MemoryRepository.upsert_by_message_id`) and the direct Celery enqueues (P11 F4 malformed payload, P11 F10 deactivated-instance, P13 A1/A2 spoof payload) are by deliberate design what those pillars are TESTING. Replacing them with HTTP-routed equivalents would either (a) defeat the assertion (F1's latency budget IS the in-process enqueue path, not a network round-trip) or (b) require constructing malformed/spoof payloads that the legitimate API path cannot produce (F4, F10, A1, A2). These five callsites are therefore exempted from `D-verify-task-pure-http-2026-05-05`'s scope and that exemption is now an architectural rule:

> **Producer-side exemption:** A verification pillar is permitted to act as a direct Celery producer (`task.delay()` / `task.apply_async()`) and as a direct service-layer caller (`Service(...).method()` / `Repository(...).method()`) WHEN AND ONLY WHEN the assertion under test is a property of the producer-side path itself (latency, idempotency, or the worker's response to a payload shape that the HTTP API contract does not permit). This exemption MUST be declared inline in the pillar's docstring with a one-line justification; otherwise the call must be routed through HTTP.

This exemption preserves the security boundary `D-verify-task-pure-http` was actually written to enforce: the verify task does not hold privileges (DB role grants, secrets) that the production worker doesn't already have, because every producer-side call uses the SAME `app.worker.celery_app` and the SAME `app.memory.service.MemoryService` the production code uses. The privilege the verify task does NOT need (and per Commit 4 of Phase 2 does NOT have) is direct INSERT/UPDATE on `users`, `scope_assignments`, etc. — those are gated behind admin HTTP routes per Commit 12.

The one direct ORM write that remains under this exemption (P11 F10 toggling `luciel_instances.active`) is borderline: it is not testing the mutation itself, only using it as setup/teardown for the Gate-4 assertion. Honest call: this gets a small admin POST `/admin/forensics/luciel_instances/{id}/active_step29c` route in Commit C, and P11 F10 is migrated to call it. Documented as Commit C.5 below.

#### REVISED Commit C sub-shape (replaces v1's 4-sub-commit shape)

With reads, exempt-producer-calls, and the one borderline ORM write disambiguated, the honest sub-commit shape is:

| Sub-commit | Scope | New endpoints (suffix `_step29c`) |
| --- | --- | --- |
| **C.1** | P11 — 7 forensic reads → admin GETs. NO change to F1/F2/F4/F10 producer-side calls (declare exemption in docstring). | 4 GETs: `/admin/forensics/api_keys` (id-or-prefix lookup), `/admin/forensics/memory_items` (by tenant + filters), `/admin/forensics/admin_audit_logs` (by tenant + action), `/admin/forensics/luciel_instances/{id}` (state read). |
| **C.2** | P12 — 5 forensic reads → admin GETs (some reuse from C.1). | 1 new GET: `/admin/forensics/messages` (by tenant + user/session/order). `api_keys` and `agents` reuse C.1 + a new `/admin/forensics/agents` route. So 1–2 new endpoints. |
| **C.3** | P13 — 10 forensic reads → admin GETs (heavy reuse from C.1+C.2). NO change to A1/A2 producer-side spoof enqueue (declare exemption). | 0–1 new endpoint: `/admin/forensics/admin_audit_logs` filter extension to include `action_in=[...]` for the multi-action spoof poll, if not already covered by C.1's design. |
| **C.4** | P14 — 5 forensic reads → admin GETs (likely full reuse). | 1 new GET: `/admin/forensics/users/{id}` (state read). |
| **C.5** | P11 F10 — the one borderline ORM write (`luciel_instances.active` toggle). Admin POST route + harness migration; F10 docstring updated to reflect HTTP-routed setup. | 1 new POST: `/admin/forensics/luciel_instances/{id}/active`. |
| **C.6** | Cross-pillar cleanup — drop `from app.db.session import SessionLocal` from all four pillar files; consolidate inline `_worker_reachable()` (P11 + P13) into new `app/verification/_infra_probes.py`; harness module `app/verification/http_client.py` gains a small `forensics_get(path, **filters)` convenience wrapper to keep callsites short. | 0 new endpoints; 1 new module. |

**Total estimated endpoint surface:** 6–8 new admin routes (5–7 GETs + 1 POST), all `platform_admin`-gated, all with audit-log rows on call, all returning forensic-shape JSON. Each sub-commit is followed by a verify gate (FINAL STEP 26 MATRIX 23/23 green) before the next sub-commit starts. The `SessionLocal` import is dropped per-pillar inside C.1–C.4; the import line stays present in any pillar still under migration to keep mid-state honest.

**Verify-after-every-commit doctrine** is enforced from C.1 through C.6 inclusive. Any sub-commit whose verify gate is not 23/23 green halts C-series work until the regression is understood and either fixed or honestly logged as a new drift.

### D-call-helper-missing-params-kwarg-2026-05-05

RESOLVED 2026-05-06. See `docs/CANONICAL_RECAP.md` §15 "Resolved by Step 29 (verify-harness debt closure, no production touch)" for full resolution evidence. Original surface and resolution path (extend `call()` with `params=` kwarg, migrate P14 inlined query string, add unit tests) preserved here for cross-reference:

**Surface:** `app/verification/http_client.py:call()` did not accept `params=`. P14 line ~347 had the workaround comment. Only one site used inlined query strings.

**Resolution path (SHIPPED at Commit B `17cd12b`):**
1. Extended `call()` signature: `def call(method, path, key, *, json=None, files=None, data=None, params=None, expect=200, client=None) -> httpx.Response`.
2. Forwarded `params=params` to `httpx.Client.request(...)`.
3. Migrated P14's inlined `?audit_label=...` back to `params={"audit_label": ...}`.
4. Added 5 unit tests in `tests/verification/test_http_client_params.py` covering basic forwarding, URL-encoding of unsafe chars, interaction with pre-existing query strings, `params=None` no-op, `params={}` no-op.

**Drift entry flipped to RESOLVED at Commit B.2 (`8a06652`).**

### D-step29-audit-undercounts-verify-debt-2026-05-06

(NEW, opened-and-closed in this commit B.3.) The 2026-05-05 v1 enumeration of `D-verify-task-pure-http-2026-05-05`'s surface counted `SessionLocal()` block opens (and miscounted that at 8 vs the true 12) but did not enumerate the reads inside each block, did not separate forensic reads from producer-side service/Celery calls, and did not surface the one borderline direct ORM write in P11 F10. The v1 estimate of "4-7 new admin GET routes" was based on this undercount and would have caused Commit C.1's sub-commit boundary to not match the actual code surface, leaving P11 partially migrated mid-Step-29.

**Diagnosis:** The v1 audit was authored 2026-05-06 ~19:55 EDT under time pressure (Step 28 close-out in flight), and its §6 enumeration was a `grep -n SessionLocal` snapshot rather than a read of each block's body. The v1 also did not consider that some in-process calls are by deliberate design what the pillar is testing and therefore cannot be migrated without losing the assertion (the producer-side exemption now codified above).

**Resolution (this commit, B.3, docs-only):** §6 above re-enumerated against the tree at `8a06652` with full per-callsite breakdown. New architectural rule "Producer-side exemption" added inline. Commit C sub-shape revised from v1's 4 sub-commits (estimated 4–7 GETs) to v2's 6 sub-commits C.1–C.6 (estimated 6–8 routes including 1 POST). §10 Commit C entry updated to point at this revised shape. No production state touched. No verify re-run — docs-only.

**Forward-looking guard:** Future Step-N audit documents that enumerate code-surface debt MUST do so by reading each call-site's body (not just `grep` for the entry-point construct), MUST distinguish read-only forensics from producer-side mutations, and MUST declare any architectural exemption inline at the audit point rather than discovering it mid-implementation. A pre-implementation re-audit pass (B.3 here) is now the locked pattern when an audit's enumeration was not produced by per-callsite read.

---

**Either drift can be the FIRST code commit after the harness lands**, or they can be folded into the same commit — historical recommendation, superseded by the no-deferral commit order locked in §10 below.

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
5. ✅ Closes drifts `D-verify-task-pure-http-2026-05-05` AND `D-call-helper-missing-params-kwarg-2026-05-05` within Step 29 — NOT deferred to a follow-up step. Per the 2026-05-06 ordering revision (see §10 below), both drift closures land BEFORE the pytest harness so the harness wraps the honest end-state, not the transitional one. **Status update 2026-05-06 ~21:00 EDT: `D-call-helper-missing-params-kwarg-2026-05-05` is RESOLVED — code shipped at `17cd12b` (Commit B), drift entry flipped to RESOLVED at Commit B.2 (`8a06652`). Resolution evidence: `step29-commit-b1-local.json` 23/23 green run, which exercises the post-B `params=` kwarg path in P14. `D-verify-task-pure-http-2026-05-05` remains OPEN and is owned by Commit C, with a revised 6-sub-commit shape (C.1–C.6) authored by Commit B.3 (`<this-commit>`). The revised §6 enumeration distinguishes 27 forensic reads + 1 borderline ORM write (in scope) from 5 producer-side callsites (exempt by the new rule codified in B.3). Adjacent drift `D-step29-audit-undercounts-verify-debt-2026-05-06` was opened-and-closed by B.3 to log the honest reason the v1 enumeration was insufficient and the forward-looking guard against the same pattern in future Step-N audits.**

---

## 10. Audit closure (REVISED 2026-05-06 ~19:55 EDT — no-deferral order)

This audit is **read-only**. No source files in `app/` are modified. The next commits are the implementation commits.

**REVISED commit shape for the implementation (replaces the v1 ordering):**

The initial draft of this section recommended landing the pytest harness first and folding the two drifts into a follow-up commit "for smaller blast radius." On re-read against the user's standing principle — "we are designing so let us not defer errors, it could come back to bite us" / "honest long term fixes and not just taking shortcuts" / "I dont want to defer anything we need to be a little due dilligent with our business" — that recommendation was the avoiding-problems pattern. The drifts have already been deferred once (logged 2026-05-05, parked for Step 29). Step 29 IS the explicit window to close them. Landing the harness first and pushing the drifts forward a third time would be a textbook lazy-defer dressed up as engineering hygiene.

Revised order (all three commits land within Step 29):

- **Commit A (this audit):** docs-only, no tag. SHIPPED at `4212072`.
- **Commit B — `D-call-helper-missing-params-kwarg-2026-05-05` closure:** smallest of the three, lowest risk. Extend `app/verification/http_client.py:call()` to accept `params=` and forward to `httpx`. Migrate P14 line 347's inlined `?audit_label=...` back to `params={"audit_label": ...}`. Add a unit test for `call(..., params={"k": "v"})`. Verify FINAL STEP 26 MATRIX 23/23 green before moving on. Verify-after-every-commit doctrine RE-ENGAGES here. No tag. **SHIPPED at `17cd12b`. Drift entry RESOLVED at Commit B.2 (docs-only flip, no production touch, no verify re-run — resolution evidence is the post-B.1 `step29-commit-b1-local.json` 23/23 green run which exercises the post-B `params=` kwarg path in P14).**
- **Commit B.2 — docs-only flip of `D-call-helper-missing-params-kwarg-2026-05-05` from DEFERRED to RESOLVED (NEW, 2026-05-06 ~20:50 EDT):** docs-only edit-pass on `docs/CANONICAL_RECAP.md` and this file. Strikes through the original DEFERRED drift entry with a forward-pointer to the new "Resolved by Step 29 (verify-harness debt closure, no production touch)" subsection in CANONICAL_RECAP §15, which records the resolution lineage: Commit B (`17cd12b`) shipped the code fix, Commit B.1 (`e4b03a4`) shipped an unrelated honesty fix discovered by verify-after-every-commit, and the resolution claim is backed by the `step29-commit-b1-local.json` 23/23 green run captured 2026-05-06 ~20:40 EDT (the B.1 run, not a B-only run, because B.1 superseded the B-only run in the audit trail). No production touch. No verify re-run — there is no code change to validate. No tag. **SHIPPED at `8a06652`.**
- **Commit B.3 — re-audit of `D-verify-task-pure-http-2026-05-05` surface + new drift `D-step29-audit-undercounts-verify-debt-2026-05-06` opened-and-closed (NEW, 2026-05-06 ~21:00 EDT):** docs-only re-audit. Reading the four pillar files end-to-end against the tree at `8a06652` revealed that the v1 §6 enumeration counted only `SessionLocal()` block opens (and miscounted at 8 vs the true 12), did not enumerate the reads inside each block (true count is 27 distinct forensic queries), did not separate read-only forensics from in-process producer-side calls (`MemoryService.enqueue_extraction`, `MemoryRepository.upsert_by_message_id`, `extract_memory_from_turn.delay()`/`apply_async()`), and did not surface the one borderline direct ORM write in P11 F10 (`luciel_instances.active` toggle). The v1's "4-7 new admin GET routes" estimate was based on this undercount. B.3 replaces §6 with a per-callsite breakdown by pillar, codifies a new architectural rule ("Producer-side exemption": a verification pillar may act as a direct Celery producer or service-layer caller WHEN AND ONLY WHEN the assertion under test is a property of the producer-side path itself — latency, idempotency, or a payload shape that the HTTP API contract does not permit — and the exemption must be declared inline in the pillar's docstring), and revises the Commit C sub-shape from v1's 4 sub-commits to v2's 6 sub-commits (C.1–C.6, estimated 6–8 new routes including 5–7 GETs and 1 POST for the borderline ORM write). The exemption preserves the security boundary `D-verify-task-pure-http` was actually written to enforce: the verify task does not hold privileges (DB role grants, secrets) that the production worker doesn't already have, since every exempt callsite uses the SAME `app.worker.celery_app` and the SAME `app.memory.service` that production code uses. The `luciel_worker` Postgres role's zero-INSERT/UPDATE-on-`scope_assignments`/`users` boundary established by Commit 4 of Phase 2 is unchanged. Driver discovery: while preparing C.1, a per-callsite read showed P11's `SessionLocal()` block at line 191–503 contains direct `MemoryService` and `MemoryRepository` calls plus `apply_async()` enqueues plus an ORM write to `luciel_instances.active` — surfaces v1 had not enumerated. Per the user's standing principle ("we are designing so let us not defer errors" / "honest long term fixes and not just taking shortcuts" / "we cannot make any compromises in our security and programmatic errors"), this re-audit had to land BEFORE C.1 was authored, not be discovered mid-commit. No production touch. No verify re-run — docs-only. No tag.
- **Commit B.1 — `D-pillar-13-mode-gate-broker-only-2026-05-06` closure (NEW, inserted 2026-05-06 ~20:30 EDT):** discovered when B's local verify gate ran 22/23 with P13 silently FAIL on Assertion A2 in the absence of a local Celery worker. Diagnosis: P13's mode-detection line `mode_full = _broker_reachable()` checks Redis ping only (proves enqueue capability, not consumer presence) while P11's mode-detection correctly uses `_broker_reachable() and _worker_reachable()`; without a worker subscribed to the queue, P13 enqueues the spoof payload, sleeps 60s, then asserts on an audit row no worker had any chance to write. The Gate 6 worker code at `app/worker/tasks/memory_extraction.py` is innocent. Fix: mirror P11's `_worker_reachable()` helper inline in P13 (verbatim copy, no shared module yet) and update the mode-detect line to require both probes True. Inline duplication is intentional and bounded — both copies will move into a shared `app/verification/_infra_probes.py` module in Commit D when the pytest harness lift already needs to touch the verification infra layer. Doing the consolidation in B.1 would expand the blast radius to P11 unnecessarily. Verify gate after B.1: P13 declares MODE=degraded under no-worker conditions and the suite returns 23/23 green honestly. Full A1/A2 spoof-guard verification is owned by the prod gate that runs against the deployed Celery worker. No tag. Forward-looking guard recorded in CANONICAL_RECAP §15: any future verification pillar declaring a `MODE=full` branch dependent on async infrastructure must verify the FULL chain (broker AND consumer), never the nearest-hop reachability alone.
- **Commit C — `D-verify-task-pure-http-2026-05-05` closure (REVISED 2026-05-06 ~21:00 EDT by Commit B.3):** 27 forensic reads + 1 borderline ORM write across P11/P12/P13/P14 (per-callsite breakdown in §6 above). 5 producer-side callsites (P11 F1/F2/F4/F10 and P13 A1/A2 spoof enqueue) are scoped OUT under the new "Producer-side exemption" rule because the assertion under test IS the producer path. Estimated 6–8 new platform-admin-gated routes (5–7 GETs + 1 POST), all suffixed `_step29c`, all with audit-log rows on call. Sub-commit shape (replaces v1's 4-sub-commit shape): **C.1** P11 reads (4 GETs new), **C.2** P12 reads (1–2 GETs, some reuse), **C.3** P13 reads (0–1 GET, heavy reuse + spoof exemption declaration), **C.4** P14 reads (1 GET, mostly reuse), **C.5** P11 F10 borderline ORM write → admin POST + harness migration, **C.6** cross-pillar cleanup (drop `SessionLocal` imports, consolidate `_worker_reachable()` into `app/verification/_infra_probes.py`, add `forensics_get()` wrapper to `http_client.py`). Verify 23/23 green between each sub-commit. Drop `from app.db.session import SessionLocal` per-pillar inside C.1–C.4; the import line stays present in any pillar still under migration to keep mid-state honest. No tag until the suite is fully pure-HTTP.
- **Commit D — pytest harness:** lands AFTER C, on the pure-HTTP suite, so the harness wraps the honest end-state. Thin `tests/integration/test_pillars.py`, pytest+pytest-asyncio in deps, `verify` marker, `.github/workflows/verify.yml`. Tag candidate: `step-29-complete`.

Why this order matters: doing the harness first then refactoring under it would mean rewriting 12 pillar test bodies twice (once into pytest collection, once for the HTTP migration). Doing the HTTP migration first means the harness wraps a clean surface and Commit D becomes a thin wrapper rather than a deep rewrite.

**Verify-after-every-commit doctrine** is fully enforced from Commit B onward, including after each per-pillar sub-step inside Commit C. The B.1 insertion (2026-05-06) is itself an instance of this doctrine working as intended — the discovery of the P13 mode-gate honesty defect happened precisely because verify was re-engaged immediately after B; under the v1 "land harness, defer drifts" ordering it would have been masked indefinitely behind the harness's own test-collection layer.

End of audit.
