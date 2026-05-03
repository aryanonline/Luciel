# Phase 3 — Compliance Backlog

**Status:** Tracked. Items here are **not** part of Step 28 Phase 2.
They surfaced during Phase 2 hotfix diagnosis and represent compliance
gaps in service of Luciel's PIPEDA posture and future SOC 2 / GDPR
readiness for the multi-tenant brokerage SaaS use case.

**Owner:** Aryan Singh
**Created:** 2026-05-03 (mid-Phase 2)
**Trigger:** Phase 2 HOTFIX commit `2c7d0fb` revealed under-instrumented
audit-emission paths during the Pillar 17 / Pillar 19 diagnosis.
**Schedule rule:** Phase 3 begins **after** Phase 2 prod-touching commits
4-7 land green and stable in prod for ≥ 7 days. Do not interleave.

---

## Severity tiers (compliance-first, not feature-first)

- **P0 — silent integrity loss.** A regulator-facing claim ("we revoke
  keys", "we audit tenant lifecycle", "we cascade retention") has no
  audit evidence. Sue-risk on customer dispute. Fix before any sales
  motion that promises audit immutability.
- **P1 — gap with workaround.** Compliance posture is provable today
  via DB inspection but not via the audit-log API surface. Fixable by
  threading audit emission through the missing layer; no data loss
  if deferred a quarter.
- **P2 — documentation / posture.** Behavior is correct but undocumented;
  a regulator or auditor would have to reconstruct intent from code.
  Risk surfaces during diligence, not during operation.

---

## P3-A. OnboardingService writes ZERO audit rows  *(P0)*

**Discovered:** 2026-05-03 during Pillar 19 diagnosis.

**What's missing:** When a new tenant is onboarded via
`POST /api/v1/admin/tenants/onboard` → `OnboardingService.onboard_tenant`,
the service creates atomically:

1. `tenant_configs` row
2. `domain_configs` row (default domain)
3. `retention_policies` rows (5 categories: sessions, messages,
   memory_items, traces, knowledge)
4. `api_keys` row (the first admin key)

**Zero of these emit `admin_audit_logs` rows.** Verified by:
```bash
grep -c "audit\|AdminAudit\|record(" app/services/onboarding_service.py
# returns 0
```

The downstream `api_key_service.create_key()` call also skips audit
emission — only the API endpoint `POST /api/v1/admin/api-keys` writes
the `ACTION_CREATE` audit row, and onboarding bypasses that endpoint
to mint its first key directly through the service.

**Why this is P0:**
- A brokerage onboarded today has no immutable record of:
  - WHO created the tenant (which platform_admin actor)
  - WHEN it was created (audit log timestamp, not just `created_at`)
  - WHAT retention policies were initially set (vs later modified)
  - WHAT the first admin key's permissions and rate limit were
- For PIPEDA: the tenant lifecycle event is not in the audit trail.
  A breach investigation would have to fall back to `created_at`
  columns and infer actor from operational logs.
- For SOC 2: this is a CC7.2 audit-evidence gap.
- For commercial defense: a brokerage disputing a charge or claiming
  unauthorized account creation cannot be answered from the audit log.

**Fix shape (when prioritized):**
1. Add `audit_ctx: AuditContext` parameter to `OnboardingService.onboard_tenant`
   (REQUIRED, not optional — same contract as `bulk_soft_deactivate_memory_items_for_domain`).
2. Emit four audit rows in the same transaction as the writes:
   - `ACTION_CREATE` / `RESOURCE_TENANT` (with `tenant_id=new_tid`)
   - `ACTION_CREATE` / `RESOURCE_DOMAIN` (with default_domain_id)
   - `ACTION_CREATE` / `RESOURCE_RETENTION_POLICY` × 5 (one per category, OR a single bulk row with breakdown)
   - `ACTION_CREATE` / `RESOURCE_API_KEY` (admin key — currently emitted only by API endpoint mint path)
3. Thread `audit_ctx` down from the API endpoint at `app/api/v1/admin.py`
   line 151 (`OnboardingService(db)` construction).
4. Add Pillar 20 (or extend Pillar 1) to assert exactly four (or 8 if
   per-policy) `ACTION_CREATE` rows tagged with the new tenant_id
   appear after onboard.

**Estimated effort:** 1 commit, ~120 LOC + 1 new pillar (~80 LOC).
**Touches:** `app/services/onboarding_service.py`, `app/api/v1/admin.py`,
`app/verification/tests/pillar_20_onboarding_audit.py` (new).
**Cross-references:** canonical-recap §4.1 (drift list), Invariant 4
(audit-before-commit), Pillar 19 docstring.

---

## P3-B. ApiKeyService.create_key writes no audit row  *(P1)*

**Discovered:** 2026-05-03 during Pillar 19 diagnosis (related to P3-A).

**What's missing:** `ApiKeyService.create_key()` (api_key_service.py
line 118-212) flushes a new `api_keys` row but emits no `admin_audit_logs`
row. The audit emission lives in the API endpoint at
`app/api/v1/admin.py` line 594-611, *after* the service call returns.

