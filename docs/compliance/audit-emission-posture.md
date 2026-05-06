# Audit emission posture

**Status:** canonical (Step 28, C7)
**Owner:** platform compliance
**Last updated:** 2026-05-06
**Resolves backlog items:** P3-C (bulk-summary audit emission), P3-F (retention purge audit coverage)

## 1. Why this document exists

Luciel persists compliance-relevant events in two append-only streams:

- `admin_audit_logs` -- actor/action/resource events for control-plane and
  data-plane writes (tenant onboarding, scope changes, key lifecycle,
  cascade deactivations, etc). Each row is hash-chained (Pillar 23,
  P3-E.2) and constrained append-only at the DB-grant level (Pillar 22,
  P3-E.1).
- `deletion_logs` -- bulk purge / anonymize events emitted by the
  retention scheduler and the manual-purge admin path
  (`RetentionService.enforce_all_policies` /
  `RetentionService.run_for_category`).

A regulator asking "show me every action that touched tenant X data"
needs a clear, written answer to two specific questions:

1. Why does retention purge **not** appear in `admin_audit_logs`?
2. Why does a single cascade row in `admin_audit_logs` represent N
   affected resources instead of N rows?

This document is that written answer. Both choices are deliberate, both
preserve full forensic detail, and both are verifiable from the schema.

## 2. The two streams at a glance

| Property | `admin_audit_logs` | `deletion_logs` |
|---|---|---|
| Granularity | Per-action (one event = one row, except cascade -- see Section 4) | Per-purge-run (one batch = one row) |
| Actor | `actor_user_id` / `actor_label` (human or system) | `triggered_by` (`scheduler` or `admin:<user>`) |
| Resource shape | `resource_type` + `resource_pk` + `resource_natural_id` | `data_category` + `cutoff_date` |
| Bulk affordance | `after.count`, `after.affected_pks`, `after.breakdown` | `rows_affected` (top-level integer) |
| Append-only enforcement | DB grants (Pillar 22) + hash chain (Pillar 23) | Append-only by code; INSERT-only path |
| Retention | PIPEDA P5 audit-class retention | Minimum 2 years per `DeletionLog` docstring (PIPEDA breach record) |
| Tenant scoping | `tenant_id` (NULL for platform-scope rows) | `tenant_id` (NULL for platform-wide policies) |
| API surface | `/api/v1/audit-logs` | `/api/v1/retention/logs` |

The two streams are **complementary**, not redundant. A unified audit
export merges them -- see Section 5.

## 3. Canonical decision: `deletion_logs` is the canonical record for retention purges (P3-F resolved as Option A)

### 3.1 Decision

`RetentionService` writes one `deletion_logs` row per `(category, tenant)`
purge run via `app/policy/retention.py:307` (`DeletionLog(...)` ->
`RetentionPolicyRepository.log_deletion`). It does **not** mirror that
event into `admin_audit_logs`. This is the canonical compliance record
for retention purge events.

### 3.2 Why this is correct (and why option B was rejected)

The alternative -- mirror every `deletion_logs` insert with an
`admin_audit_logs` row of `action='retention_purge'` -- was rejected for
four concrete reasons:

1. **Schema fit.** `admin_audit_logs` is built around per-resource
   actor/before/after pairs. A purge has no per-row before/after -- it
   has a `cutoff_date` and a row count. Forcing the data into
   `admin_audit_logs` either drops information (no `cutoff_date` /
   `data_category` columns) or stuffs it into `after_json` (already
   what `deletion_logs` carries natively in typed columns).
2. **Write amplification.** The retention scheduler runs daily and
   touches every active retention policy. Mirroring every purge into
   `admin_audit_logs` doubles the audit-stream insert volume on a
   critical scheduled job -- and that stream is now hash-chained, so
   every extra insert advances the chain.
3. **Hash-chain churn.** Pillar 23 (P3-E.2) makes the
   `admin_audit_logs` chain a forensic primitive. The cleaner that
   stream, the easier downstream proofs become. Bulk machine-driven
   purges that already have their own immutable record do not belong
   in the chain.
