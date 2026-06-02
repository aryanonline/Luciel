# ARC 15 Backend — Implementation Report

Branch: `arc15-backend`. Spec: `ARC15_BACKEND_SPEC.md`.

Scope delivered: WU1 (instance config pillars), WU2 (persona composer), WU3
(personality + escalation-contact admin APIs), WU4 (Arc 17 connection-contract
slice), WU5 (connection dispatch gate). All five work units are committed.

## Honesty invariants (held)

* No endpoint returns `connected` for a connection with no real backing. Only
  `property_source` (CSV) and `outbound_webhook` connect LIVE; everything else
  rounds-trips as `unconfigured` with an `arc17_pending` marker.
* No raw-prompt-authoring hook is exposed: the personality surface is preset +
  four bounded axes + framed `business_context` only. There is no
  `system_prompt_additions` request field anywhere.
* No escalation-trigger configuration is accepted. The escalation API governs
  CONTACT + ROUTING only; the four escalation signals are fixed runtime
  cognition, returned read-only.
* `config_json` carries non-secret config only — a boundary guard rejects
  secret-looking keys (422 `secret_in_config_json`); `credential_ref` stays NULL
  in this slice.

---

## Migrations

### `arc15_a_instance_config_pillars` (WU1)
down_revision `arc14_u4_leads`. Adds to `instances`:

| column | type | notes |
|---|---|---|
| `personality_preset` | PG enum `personality_preset` (5 values) | NOT NULL default `warm_concierge` |
| `personality_axes` | JSONB nullable | `{tone, verbosity, formality, pace}` when `custom` |
| `business_context` | Text nullable | tier-capped at the Pydantic layer |
| `escalation_config` | JSONB nullable | contact + routing only (WU3) |

Downgrade drops the columns + the `personality_preset` enum. Data-safe (narrows).

### `arc15_b_instance_connections` (WU4)
down_revision `arc15_a_instance_config_pillars`. Creates the real §3.8.2
`instance_connections` table.

* Two PG enums: `connection_type` (`calendar, email_sender, sms_sender, crm,
  property_source, outbound_webhook`) and `connection_status` (`unconfigured,
  connected, error, expired`), both `create_type=False` + explicit `.create()`.
* Columns: `id, admin_id, instance_id, connection_type, provider, config_json
  (JSONB, NON-SECRET), credential_ref, status, last_verified_at, created_at,
  updated_at, revoked_at`.
* Partial unique index `uq_instance_connections_active` over
  `(admin_id, instance_id, connection_type, provider) WHERE revoked_at IS NULL`.
* RLS: ENABLE + FORCE + PERMISSIVE policy on
  `current_setting('app.admin_id', true)` with USING + WITH CHECK (fail-closed),
  mirroring the arc12_wu2 posture.
* Downgrade reverses RLS, drops the table, drops both enums.

No live `alembic upgrade head` was run — Postgres is unavailable in this
environment (see Deferred gaps). Local mitigation: the migration-shape AST/text
test `tests/db/test_arc15_b_instance_connections_migration_shape.py` pins
revision chaining, enum vocabularies, all columns, the partial-unique index, the
RLS posture, and downgrade reversal.

---

## Endpoints

### Personality (WU3) — mount `/api/v1/admin/instances/{instance_id}/personality`

**GET ``** → `200 PersonalityConfigResponse`
```json
{
  "instance_id": 1, "admin_id": "a", "admin_tier": "pro",
  "custom_preset_available": true, "business_context_max_chars": 2000,
  "personality_preset": "warm_concierge",
  "personality_axes": null,
  "business_context": null,
  "updated_at": "2026-06-02T00:00:00Z"
}
```

**PUT ``** body `PersonalityConfigUpdate` (extra=forbid):
```json
{ "personality_preset": "custom",
  "personality_axes": {"tone":"warm","verbosity":"balanced","formality":"casual","pace":"relaxed"},
  "business_context": "..." }
```
→ `200 PersonalityConfigResponse`. `custom` on Free → 403; axes only valid when
preset==`custom` (422 otherwise); `business_context` over tier cap → 422.

### Escalation contact (WU3) — mount `/api/v1/admin/instances/{instance_id}/escalation`

