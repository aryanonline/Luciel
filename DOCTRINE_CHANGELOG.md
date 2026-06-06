# Doctrine Changelog

In-repo proxy for "the architecture doc was updated" (Architecture §5.9.3).

The ratified Architecture document (§8 Module/Path Doctrine) is canon and
lives in the Space, not in this repo, so CI cannot diff it. Whenever a PR
changes a **doctrine-anchored path** (any `paths` entry in
`DOCTRINE_ANCHORS.toml`), the author MUST add an entry here describing the
change. The reviewer reconciles the entry against the Space doc. The CI
doc-sync gate (`.github/scripts/doctrine_sync_gate.py`) enforces that an
anchored-path change is accompanied by a change to this file.

Newest entries first.

## Unit 13f MOVE 2 — consolidate the behavioral tenant-isolation suite under `tests/isolation/` (2026-06-06)

Resolves the MOVE 2 deferral recorded in the entry below. Per the
**founder ruling**, `tests/isolation/` is defined by **behavioral
purpose — a LIVE cross-tenant non-access gate — NOT by filename and NOT
by the prior 563-test `tests/security/` + `tests/db/` bucket**. Flips the
`isolation_suite` anchor in `DOCTRINE_ANCHORS.toml` from
**ISOLATION-SUITE** (`paths = ["tests/security/", "tests/db/"]`, flagged
for founder ruling) to **MATCHES-DOC** (`paths = ["tests/isolation/"]`).

- **Classification criterion (by assertion mechanism, read per-file, not
  by filename):** a test is behavioral tenant-isolation (category A) only
  if it proves, at runtime against a live Postgres and as a
  **NON-SUPERUSER** role (RLS only bites non-superusers — a superuser
  session proves nothing), that tenant A cannot read/write/observe tenant
  B's rows: FORCE RLS, GUC / `bind_tenant_scope` scoping, cross-tenant
  non-access, RLS fuzz/leak. A test that only regex/parses migration
  **source text** and does not run against a live DB is a
  migration-**contract** test (category B). Everything else — general
  security (JWT, rate-limit), perf, retrieval, log-format, single-tenant
  lifecycle — stays in place (category C).