4. **Action-class hygiene.** `admin_audit_logs` represents control-plane
   and data-plane *control* events (who deactivated what, who minted
   what key). Retention purge is a different action class -- a
   policy-driven scheduled data lifecycle event. Keeping the two
   classes in separate streams maps cleanly to the PIPEDA distinction
   between audit logging (P4 -- Accountability) and retention
   recordkeeping (P5 -- Limiting Retention).

### 3.3 What `deletion_logs` carries (forensic completeness)

Each `DeletionLog` row records:

- `tenant_id` -- purge scope (NULL = platform-wide policy)
- `data_category` -- `sessions` / `messages` / `memory_items` / `traces` / `knowledge_embeddings`
- `action_taken` -- `deleted` or `anonymized`
- `rows_affected` -- integer count
- `cutoff_date` -- ISO-8601 boundary; rows older than this were affected
- `triggered_by` -- `scheduler` (automatic) or `admin:<user>` (manual)
- `reason` -- optional free text; **always** populated on partial
  failure with `PARTIAL: <ExceptionType>: <message>` (see
  `app/policy/retention.py:314`)
- `created_at` -- when the purge ran (TimestampMixin)

This is strictly more information than a per-row `admin_audit_logs`
mirror would be able to record without schema additions.

### 3.4 Audit-export contract

For a regulator-facing "all actions on tenant X data" export:

1. Pull `admin_audit_logs WHERE tenant_id = :t` for control/data-plane events.
2. Pull `deletion_logs WHERE tenant_id = :t OR tenant_id IS NULL` for
   retention events (NULL covers platform-wide policies that touched
   tenant X's category data).
3. Order by `created_at`. Both streams use `TimestampMixin` so
   timestamps are comparable.
4. Render as a single timeline.

There is no information loss in this merge -- every event lives in
exactly one stream.

### 3.5 Known gap (tracked, not in scope for C7)

`app/policy/retention.py:322-327` swallows `DeletionLog` write failures
to a `logger.error` with no metric and no fallback audit row. If the
delete/anonymize succeeded but the audit insert fails, the rows are gone
but the compliance record is missing. Tracked in the C11 sweep close
(P3-F follow-up) as: emit a Prometheus counter on
`retention_audit_write_failure_total` and write a fallback
`admin_audit_logs` row with `action='retention_audit_failure'` so the
gap surfaces in monitoring and in the unified export.

## 4. Bulk-summary audit emission (P3-C resolved)

### 4.1 Posture

Several cascade paths emit **one** summary `admin_audit_logs` row
covering N affected resources, not N+1 individual rows. This is the
intended posture, not a defect. The disaggregated detail is preserved
in `after_json`.

### 4.2 Bulk paths and their `after_json` contracts

| Path | Audit action | `after_json` keys |
|---|---|---|
| `LucielInstanceRepository.deactivate_all_for_domain` (`app/repositories/luciel_instance_repository.py:493`) | `ACTION_CASCADE_DEACTIVATE` | `count`, `affected_pks`, `affected_instance_ids`, `trigger` |
| `AdminService.deactivate_domain` agents cascade (`app/services/admin_service.py:243`) | `ACTION_CASCADE_DEACTIVATE` | `count`, `affected_pks`, `trigger` |
| `AdminService.bulk_soft_deactivate_memory_items_for_domain` (`app/services/admin_service.py:695`) | `ACTION_CASCADE_DEACTIVATE` | `count`, `scope`, `tenant_id`, `domain_id`, `breakdown` (per `agent_id`), `trigger`, `updated_by` |
| `AdminService.bulk_soft_deactivate_memory_items_for_tenant` (`app/services/admin_service.py:377`) | `ACTION_CASCADE_DEACTIVATE` | `count`, `scope`, `tenant_id`, `breakdown` (per `agent_id` x `luciel_instance_id`), `trigger`, `updated_by` |
| `AdminService.deactivate_tenant_with_cascade` agents step (`app/services/admin_service.py:946`) | `ACTION_CASCADE_DEACTIVATE` | `count`, `affected_pks` (`ac_pks`), `trigger` |
| `AdminService.deactivate_tenant_with_cascade` domains step (`app/services/admin_service.py:993`) | `ACTION_CASCADE_DEACTIVATE` | `count`, `affected_pks` (`dc_pks`), `trigger` |

### 4.3 Why bulk-summary is correct

1. **Information preservation.** Every per-resource ID is recoverable
   from `after_json.affected_pks` (or `after_json.breakdown` when grouping
   is needed). Nothing is lost.
2. **Audit-stream volume.** A tenant deactivation can cascade across
   thousands of `memory_items` and hundreds of agents. N+1 rows would
   bloat the stream by orders of magnitude per tenant offboarding.
3. **Hash-chain economy.** Pillar 23 chains every row. Bulk emission
   keeps the chain proportional to events-of-record, not to data
   volume.
4. **Export ergonomics.** A regulator reading a tenant-offboarding
   timeline reads "cascade deactivated 47 memory items at 2026-05-06
   14:32" once, not 47 identical-actor identical-action rows. The
   per-resource detail is one `after_json` field away when needed.

### 4.4 How to expand a bulk row into per-resource detail

For per-resource forensic queries:

```python
# Given a bulk admin_audit_logs row `bulk_row`:
detail = bulk_row.after_json or {}
count = detail.get("count")
affected_pks = detail.get("affected_pks") or []
breakdown = detail.get("breakdown") or []
# `affected_pks` is the simple list; `breakdown` is the grouped form
# (per agent_id, per luciel_instance_id, etc) when present.
```

Tooling that needs strict per-row format (e.g. a customer DSR export
demanding one CSV row per affected memory) can iterate `affected_pks` /
`breakdown` and synthesize per-row records at export time. The bulk row
is the source of record; the synthesized rows are derived view.

### 4.5 Empty-cascade emission contract

`AdminService.bulk_soft_deactivate_memory_items_*` emit a
`ACTION_CASCADE_DEACTIVATE` row **even when `count == 0`** (see method
docstrings at `app/services/admin_service.py:399-404` and
`app/services/admin_service.py:722-725`). This is intentional -- it
records that the cascade ran and inspected the scope, distinct from
"the cascade didn't run".

`LucielInstanceRepository.deactivate_all_for_domain` currently emits
**only when `updated > 0`** (`app/repositories/luciel_instance_repository.py:539`).
This minor inconsistency does not affect compliance posture (the
parent cascade row in the AdminService path always emits) but is
tracked as a Phase 4 cosmetic in the C11 sweep close: align all bulk
paths to "always emit, even when count == 0" for forensic
completeness.

## 5. Unified audit export (regulator-facing)

The canonical "everything that happened to tenant X" export is the
ordered union of:

- `admin_audit_logs WHERE tenant_id = :t` (control/data-plane events,
  hash-chained)
- `deletion_logs WHERE tenant_id = :t OR tenant_id IS NULL` (retention
  events, append-only by construction)

ordered by `created_at`. The bulk rows in `admin_audit_logs` are
expanded on demand using the contract in Section 4.4 if a per-resource format
is required.

## 6. Cross-references

- Pillar 19 -- `admin_audit_logs` API tenant-scoping enforcement
- Pillar 20 -- onboarding service emits 4 ACTION_CREATE audit rows (P3-A)
- Pillar 21 -- cross-tenant scope-leak fuzz suite (P3-D)
- Pillar 22 -- DB grants enforce audit-log append-only (P3-E.1)
- Pillar 23 -- audit-log hash chain integrity (P3-E.2)
- `docs/CANONICAL_RECAP.md` Section audit-emission-posture
- `docs/PHASE_3_COMPLIANCE_BACKLOG.md` P3-C, P3-F