**GET ``** → `200 EscalationConfigResponse`
```json
{
  "instance_id": 1, "admin_id": "a", "admin_tier": "enterprise",
  "available_notify_channels": ["email","sms","slack","custom"],
  "escalation_signals": ["explicit_human_request","cannot_confidently_answer",
                         "strong_negative_sentiment","high_value_lead"],
  "escalation_config": { "primary_email": "ops@x.com" },
  "updated_at": "2026-06-02T00:00:00Z"
}
```

**PUT ``** body `EscalationConfigUpdate` (extra=forbid): `{ "config": { ... } }`
→ `200 EscalationConfigResponse`. The `config` object is validated by
`app.policy.escalation_config.validate_escalation_config_for_tier` once the tier
is resolved; any escalation-TRIGGER key is rejected (never silently dropped).

### Connections (WU4) — mount `/api/v1/admin`, four-walls + `PERM_CONFIGURE_CONNECTIONS`

**GET `/instances/{instance_id}/connections`** → `200 ConnectionListResponse`
```json
{ "instance_id": 1, "admin_id": "a", "connections": [ ConnectionView, ... ] }
```

**POST `/instances/{instance_id}/connections`** body `ConnectionCreate`:
```json
{ "connection_type": "property_source", "provider": "csv",
  "config_json": {"store_ref": "s3://bucket/listings.csv"} }
```
→ `201 ConnectionCreateResponse`. Honesty fork:
* LIVE (`property_source`, `outbound_webhook`) → row `status="connected"`,
  `arc17_pending=null`.
* DEFERRED (`calendar`, `crm`, `email_sender`, `sms_sender`) → row
  `status="unconfigured"` + `arc17_pending`:
  ```json
  { "deferred": true, "connection_type": "calendar",
    "available_in": "arc17", "message": "..." }
  ```
Required `config_json` keys per type: `property_source`→`store_ref`,
`outbound_webhook`→`url` (422 `config_json_missing_required_keys` if absent;
422 `secret_in_config_json` if a secret-looking key is present).

`ConnectionView`:
```json
{ "id": 5, "instance_id": 1, "admin_id": "a",
  "connection_type": "property_source", "provider": "csv",
  "status": "connected", "config_json": {"store_ref":"..."},
  "last_verified_at": "2026-06-02T00:00:00Z",
  "created_at": "...", "updated_at": "..." }
```

**DELETE `/connections/{connection_id}`** → `200 ConnectionDeleteResponse`
```json
{ "instance_id": 1, "connection_id": 5, "disconnected": true }
```
Load fenced to the admin via `get_live_for_admin` (Wall-1; 404 if not theirs),
then soft-delete (`revoked_at`). Every configure/disconnect is audited
(`ACTION_CONNECTION_CONFIGURED` / `ACTION_CONNECTION_DISCONNECTED`,
`RESOURCE_INSTANCE_CONNECTION`), recording config **keys only**, never values.

### Tools (WU4 change) — `GET /api/v1/admin/instances/{instance_id}/tools`

`ToolView` gains `connection_type: str | None` and `connection_status`. The list
route loads live status via `InstanceConnectionRepository.live_status_by_type`
and threads it into each view.

---

## Preset → axis map (WU2, `app/persona/presets.py`)

Four axes: `tone, verbosity, formality, pace`.

| preset | tone | verbosity | formality | pace |
|---|---|---|---|---|
| `warm_concierge` | warm | balanced | casual | relaxed |
| `professional_advisor` | neutral | balanced | professional | measured |
| `friendly_expert` | enthusiastic | detailed | casual | measured |
| `trusted_authority` | authoritative | concise | formal | brisk |
| `custom` | (admin-supplied `personality_axes`; falls back to `warm_concierge` axes if unset) | | | |

The composer renders platform-controlled PRESET and BUSINESS_CONTEXT stanzas
from these axes; admins never author raw prompt text.

---

## connection_status mapping (WU4, `admin_tools._connection_status_for`)

The ToolView chip is derived purely from `requires_connection` + the live row
status:

