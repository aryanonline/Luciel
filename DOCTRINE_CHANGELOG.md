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