**Why this is P1, not P0:** Today, the only callers of `create_key` that
skip the API endpoint are:
1. `OnboardingService.onboard_tenant` (covered by P3-A above)
2. Internal scripts (`scripts/mint_platform_admin_ssm.py`,
   `scripts/rotate_platform_admin_keys.py`) — bootstrap and break-glass
   flows that already log to operational logs and SSM.

So the gap is real but the blast radius is contained. Still worth
fixing to make the contract uniform: every `api_keys` insertion produces
an audit row, end of story.

**Fix shape:**
1. Move audit emission into `ApiKeyService.create_key()` itself, gated
   on `audit_ctx is not None` (with `AuditContext.system(label="create_key")`
   fallback for legacy/script callers).
2. Remove the duplicate emission block from `app/api/v1/admin.py`
   line 594-611 to avoid double-counting (or audit dedup if the API
   endpoint wants additional context beyond the service-level row).
3. Add a regression test that exercises `OnboardingService` and
   asserts the admin key's `ACTION_CREATE` audit row is present (this
   overlaps with P3-A's Pillar 20).

**Estimated effort:** 1 commit, ~40 LOC. Trivial *after* P3-A lands.
**Cross-references:** Pillar 17 docstring (D5 contract).

---

## P3-C. Bulk-summary audit emission is undocumented as compliance posture  *(P2)*

**Discovered:** 2026-05-03 during Pillar 7 diagnosis.

**What's missing:** Several cascade paths emit ONE summary audit row
covering N affected resources, not N+1 individual rows:

- `LucielInstanceRepository.deactivate_all_for_domain` — one row covers
  both domain-scope and agent-scope LucielInstances.
- `AdminService.bulk_soft_deactivate_memory_items_for_domain` — one row
  covers all `memory_items` rows attributed to agents in the domain.
- `AdminService.bulk_soft_deactivate_memory_items_for_tenant` — same
  pattern at tenant scope.
- `AdminService.deactivate_domain` — one row covers all `agents` in
  the domain.

The `after_json` payload always contains `affected_pks`, `count`, and
sometimes a `breakdown` (per-agent or per-instance grouping). So the
information IS preserved per-resource — just compressed into one row.

**Why this is correct but undocumented:**
- For audit *export* (CSV/JSON for a regulator), one row per cascade
  event reads more naturally than N+1 rows reconstructed from scratch.
- For audit *immutability proofs* (e.g. hash-chain of rows), fewer
  rows means smaller proofs and less write amplification.
- For audit *retention* (PIPEDA P5), bulk rows compress better and
  age out cleaner than per-resource rows.

**The gap:** No document explains this is the *intended posture*.
A regulator asking "why does row N represent 47 deactivated memory items
instead of 47 rows" needs a written answer in the canonical recap or
in a dedicated `docs/compliance/audit-emission-posture.md`.

**Fix shape:**
1. Add `docs/compliance/audit-emission-posture.md` documenting the
   bulk-vs-per-row decision matrix, what `after_json` carries, how
   to expand a bulk row into per-resource detail at audit-export time.
2. Add a §X to canonical-recap referencing the posture doc.
3. (Optional) Add a helper in `app/repositories/admin_audit_repository.py`
   that takes a bulk row and emits per-resource shadow rows on demand
   — useful if a customer/regulator demands per-row format.

**Estimated effort:** 1 commit, ~60 LOC of docs + 1 recap section update.
**Touches:** `docs/compliance/audit-emission-posture.md` (new),
`docs/CANONICAL_RECAP.md`, optionally `admin_audit_repository.py`.

---

## P3-D. Cross-tenant scope-leak fuzz suite  *(P1)*