| `requires_connection` | live status | `connection_status` |
|---|---|---|
| None | (any) | `null` (no chip) |
| set | no row | `action_needed` |
| set | `unconfigured` | `action_needed` |
| set | `connected` | `connected` |
| set | `error` | `reconnect_needed` |
| set | `expired` | `reconnect_needed` |

The six connection-bearing tools declare `requires_connection`:
`book_appointment`→`calendar`, `send_email`→`email_sender`,
`send_sms`→`sms_sender`, `lookup_property`→`property_source`,
`push_to_crm`→`crm`, `bring_your_own_webhook`→`outbound_webhook`.
(`schedule_callback`, `call_sibling_luciel` inherit `None` → no chip, no gate.)

---

## Escalation-config shape (WU3, `app.policy.escalation_config`)

Fixed runtime signals (read-only, never configurable):
`explicit_human_request`, `cannot_confidently_answer`,
`strong_negative_sentiment`, `high_value_lead`.

Tier-conditional CONTACT + ROUTING surface:

| tier | notify channels | secondary contact | chains |
|---|---|---|---|
| free | `email` | no | no |
| pro | `email, sms` | yes | no |
| enterprise | `email, sms, slack, custom` | yes | yes |

Free accepts `primary_email`; Pro adds `primary_contact` / `secondary_contact` /
`routing_rules`; Enterprise adds `chains`. Any trigger-config key is rejected.

---

## WU5 — connection dispatch gate (`app/tools/authorization.py`)

`DefaultDenyToolAuthorizer.authorize` runs four gates in order:
`_check_row → _check_tier → _check_channels → _check_connection`.

`_check_connection`: a tool declaring `requires_connection` needs a live
`instance_connections` row with `status=='connected'`; otherwise it is refused
with the same structured shape as a default-deny
(`failure_kind="connection_not_configured"`) — never a silent failure.
`requires_connection is None` skips the gate. A connection-bearing tool with no
reachable DB session is refused (load-bearing), never silently allowed.

| state | result |
|---|---|
| `connected` | allow |
| `unconfigured` / `expired` / `error` / no row | deny, `connection_not_configured` |
| `requires_connection is None` | skip (allow) even with no session |
| `requires_connection` set, no session | deny (load-bearing) |

---

## Tests

WU4/WU5 + impacted suites: 58 passed
(`test_arc15_wu5_connection_gate`, `test_arc15_wu4_connections_routes`,
`test_arc15_wu4_connection_status_mapping`,
`test_arc15_b_instance_connections_migration_shape`,
`test_arc12_wu2b_admin_tools_routes`).

Broad regression (`tests/tools tests/api tests/policy tests/db`, excluding two
psycopg2-blocked ops files): **1546 passed, 75 skipped, 0 failed**.

Test env: `DATABASE_URL=sqlite:///:memory: OPENAI_API_KEY=… ANTHROPIC_API_KEY=…
MODERATION_PROVIDER=null`. Route tests are AST/text shape tests; behavioural
tests run against in-memory SQLite with a private MetaData (Postgres-flavoured
migrations are not applied to SQLite).

---

## Deferred gaps (with rationale)

* **Full Arc 17 (OAuth, secret/credential storage, connection-health worker)** —
  out of scope for this contract slice. `credential_ref` is present in the
  schema but stays NULL; only CSV/webhook connect live. Deferred connectors land
  `unconfigured` + `arc17_pending` rather than faking `connected`.
* **Native CRM / calendar / email / SMS connectors** — deferred to Arc 17; the
  WU5 gate keeps the dependent tools refused until a real backing exists.
* **Live `alembic upgrade head`** — Postgres is unavailable in this environment;
  the migration is Postgres-flavoured (enum types, partial unique index, RLS),
  so it is not exercised against SQLite. Mitigation: the migration-shape AST/text
  test pins the full structure + RLS posture; the live upgrade is to be run in
  CI/staging.
* **`tests/db/test_c6_3_ops_session.py` (7) + `test_c6_4_ops_role_behavioural.py`** —
  pre-existing infra failures (`ModuleNotFoundError: No module named 'psycopg2'`);
  the ops DB engine needs the Postgres driver. Unrelated to ARC 15; excluded from
  the regression run.
