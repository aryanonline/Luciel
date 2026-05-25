# Arc 9 C4.4 — Service-layer audit (Wall 3 / instance_id)

**Audit author:** AI partner (autonomous mandate).
**Audit date:** 2026-05-24.
**Repo HEAD at audit start:** `9869155` (C4.3 merged).
**Audit scope:** every non-FastAPI code path that opens a DB session
against the Wall-1 (`tenant_id`) and Wall-3 (`luciel_instance_id`)
RLS-protected tables.

---

## 1. Question we are answering

C4.1 emitted the `app.instance_id` GUC. C4.2 wired
`get_tenant_scoped_db` to bind the instance_id ContextVar from
`request.state.luciel_instance_id`. C4.3 created the six per-table
Wall-3 RLS policies on `api_keys`, `knowledge_embeddings`,
`memory_items`, `sessions`, `traces`, `admin_audit_logs`.

But `get_tenant_scoped_db` is the FastAPI dependency. **Every code
path that opens a DB session outside an HTTP request bypasses it.**
Under the Arc 9 master flag `rls_tenant_context_enabled=True`, every
such path would hit the engine-level after_begin listener emitting
`set_config('app.admin_id', '', true)` + `set_config('app.instance_id',
'', true)` — empty strings — and the Wall-1 strict RLS policies on
C3.x tables would deny all reads and writes.

This audit identifies every such path, classifies it, and either
fixes it or formally records the gap with a remediation path.

---

## 2. Direct callers of the binding primitives (sanity check)

```
$ grep -rn "set_current_instance_id\|set_current_admin_id" app/ --include="*.py"
```

Only these legitimate sites in production code:

- `app/db/tenant_context.py` — the ContextVar definitions.
- `app/db/instance_context.py` — the ContextVar definitions.
- `app/api/deps.py:118-119` — the FastAPI dep producer.
- `app/db/tenant_scope.py` — C4.4 NEW: the non-HTTP binding helper.

**No rogue callers.** ✅

---

## 3. Direct GUC emitters (sanity check)

```
$ grep -rn "set_config.*app\." app/ --include="*.py"
```

Only `app/db/session.py:156` and `app/db/session.py:170` — the engine-
level after_begin listener. **No bypass paths.** ✅

---

## 4. Non-HTTP DB session paths

Surveyed every `SessionLocal()` and `Session(bind=...)` callsite in
`app/`. Three categories below.

### 4.A — Celery worker tasks

#### 4.A.1 `app/worker/tasks/memory_extraction.py::extract_memory_from_turn`

| Property | Value |
|---|---|
| Entry trigger | Celery task `memory.extract_memory_from_turn` |
| Carries tenant context? | YES — `tenant_id: str`, `luciel_instance_id: int \| None` in the task payload |
| Pre-C4.4 binding | NONE — `SessionLocal()` opened directly |
| Wall-1 tables written | `messages`, `sessions`, `memory_items`, `admin_audit_logs`, `scope_assignments` (read), `agent_configs` (read) |
| Wall-3 tables written | `memory_items` (C4.3c), `sessions` (C4.3d), `admin_audit_logs` (C4.3f) |
| Severity if unfixed | CRITICAL — under the master flag, every BEGIN inside the task emits empty GUCs; all C3.x strict policies deny → task fails permanently → DLQ flood |
| C4.4 fix | Wrap the entire function body in `with bind_tenant_scope(admin_id=tenant_id, instance_id=luciel_instance_id):` BEFORE opening `SessionLocal()`. SessionLocal opens inside the with-block so the first BEGIN sees the GUCs. |
| Status | **FIXED** in this commit. |

#### 4.A.2 `app/worker/tasks/retention.py::retention_purge`

| Property | Value |
|---|---|
| Entry trigger | Celery beat (nightly) |
| Carries tenant context? | PARTIAL — scans cross-tenant, then loops per `tenant_id` |
| Pre-C4.4 binding | NONE |
| Wall-1 tables deleted | The 12-step DELETE chain in `AdminService.hard_delete_tenant_after_retention` hits `messages`, `conversations`, `sessions`, `memory_items`, `traces`, `knowledge_embeddings`, `agent_configs`, `subscriptions`, `scope_assignments`, `api_keys`, `user_invites`, `user_consents`, `identity_claims`, `instances`, `admin_widget_domains`, `retention_policies`, `deletion_logs`, `admin_audit_logs` |
| Wall-3 tables deleted | `memory_items`, `sessions`, `traces`, `api_keys`, `knowledge_embeddings`, `admin_audit_logs` |
| Severity if unfixed | **CRITICAL with a twist** — Wall-1 strict policies pass once we bind `admin_id=tenant_id`. But Wall-3 NULL-permissive policies with empty `app.instance_id` admit ONLY rows where `luciel_instance_id IS NULL`. **Instance-scoped Wall-3 rows would SURVIVE the retention purge → inconsistent state across walls.** |
| C4.4 fix | Bind `admin_id=tenant_id, instance_id=None` so Wall-1 enforcement matches. **KNOWN GAP**: Wall-3 instance-scoped rows survive — this is documented inline at the call site and a runtime guard refuses to run when `rls_tenant_context_enabled=True` until C6 wires a BYPASSRLS role for ops paths. |
| Status | **PARTIAL FIX + GUARD**. Full fix blocked on C6. |