**Discovered:** 2026-05-03 (proactive, not bug-driven).

**What's missing:** Pillar 19 asserts that *one* specific cross-tenant
attempt (a tenant_admin querying `?tenant_id=<other-real-tenant>`)
returns rows scoped to the caller's tenant. But the audit-log API has
many filter axes:

- `tenant_id` (currently guarded)
- `domain_id`
- `agent_id`
- `actor_label`
- `resource_pk`
- `resource_type`
- date ranges

Each axis is a potential leak vector if scope-override middleware
forgets to also force that filter for non-platform-admin callers.

**Fix shape:**
1. Add `app/verification/tests/pillar_21_audit_log_fuzz_scope.py` — for
   each filter axis, mint a row in tenant B, then query as tenant_admin
   of tenant A with that filter pointing at tenant B's row. Assert the
   row is NOT visible.
2. Cover positive case (caller's own data IS visible with the same
   filter shape) so the test isn't passing by accident.

**Estimated effort:** 1 commit, ~150 LOC.

---

## P3-E. Audit-log immutability proof  *(P1)*

**Discovered:** 2026-05-03 (proactive).

**What's missing:** Phase 2 Commit 2 assertion 4 confirms POST/PUT/PATCH/
DELETE on `/audit-log` return 404/405. Good — that closes the API
surface. But:

- DB role grants are not asserted at runtime. A `luciel_admin` role
  with UPDATE/DELETE on `admin_audit_logs` would let a compromised
  app process tamper with the audit log directly via SQL.
- No hash-chain or write-once-read-many (WORM) enforcement at the row
  level. An operator with DB superuser could rewrite history.

**Fix shape (per pillar):**
1. Pillar 22: at runtime, query `information_schema.role_table_grants`
   and assert the app's DB role has `INSERT, SELECT` only on
   `admin_audit_logs`, no UPDATE/DELETE.
2. Pillar 23 (longer): add a per-row hash chain (each row's hash
   includes the previous row's hash). On read, verify the chain.
   Tampering breaks the hash chain immediately. This needs an Alembic
   migration to add `hash` and `prev_hash` columns.

**Estimated effort:** Pillar 22: ~1 commit, ~80 LOC. Pillar 23:
~3 commits, schema + repo + verification, ~400 LOC.

---

## P3-F. Retention purge audit coverage  *(P1)*

**Discovered:** 2026-05-03 (during Phase 2 Commit 8 retention work).

**What's missing:** `RetentionService.enforce_all_policies` writes
`deletion_logs` rows (not `admin_audit_logs`) for purged data. Per
PIPEDA P5 / data-minimization, the deletion event itself IS recorded.
But `deletion_logs` is a separate table from `admin_audit_logs` — it
doesn't surface in the audit-log API and isn't subject to the same
immutability guarantees.

**Decision needed:** Should retention purges *also* emit a row to
`admin_audit_logs` (for unified audit export), or is `deletion_logs`
the canonical compliance record? Currently both exist with overlapping
but non-identical semantics.

**Fix shape:** Either
- (A) Document `deletion_logs` as the canonical record for purge events,
  exclude from the `admin_audit_logs` API contract, treat as separate
  audit stream. Add §to recap.
- (B) Mirror every `deletion_logs` insert with an `admin_audit_logs`
  row of `action='retention_purge'`, accept the duplication, gain
  unified export.

**Estimated effort:** Decision + 1 commit, ~80 LOC either way.

---

## Sequencing

When Phase 2 lands and we re-open compliance work:

1. **P3-A first.** Onboarding audit gap is the single P0. Everything
   else is P1/P2.
2. **P3-B as a bundle with P3-A.** They share the audit-emission code path.
3. **P3-C** alongside the next canonical recap update (cheap, doc-only).
4. **P3-D** before any sales motion that markets cross-tenant isolation.
5. **P3-E and P3-F** before the first SOC 2 readiness assessment.

**Estimated total:** ~6 commits, ~700 LOC of code + ~200 LOC of docs.
~1-2 weeks of focused work.

---

## How to update this file

When an item lands: move it to a `## Resolved` section at the bottom
with the resolving commit SHA, date, and a one-line summary of the
fix. Don't delete — the audit-of-the-audit-backlog is itself useful.

When a new gap is discovered during another step's work: add it as
P3-G, P3-H, etc. with the same severity / discovered / what's missing
/ why it matters / fix shape / effort structure used above.
