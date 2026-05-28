# ARC 12 — Work Units (dependency-ordered)

Verified repo facts (confirmed by founder before delegation — trust but verify against live tree):
- `app/tools/base.py` — `LucielTool(ABC)` with `name`, `description`, `parameter_schema`, sync `execute(**kwargs)->ToolResult`, `declared_tier: ActionTier|None`. **NOT the §3.3.1 contract.**
- `app/tools/registry.py` — `ToolRegistry` registers `SaveMemoryTool`, `SessionSummaryTool`, `EscalateTool` (all cognition — must be evicted).
- `app/tools/broker.py` — `ToolBroker(registry, classifier=None)`; `execute_tool()` runs the action-classification gate then dispatches. **No authorization check yet.** Keep the classification gate.
- `app/tools/implementations/` — the 3 cognition tools live here.
- `app/services/chat_service.py` (746 lines) — the LIVE widget chat path. Calls `tool_registry.get_tool_descriptions(allowed=ctx.allowed_tools)` + `tool_broker.parse_and_execute(...)`, detects which tool fired via substring match on `raw_reply`. Carries `LucielContext` with `tenant_prompt/domain_prompt/agent_prompt/instance_prompt`, `_resolve_luciel_context(... domain_id, agent_id ...)`, `_compose_system_prompt_additions`. `respond()` and `respond_stream()` both thread `domain_id`/`agent_id`. **This is superseded v1 three-layer scaffold + a shadow loop.**
- `app/runtime/orchestrator.py` — `LucielOrchestrator.run` is an Arc-11 stub (retrieve + trace only). Does NOT call the broker. Leave it a stub — Arc 14 owns it. Do not grow it.
- `app/policy/scope.py` — `ScopePolicy` with `enforce_admin_owns_instance`, `enforce_role_on_instance(allowed_roles=...)`, `_resolve_role_on_instance`, four `ScopeRole` enum members (ADMIN_OWNER, ADMIN_MANAGER, INSTANCE_OPERATOR, READ_ONLY_VIEWER). **This is the Wall-2 spine for sibling grant authoring — reuse it, do not reinvent.**
- `app/policy/entitlements.py` — `TierEntitlement` frozen dataclass + `TIER_ENTITLEMENTS` map (free/pro/enterprise). Has `composition_enabled`, `max_composition_depth`, `webhook_outbound_enabled`, `knowledge_share_grants_enabled`, `cross_instance_memory_federation`. `max_composition_depth` is consumed NOWHERE (dead). `resolve_entitlement(tier, axis, overrides)` is the lookup. Dataclass is frozen — removing a field touches every constructor call-site + the field list.
- `app/models/admin_audit_log.py` — `admin_audit_logs` table: `action` (String(64), validated against `ALLOWED_ACTIONS` set), `resource_type`, `resource_pk`, `resource_natural_id`, `before_json`/`after_json` (JSONB), `actor_*`, `admin_id`, `luciel_instance_id`, append-only `row_hash`/`prev_row_hash` chain. **Sibling grant author/approve/revoke audit rides this — add new ACTION_* verbs to the allow-list.**
- `app/models/instance.py` — `instances` table: `id` (int PK), `admin_id` (str), `instance_slug`, `display_name`, `active`, `instance_status`, `soft_deleted_at`, etc. Instance PK is **int**. (Note: `instances` legacy `allowed_tools` attr is referenced via getattr in chat_service — superseded by per-instance tool authorization built in WU2.)
- `app/api/deps.py` — module-level `_tool_registry = ToolRegistry()`, `_tool_broker = ToolBroker(registry=_tool_registry)`, injected into `ChatService`.
- Single Alembic head: `arc11_closeout_b_ingestion_error_code`. Single `main` branch. Chain all new migrations off the head (and off each other in WU order).

---