- **`git mv` into `tests/isolation/` (7 files, 30 tests, all category A):**
  `test_c9_5_live_rls_integration` (ephemeral non-superuser role; GUC
  unset ⇒ 0 rows; GUC=A ⇒ only A; cross-tenant INSERT/UPDATE denied;
  `is_local` GUC no-leak), `test_arc9_ws4b_rls_fuzz` (non-superuser; every
  FORCE-RLS table 0 rows on unset/bogus GUC), `test_arc11_knowledge_rls`
  (non-superuser; Admin B sees no A sources/chunks; JOIN-RLS + write
  denial), `test_rescanc_graph_kb_rls` (non-superuser; B cannot read A's
  graph nodes/edges), `test_unit13d_analytics_isolation`
  (`bind_tenant_scope`; A's analytics exclude B),
  `test_unit13e_session_key_isolation` (cross-tenant session-key never
  bridges), `test_unit13e_session_summary_isolation` (non-superuser; GUC=A
  ⇒ only A's summaries).
- **`git mv` into the new `tests/migrations_contract/` package (29 files,
  all category B — migration-shape/contract, conn-less):** the
  `test_rls_c3_*` / `test_rls_c4_3*` / `test_rls_c5_*` families,
  `test_c5_4_tenant_leak_regression` (the 46-test static-shape net — it
  parses migration SQL, cannot run against a real DB, so it is CONTRACT),
  `test_arc15_a/b_*_migration_shape`,
  `test_rescanc_{escalation_delivery,graph_kb}_migration_shape`,
  `test_rls_admin_audit_logs_migration`, `test_rls_arc12_ex2_*`,
  `test_arc12_ex3_*`, `test_c6_1/c6_2_*_migration`,
  `test_unit13c_auth_class_migration`,
  `test_arc10_audit_archiver_role_privileges` (privilege-grant shape),
  `test_arc11_rls_migrations_shape` (from `tests/security/`).
- **Nothing deleted or weakened.** This is a relabel: the full suite total
  is unchanged at **2834 passed / 0 failed / 36 skipped / 1 xfailed**. The
  labeled isolation count drops from **563 → 30** *by design* — the entire
  delta is migration-shape/non-isolation tests that moved to
  `tests/migrations_contract/` or stayed in place, **not** behavioral
  coverage. Every category-A nodeid that passed under the old bucket is
  present and passing under `tests/isolation/` (before/after collect diff:
  identical, 30 of 30).
- `run_tests.sh`: the behavioral-isolation subset is now `tests/isolation`
  (only); the full-suite path `tests/` is unchanged. The doc-sync gate
  derives its globs from the anchor `paths`, so it now watches
  `tests/isolation/`; `tests/migrations_contract/` is not a §8 doctrine
  path and gets no anchor.

## Unit 13f — §8 path cleanup: move Alembic tree to `app/migrations/` (2026-06-06)

Relocated the Alembic migration tree from `alembic/` to its §8 doctrine
path `app/migrations/` as a **pure config-coupled relocation — zero
behavior change**. Flips the `migrations` anchor in
`DOCTRINE_ANCHORS.toml` from **CONFIG-BOUND-EXCEPTION** (`paths =
["alembic/"]`, "FLAGGED FOR FOUNDER RULING") to **MATCHES-DOC** (`paths =
["app/migrations/"]`).

- `git mv` of `alembic/env.py`, `alembic/script.py.mako`, and every
  `alembic/versions/*.py` into `app/migrations/` — all are 100%-similarity
  renames; no migration SQL was touched. The revision chain is intact,
  single head = `unit13e_session_summaries`; `alembic upgrade head` and a
  single-step downgrade/re-upgrade round-trip both verified from the new
  location. (A pre-existing `downgrade base` FK-ordering bug in the
  historical chain is unrelated to this move and out of scope under the
  zero-behavior-change mandate.)
- `alembic.ini`: `script_location` repointed `alembic` → `app/migrations`.
  `env.py` is location-agnostic (imports `app.core.config.settings`, no
  `__file__` path math), so it needed no edit.
- Functional path references updated: `Dockerfile` (dropped the now
  redundant `COPY alembic/ alembic/`; `COPY app/ app/` carries the tree),
  `.github/workflows/widget-e2e.yml` (`alembic/versions/**` →
  `app/migrations/versions/**` in the PR path filter),
  `widget/README.md`, the `scripts/deploy_30a*.{ps1,sh}` and
  `scripts/mint_*.py` helpers, and ~44 test/script files that construct
  the `versions/` path. `scripts/arc11_close_audit.py` now prunes
  `app/migrations/` from its `Arc-14` AST scan, preserving the prior
  "migrations live outside `app/` so the scan never sees them" invariant.
- Historical prose mentions of `alembic/versions/...` in model docstrings
  and frozen audit reports were intentionally left as-is: they document
  where each table was *born* and are not functional path resolutions.

The companion MOVE 2 (consolidating the tenant-isolation suite under
`tests/isolation/`) was **deferred under the spec's STOP-AND-REPORT
clause**: the `tests/security/` + `tests/db/` files cannot be cleanly
bisected into "genuine tenant-isolation" vs "general-db" without risking
the non-negotiable isolation gate. File names actively mislead (the
`test_rls_c3_*` / `test_rls_c4_3*` / `test_rls_c5_*` families are
migration-*shape* tests that regex the migration source and explicitly
defer live enforcement to the C7/C9.5 suites, despite carrying
`*_instance_isolation` policy names), and the 46-test
`test_c5_4_tenant_leak_regression.py` sits exactly on the
isolation-purpose / shape-mechanism boundary the spec cannot
disambiguate. The `isolation_suite` anchor therefore remains
**ISOLATION-SUITE** at `tests/security/` + `tests/db/` pending a founder
ruling. No isolation test was moved, weakened, or dropped.

## Unit 13d — build the §3.9 Analytics & Reporting subsystem (2026-06-06)

Built the §3.9 Analytics & Reporting subsystem at the §8 doctrine path
`app/analytics/`, flipping the `analytics` anchor in
`DOCTRINE_ANCHORS.toml` from **NO-MODULE-YET** to **MATCHES-DOC** (with
the real `paths = ["app/analytics/"]`).

- `app/analytics/service.py::AnalyticsService` — READ-ONLY aggregate
  metrics over the existing tenant-scoped stores (sessions, leads,
  escalation_events, traces, admin_audit_log, the Redis budget meter).
  Every query is a SELECT of aggregates scoped `WHERE admin_id=:admin_id`
  through the RLS-bound TenantScoped session. NO new tables, NO new write
  path, NO new PII. Metrics: conversations (period/total), leads
  (period/total), escalations-by-signal, escalation→ack first-response
  p50/p95, appointments booked, conversion rate (lead.outcome), channel
  mix, budget utilization (reuses the existing meter), top-N knowledge
  sources, busiest-times heatmap. Tier shape: Free → basic subset; Pro →
  full + CSV export.
- `app/api/v1/analytics.py` — tier-shaped `GET /api/v1/admin/analytics`
  and Pro-only `GET /api/v1/admin/analytics/export` (text/csv). EXTENDS
  (does not replace) `app/api/v1/admin/usage.py`; reuses the budget meter
  for utilization rather than duplicating it. Registered in the API
  aggregator.

Prerequisite write path (lead business-data, NOT analytics data —
analytics stays read-only):

- `leads.outcome` column (nullable, CHECK in converted/lost/in_progress
  or NULL) via migration `unit13d_lead_outcome`
  (down_revision = `unit13c_connection_auth_class`; round-trips).
- `PATCH /api/v1/admin/leads/{id}/outcome` (`app/api/v1/admin_leads.py`)
  — tenant-fenced, enum-validated (422), 404 cross-tenant, writes the new
  `ACTION_LEAD_OUTCOME_SET` audit action.

Isolation: a cross-tenant exclusion test seeds two tenants and asserts
tenant A's analytics counts exclude tenant B's data.

## Unit 12 — normalize code to §8 doctrine paths (2026-06-06)

Moved 8 drifted anchors to their §8 canonical locations (pure relocation,
zero behavior change) and recorded the full map in `DOCTRINE_ANCHORS.toml`:

- `app/integrations/llm/router.py` → `app/runtime/llm_router.py`
- `app/knowledge/retriever.py` → `app/runtime/knowledge_retrieval.py`
- `app/policy/moderation.py` → `app/runtime/input_safety.py`
- `app/api/v1/admin_usage.py` → `app/api/v1/admin/usage.py`
- extracted grounding from `app/runtime/orchestrator.py` → `app/runtime/grounding.py`
- `app/runtime/handoff_ack.py` → `app/runtime/handoff.py`
- Connections Layer model+repo → `app/connections/`
- lifecycle state/closure/retention → `app/lifecycle/`
- `app/services/auth_service.py` → `app/auth/access.py`
- `app/runtime/budget_meter.py` → `app/billing/metering.py`;
  `app/services/overage_billing.py` → `app/billing/overage.py`

Documented non-moves: `alembic/` (CONFIG-BOUND-EXCEPTION for the
`app/migrations/` anchor), analytics (NO-MODULE-YET), and the isolation
suite living in `tests/security/` + `tests/db/` (ISOLATION-SUITE). These
three are flagged in `DOCTRINE_ANCHORS.toml` for founder ruling.

Also added the §5.9.3 CI doc-sync gate and this changelog.
