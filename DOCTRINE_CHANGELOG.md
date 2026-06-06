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