## WU1 — Foundation: §3.3.1 contract + entitlement cleanup
1. **Retire `max_composition_depth`** from `TierEntitlement` and every constructor in `TIER_ENTITLEMENTS`, plus any doc-comment axis numbering. Grep the whole repo for `max_composition_depth` and remove all reads/tests. Keep `composition_enabled` (the §3.3.4 master switch). If a Stripe/tier-provisioning path or test references it, fix that too — non-deferrable.
2. **Migrate `LucielTool` to the §3.3.1 contract.** New required surface on the base:
   - `tool_id: str`, `display_name: str`, `description: str`
   - `input_schema: dict` (JSON Schema, validated before execute), `output_schema: dict` (JSON Schema, validated after execute)
   - `requires_tier: tuple[str, ...]` (subset of `("free","pro","enterprise")`)
   - `requires_channels: frozenset[str]` (e.g. `send_sms` → `{"sms"}`; most → `frozenset()`)
   - `execution_mode: str` — `"in_process"` | `"subprocess"`
   - `async def execute(self, input: dict, context: ToolContext) -> dict` — `context` carries at minimum `admin_id`, `instance_id`, and a DB-session/scope handle for scoping. Define a `ToolContext` dataclass in `app/tools/base.py` (or `app/tools/context.py`).
   - Keep `declared_tier: ActionTier|None` (the action-classification gate still reads it — orthogonal to authorization).
   - Keep `ToolResult` for internal broker plumbing if needed, but `execute()` returns the schema-validated `dict` per the contract. Reconcile the two cleanly (broker may wrap the dict into a ToolResult for the classification metadata, or ToolResult is retired in favor of dict — choose the path that keeps the classification gate intact and the contract literal; document the choice).
   - Provide input/output JSON-Schema validation helpers the broker and BYO sandbox both use (jsonschema lib if already a dep; otherwise a minimal validator — check pyproject first).
3. Update the action-classification gate / broker imports to the new contract WITHOUT removing the gate.
4. Tests: contract-shape test asserting every registered tool declares all §3.3.1 fields; entitlement test asserting `max_composition_depth` is gone and `composition_enabled` remains per-tier (free=False, pro=True, enterprise=True).

