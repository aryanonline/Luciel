# Arc 15 Drift-Cleanup Report

Platform-wide doctrine-alignment cleanup on branch `arc15-backend` (PR #132).
Removes three categories of stale "drift" that diverge from ratified Vision v1 /
Architecture v1. Every removal below is tied to a specific doc ruling or proven
dead with zero live readers. Suite stays green at the baseline
(**2333 passed, 61 skipped, 0 failed**); the new migration round-trips cleanly.

---

## Drift 1 — `system_prompt_additions` raw-prompt vestige

**Doc ruling:** Vision §3.5 / Architecture §3.5.1 — "the system prompt is NEVER
written by the customer … does not expose hooks for the admin to author
additional system-prompt stanzas." The structured config pillars
(`personality_preset`, `personality_axes`, `business_context`, `lead_routing`,
`escalation_config`) plus the platform-composed PRESET + BUSINESS_CONTEXT stanzas
(`app/persona/composer.py`, wired Arc 15 WU2) replace free-text prompt authoring.
The column was already runtime-dead (composer no longer reads it, the personality
API never writes it).

**Files touched:**

- `alembic/versions/arc15_c_drop_system_prompt_additions.py` *(NEW)* — drops
  `instances.system_prompt_additions`; chains off `arc15_b_instance_connections`.
  `downgrade` re-adds `TEXT NULL` (matches original `arc9_c17` shape) so the pair
  round-trips. Verified: upgrade drops, downgrade re-adds `text/YES`, re-upgrade drops.
- `app/models/instance.py` — removed the `system_prompt_additions` Mapped column +
  deprecation comment block.
- `app/schemas/instance.py` — removed the field from `InstanceCreate/Update/Read`.
- `app/schemas/admin.py` — removed the field from `TenantConfigCreate/Update`
  (the legacy translation layer) + docstring line.
- `app/schemas/onboarding.py` — removed the request field + docstring.
- `app/schemas/personality.py` — (docstring already affirms "no system_prompt_additions").
- `app/repositories/instance_repository.py` — removed `create()` param, kwarg,
  `system_prompt_additions_set` audit field, and the `_UPDATABLE_FIELDS` entry.
- `app/services/instance_service.py` — removed `create_instance()` param + passthrough.
- `app/services/onboarding_service.py` — removed param + passthrough.
- `app/services/admin_service.py` — removed from `create_tenant_config` drop-list
  tuple + translation-table docstring line.
- `app/api/v1/admin.py` — removed onboard + create-instance route passthroughs.
- `app/persona/luciel_core.py` — removed the `agent_prompt` persona layer (the
  `system_prompt_additions` carrier) from `build_system_prompt()`; renumbered layers
  (1 Core, 2 PRESET, 3 BUSINESS_CONTEXT, 4 knowledge, 5 memories, 6 tools).
- `app/services/chat_service.py` — removed the `instance_prompt` field from
  `LucielContext` (the dead carrier) and the `agent_prompt=` callsite args.
- `app/api/v1/audit_log.py`, `app/services/tier_provisioning_service.py` — updated
  stale comments referencing the dropped column.

**Tests adjusted (assert the behaviour is GONE):**

- `tests/api/test_arc15_instance_config_routes.py` — inverted
  `..._keeps_system_prompt_additions_deprecated...` →
  `test_model_drops_system_prompt_additions` (asserts absent from the model).
- `tests/db/test_arc12b_custom_roles_migration.py` — head-pin bumped to
  `arc15_c_drop_system_prompt_additions`.
- `tests/services/test_arc12_wu7_cognition.py` — `LucielContext` test now forbids
  `instance_prompt` and asserts the composed `preset_stanza` /
  `business_context_stanza` fields are present instead.
- `scripts/arc11_close_audit.py` — `expected_head` pin bumped to the new head
  (the script's own comment mandates "each arc that adds a migration bumps it").

---

## Drift 2 — Legacy Domain-layer vestiges

**Doc ruling:** Arc 5 Path A — "no Domain layer." The Admin → Instance → Lead
hierarchy has no Domain (or Agent) layer; `DomainConfig` and its table were dropped
at `arc5_c_admin_instance_subtractive` (no live model/table remains).

**Files touched:**

- `app/schemas/onboarding.py` — removed `default_domain_id` /
  `default_domain_display_name` / `default_domain_description` request params;
  deleted the `OnboardedDomainSummary` class; removed the always-`None`
  `default_domain` response field; updated docstrings.
- `app/services/onboarding_service.py` — removed the `default_domain_*` params, the
  `domain = None` stub, the `"default_domain": domain` return key, and the vestigial
  `"allowed_domains": [default_domain_id]` audit line (seeded only from the dead
  default-domain id, never a real `allowed_domains` config).
- `app/api/v1/admin.py` — removed the `OnboardedDomainSummary` import, the onboard
  route `default_domain_*` passthroughs, the `domain` variable, and the
  `default_domain=` response wiring.
- `app/persona/luciel_core.py` / `app/services/chat_service.py` — removed the
  always-`None` `tenant_prompt` / `domain_prompt` persona layers (the V1
  Domain/Agent prompt scaffold) from `build_system_prompt()` and its callsites.

Verified the two live `onboard_tenant` callers (`billing.py`,
`billing_webhook_service.py`) pass none of the removed params.

---

## Drift 3 — Legacy class aliases `LucielInstance{Create,Read,Update,Summary}`

**Doc ruling:** Arc 5 B1 — "new code must never import the legacy names." The
canonical schema names are `Instance{Create,Read,Update,Summary}`.

**Files touched:**

- `app/schemas/instance.py` — deleted the alias block
  (`LucielInstanceCreate = InstanceCreate`, etc.).
- `app/api/v1/admin.py` — switched the schema import to the canonical
  `InstanceCreate/Read/Update` and replaced every `LucielInstance*` schema usage.
- `tests/api/test_instance_lifecycle_arc11_closeout.py` — string assertions updated
  to the canonical name.

Confirmed zero remaining `LucielInstance{Create,Read,Update,Summary}` references
repo-wide.

---

## KEPT — proven NOT drift (left untouched, per the DO-NOT-TOUCH list)

- **`platform_admin` role + all `is_platform_admin` bypasses** — internal-only,
  guarded at `admin.py`; an intended capability, not drift.
- **`allowed_domains`** — live widget-CORS feature; retained on
  `TenantConfigCreate/Update`. Only the *vestigial seed* of it from the dead
  default-domain id was removed (Drift 2).
- **Applied Alembic migrations** — treated as immutable; no applied migration was
  edited. The column drop is a NEW additive migration (`arc15_c`). In particular
  `arc15_a_instance_config_pillars` intentionally LEFT the column in place
  (deprecation, not removal) and was not touched.
- **Four isolation walls / RLS / four locked roles** — untouched.
- **`tenant_id` / `legacy_tenant_id` columns** — left in place (flagged below).
- **`Instance as LucielInstance` ORM alias** (`app/api/v1/admin.py`) — this is a
  separate *model* alias, not one of the Arc 5 B1 *schema* aliases; out of scope,
  kept.
- **`app/services/scope_prompt_preflight.py`** — a LIVE no-op shim still imported by
  `scripts/mint_embed_key.py`. NOT dead; only its stale docstring (referencing
  `domain_configs` / `system_prompt_additions`) was cleaned. Shim kept.
- **Historical comments/docstrings** across `dashboard_service.py`,
  `admin_service.py`, `verification.py`, `chunker.py`, `user_invite.py`,
  `trace.py`, `models/__init__.py`, `config_repository.py` — accurate records of
  past removals (Arc 5 Path A etc.); kept as load-bearing context, not live code.

## Flagged — drift-adjacent but NOT safely removable now

- **`tenant_id` / `legacy_tenant_id`** — on the DO-NOT-TOUCH list and not provably
  dead (potential readers outside this slice). Left in place for a dedicated future
  sweep with a full reader audit. Not removed here.

---

## Verification

- `alembic upgrade head` → column dropped; `alembic downgrade -1` → column re-added
  as `text NULL`; `alembic upgrade head` → dropped again. Clean round-trip; DB head
  is `arc15_c_drop_system_prompt_additions`.
- `python -m pytest tests/` → **2333 passed, 61 skipped, 0 failed** (baseline held).