#### 4.A.3 Scan session inside `retention_purge`

| Property | Value |
|---|---|
| Tables read | `tenant_configs` |
| Wall-1 / Wall-3? | NEITHER — `tenant_configs` is metadata; no RLS policy in C1 findings |
| Status | **NO ACTION NEEDED**. ✅ |

### 4.B — Audit-log side sessions

#### 4.B.1 `app/memory/service.py:186` (extractor save-fail audit row)

| Property | Value |
|---|---|
| Pattern | Open a separate `SessionLocal()` to write an audit row in its own transaction, while the parent transaction (worker or HTTP) remains untouched. Critical for the hash-chain audit invariant. |
| Carries tenant context? | YES — the calling function has `tenant_id`, `luciel_instance_id`, `agent_id` in scope. |
| Wall-3 tables written | `admin_audit_logs` (C4.3f) |
| ContextVar inheritance | **The side session opens on the same engine and inherits the caller's ContextVars (async-local).** So when the worker calls this from inside its `with bind_tenant_scope(...)` block, the side session's BEGIN emits the correct GUCs. When an HTTP request calls this from inside `get_tenant_scoped_db`, same. |
| Status | **NO ACTION NEEDED.** The C4.4 worker retrofit makes this site correct-by-inheritance. Documented here so future readers don't mistake it for a gap. ✅ |

### 4.C — Future / out-of-scope

- **No CLI tooling** currently opens `SessionLocal` against Wall-1/Wall-3 tables. (Alembic migrations use the migration connection; admin scripts in `scripts/` are operational tools that connect with the bootstrap user role; both bypass RLS by virtue of role, not by missing ContextVars.)
- **No FastAPI background-task callsites** (`fastapi.BackgroundTasks`) open `SessionLocal` against Wall-1/Wall-3 tables. (Searched; none found.)

---

## 5. New helper: `app/db/tenant_scope.py::bind_tenant_scope`

```python
@contextmanager
def bind_tenant_scope(
    *,
    admin_id: Optional[str],
    instance_id: Optional[int],
) -> Generator[None, None, None]:
    ...
```

- Required kwarg-only args force every caller to declare intent for
  both walls per invocation.
- Independent reset in finally (mirrors `get_tenant_scoped_db`).
- 9/9 unit tests in `tests/db/test_tenant_scope.py`.

---

## 6. Test status post-C4.4

- `tests/db/` pool: **162/162 pass** (was 153/153 at C4.3; +9 from
  `test_tenant_scope.py`).
- Worker-test pool: 23 pass / 1 pre-existing Step-31 drift failure
  (unrelated, flagged for C9 sweep).
- AST validation: all three retrofitted files parse cleanly.

---

## 7. Known gaps + remediation paths

### 7.1 Retention purge under the master flag (BLOCKED ON C6)

The `retention_purge` task cannot run with `rls_tenant_context_enabled=True`
until C6 wires a dedicated PostgreSQL role with `BYPASSRLS` for
admin/ops paths. A runtime guard in `app/worker/tasks/retention.py`
refuses to run and logs a BLOCKED error when this combination is
detected, preventing inconsistent purges from shipping.

### 7.2 `messages` table (DEFERRED TO C8)

`messages` has no `tenant_id` column today (per C3 deferral). When C8
adds it, the same C3 strict shape applies, and the worker retrofit
already covers the binding path that writes messages.

### 7.3 BYPASSRLS role wiring (C6)

C6 will:
- Create a dedicated PG role `luciel_ops` with `BYPASSRLS`.
- Migrate retention + audit-log immutability paths to connect as that
  role via a separate engine.
- Document the role's access surface in the runbook.

After C6, the C4.4 retention guard can be relaxed to a positive
assertion that the ops engine is in use, not a flag check.

---

## 8. Sign-off

This audit completes the C4 commit envelope (C4.1 → C4.2 → C4.3 →
C4.4). All identified gaps that are NOT explicitly blocked on later
arcs (C6, C8) are fixed in this same commit. Deferred work is
recorded with the responsible arc and a guard that prevents incorrect
behaviour under the master flag.

**Auditor:** AI partner.
**Sign-off date:** 2026-05-24.
**Refs:** ARC9_RUNBOOK §C4, `_arc9/C1_audit_findings.md`.