## WU2 — Per-instance tool authorization (the broker, default-deny)
1. New table `instance_tool_authorizations` (hand-written migration off WU1's head):
   - `id` PK; `admin_id` (str, non-null, indexed — Wall 1); `instance_id` (int FK→instances.id, non-null, indexed — Wall 3); `tool_id` (str, non-null); `enabled` (bool, default true on insert — a row means authorized); `authorized_by_user_id`; `created_at`; `updated_at`; soft-delete/`revoked_at` per §5.5 soft-delete-by-default.
   - Unique constraint on `(admin_id, instance_id, tool_id)` over non-revoked rows.
   - RLS policy per §3.7.5 (USING + WITH CHECK on `admin_id = current_setting('app.admin_id', true)`). Enable RLS table-on.
2. Repository + service for CRUD, scoped by `(admin_id, instance_id)`.
3. **Broker authorization at dispatch** — in `ToolBroker.execute_tool` (and `parse_and_execute`), BEFORE the classification gate runs `execute()`: look up `(admin_id, instance_id, tool_id)` authorization. **Default-deny: absent row ⇒ refuse with a structured tool-error.** The broker must receive `admin_id` + `instance_id` (thread via the `ToolContext`/call context). This interface MUST be stable for Arc 14 — document the signature.
4. Tests: default-deny (no row ⇒ denied); authorized row ⇒ passes to classification; Wall-1 (admin A's row can't authorize admin B's instance); Wall-3 (instance scoping).

## WU3 — v1 tool catalog (the 8 tools, §3.3.2)
Register exactly these 8 configurable tools (and NOTHING cognition-shaped):
`book_appointment`, `send_email`, `send_sms`, `lookup_property`, `schedule_callback`, `push_to_crm`, `call_sibling_luciel`, `bring_your_own_webhook`.
- All are `requires_tier=("pro","enterprise")` per §3.3.2 (NOT free). `call_sibling_luciel` not available on Free (Decision: master switch + grants; Free has no composition).
- `send_email` `requires_channels={"email"}`, `send_sms` `requires_channels={"sms"}`. Broker denies if channel not enabled (channel adapters are Arc 13 — this is correct default-deny, not a gap).
- `execution_mode`: all `"in_process"` EXCEPT `bring_your_own_webhook` = `"subprocess"` (Decision #5). `call_sibling_luciel` is `"in_process"`.
- Apply the **interim-body rule** (00_MASTER §"interim-body rule"): `send_email`/`send_sms` bodies return structured "channel adapter ships Arc 13" with `TODO(ARC13)`; implement the rest for real to the extent dependencies exist, else interim-body with the correct arc anchor + flag. `call_sibling_luciel` and `bring_your_own_webhook` are implemented in WU5/WU6 respectively — register the contract here, wire the body there.
- Each tool: full §3.3.1 contract, input/output JSON Schema, one-sentence `description`, admin-facing `display_name`.
- Tests: every catalog tool present, correct tier/channels/execution_mode, schema validates representative I/O.

## WU4 — sibling_call_grants table + grant-authoring API + approval workflow (§3.3.4)
1. Table `sibling_call_grants` — EXACT columns from §3.3.4: `admin_id` (non-null, indexed), `caller_instance_id`, `callee_instance_id`, `granted_by_user_id`, `granted_at`, `approval_state` (`live`|`pending_approval`|`revoked`), `approved_by_user_id` (nullable), `approved_at` (nullable), `revoked_at` (nullable). Plus `id` PK, timestamps.
   - **Composite index** on `(admin_id, caller_instance_id)` (runtime dispatch lookup).
   - **Unique constraint** on `(admin_id, caller_instance_id, callee_instance_id)` over rows WHERE `approval_state != 'revoked'` (partial unique index).
   - RLS per §3.7.5; `instance_id` columns FK→instances.
   - Hand-written migration off WU3 head.
2. Grant-authoring API (`/api/v1/admin/...`): author / list / approve / reject / revoke grants.
   - **Wall-2 scope rule (the load-bearing security property):** the author must have role+scope on BOTH caller and callee instances. Reuse `ScopePolicy.enforce_role_on_instance` against BOTH instances. A user scoped to one instance CANNOT author a cross-instance grant — 403. This is Wall 2 holding at the sibling layer.
   - **Approval workflow:** on Enterprise, author ⇒ `pending_approval`; an `admin_owner` approves ⇒ `live`. On Pro ⇒ `live` immediately on author. On Free ⇒ `call_sibling_luciel` unavailable, reject grant authoring. Tier comes from the admin's subscription/entitlements.
   - **Audit:** every author / approve / reject / revoke writes an `admin_audit_log` row (add ACTION_* verbs to the allow-list). Use `before_json`/`after_json`.
   - Deactivation alignment (§3.6.1 step 3): when an instance is deactivated it already revokes grants where it is caller OR callee — verify that cascade still holds with the real table (Arc 10 wrote it against a planned table; wire it to the real one now, non-deferrable).
3. Tests: scope-on-both-endpoints enforced; Pro immediate-live; Enterprise pending→approve; Free rejected; unique-constraint (no double live edge); audit rows written; revoke flips state + sets `revoked_at`.

## WU5 — Sibling runtime dispatch + guardrails (§3.3.4)
Implement `call_sibling_luciel`'s body + the dispatch path in the broker/runtime:
1. On invoke `call_sibling_luciel(target_instance)`, in order:
   a. **Cycle detection** — track `(caller_instance_id, callee_instance_id)` pairs per inbound message; a call that revisits an instance already in the call stack is rejected and returned as a tool-error the calling Luciel can reason about.
   b. **Per-inbound fan-out budget** — max total sibling-call invocations across the whole composition tree per inbound message (cost-control, parallel to the 5-iteration loop bound). Default sized so depth 2–3 / fan-out 2–3 is unconstrained. **Runtime-internal, NOT admin-configurable, NOT in UI.** Put the default in config/constants, not entitlements.
   c. **Master switch** — `call_sibling_luciel` enabled (authorized) on BOTH caller and callee instances (per-instance tool authorization from WU2).
   d. **Grant lookup** — `sibling_call_grants` for `(admin_id, caller_instance_id, callee_instance_id)` with `approval_state='live'`. No row ⇒ tool-error "sibling target not granted".
   e. On all passing ⇒ dispatch with a derived context naming BOTH instances; emit a sibling-access audit row (Wall-3 composition exception, §3.7.3) + tool-execution-log row with the chain.
2. **No depth limit, no edge cap** (Decision #19) — guardrails are ONLY cycle detection + fan-out budget. Do not add depth/edge caps anywhere.
3. Tests: cycle rejected; fan-out budget stops cascade; master-switch-off on either side ⇒ denied; no live grant ⇒ denied; happy path dispatches with both-instance context + audit; A→B grant does not imply B→A.

## WU6 — BYO webhook subprocess sandbox (§3.3.5, Decision #5/#6)
Implement `bring_your_own_webhook` with the FULL security envelope — `execution_mode="subprocess"`:
- **Subprocess isolation:** one process per invocation, no shared state with the worker. A hung webhook is killed without losing the worker.
- **Hard 30s timeout** — subprocess killed at the boundary.
- **Input JSON Schema validated before dispatch** (admin registers schema at tool config time); **output JSON Schema validated after** — malformed output dropped, logged, treated as tool failure.
- **Retry:** 2 retries, exponential backoff (initial 500ms, max 5s), **transport errors only — NEVER on schema-validation failure.**
- **Per-endpoint circuit breaker:** open after 5 consecutive failures in a 60s window; half-open after 60s; close on first success. State keyed per registered endpoint (persist in Redis or DB — Redis is the broker/cache per §4.1; choose and document).
- **Restricted egress allowlist** by domain registered at tool config time. The subprocess must not reach arbitrary hosts.
- **Audit row per invocation** (tool execution log): `execution_mode`, input hash, output hash, latency, error class, circuit-breaker state at dispatch.
- Config: BYO endpoint + input/output schema + allowlisted domain registered per instance (a small table or extend tool-authorization config payload — choose, document, migrate).
- Tests: timeout kills subprocess; output-schema failure ⇒ tool failure + NO retry; transport error ⇒ retried with backoff; circuit opens after 5 fails / half-opens / closes on success; egress to non-allowlisted domain blocked; audit row shape complete.

## WU7 — chat_service alignment sweep + cognition relocation (founder rulings 4 & 5)
1. **Create `app/cognition/` (or `app/runtime/cognition/`) interim module.** Header comment + `TODO(ARC14)` marking it the temporary host until `LucielOrchestrator.run` subsumes it (ruling 4a). Move escalate / save_memory / summarize behavior **as-is** (ruling 4b) — minimal, called DIRECTLY by chat_service, NOT via broker/registry, NOT tier-gated (ruling 4c). Reuse `EscalationService` for the escalate path.
2. **Evict** `SaveMemoryTool`, `SessionSummaryTool`, `EscalateTool` from `registry.py` `_register_defaults` and delete the three implementation files (their behavior now lives in the cognition module). Registry holds ONLY the 8 configurable tools.
3. **Strip superseded Domain/Agent scaffold from `chat_service.py`:** remove `domain_id`/`agent_id` threading, `_resolve_luciel_context`'s domain/agent resolution, `_compose_system_prompt_additions`, and the `tenant_prompt/domain_prompt/agent_prompt` layers in `LucielContext`. Collapse to the single Admin→Instance boundary (§3.7.2). Keep instance persona/`system_prompt_additions`, knowledge retrieval, this-session history (Wall 4), LLM call.
4. **Remove substring tool-detection** (`"escalate_to_human" in raw_reply` etc.) as the dispatch mechanism. The 8 catalog tools dispatch through the broker (default-deny per instance from WU2) — in practice nothing dispatches until an admin authorizes a tool, which is correct for Arc 12. Cognition (escalate/save_memory/summarize) runs through the cognition module, not substring matching.
5. Update `app/api/deps.py` and any callers/tests for the new `ChatService` shape. Fix the `instances.allowed_tools` getattr fallback — superseded by per-instance tool authorization (WU2).
6. Tests: chat path still answers; cognition (escalate/lead-capture/summarize) still fires and is behavior-equivalent to before (ruling 4b); NO domain/agent code remains (grep clean); NO substring dispatch remains; registry contains only the 8 tools.

## WU8 — Full suite + alignment pass
- `pytest` green across the repo. Fix anything the sweep broke (non-deferrable).
- Grep sweeps to prove removal: `max_composition_depth`, `domain_id`/`agent_id` in chat path, substring tool-detection, the 3 cognition tool classes outside the cognition module.
- Produce `arc12_findings.md` in repo root: what was built, the cognition relocation as a **documented interim deviation** (ruling 6), the larger-than-delta-row chat_service diff (ruling 5), any document gaps/contradictions found for founder review, and the entitlements-vs-Vision-§7 reconciliation.
