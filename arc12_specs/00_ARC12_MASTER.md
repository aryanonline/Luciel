# ARC 12 — Master Spec (Tool Registry, BYO Webhook Sandbox, Sibling-Luciel Composition)

Repo: https://github.com/aryanonline/Luciel  •  Work branch: `arc12/tool-registry-sibling-byo` (already created off `main` @ Arc 11 head `d4baf14`). **Commit to this branch. Do NOT open a PR or merge — the founder closes the arc.**

## Source of truth
The three canonical documents are the alignment target. In order of authority: **Vision > Architecture > this spec.** If this spec ever contradicts the documents, the documents win — flag it, do not silently follow the spec. Key sections you must honor: Architecture §3.3 (tool subsystem), §3.3.1 (tool contract), §3.3.2 (v1 catalog), §3.3.3 (per-instance authorization), §3.3.4 (sibling composition), §3.3.5 (tool execution isolation / BYO envelope), §3.4 (cognition is always-on), §3.7.2 (Wall 2 role+scope), §6 (arc deltas), §7 (locked decisions). Decisions that bind this arc: #5, #6, #19, #20, #21; roles are the four locked roles only (Decision #22 / Arc 12b is OUT of scope).

## Founder rulings made in this session (binding, treat as canonical)
1. **Alignment sweep is non-deferrable and file-wide.** The codebase at any point must contain exactly what the roadmap has shipped through the arc in progress — nothing speculative for a future arc, nothing left over from superseded architecture. No "clean it up later."
2. **Structure vs. behavior rule.** "Remove everything not yet built" governs *structure* (future-arc code, superseded scaffolds) → cut it. "Cognition is always-on" (§3.4) governs *behavior* the documents mandate on every Luciel every tier → cannot be cut, only relocated to its sanctioned interim host. When they collide, **behavior the documents mandate is preserved; structure the documents don't sanction is removed.**
3. **`max_composition_depth` is dead** — it contradicts Decision #19 ("no depth limit, no edge cap on the customer-facing composition graph"). Remove it. Keep `composition_enabled` as the §3.3.4 master switch.
4. **Cognition relocation is approved (option 2):** evict the 3 cognition tools from the registry and relocate them to a minimal, always-on cognition module — NOT deleted. Three binding conditions:
   - (a) **Interim-marked + ARC-14-tracked.** The module carries a clear greppable header + a tracked `TODO(ARC14)` stating it is the temporary host until `LucielOrchestrator.run` (Arc 14) subsumes it. Its absorption is an Arc 14 exit criterion.
   - (b) **Behavior-preserving, not behavior-expanding.** Move escalate / save_memory / summarize as they functionally are today, minus the substring dispatch and the Domain/Agent scaffold. Do NOT improve, extend, or re-architect cognition. Redesign is Arc 14.
   - (c) **Minimal & non-tier-gated**, exactly as §3.4 requires — no broker, no registry, no shadow agentic loop.
5. **chat_service.py alignment sweep is in scope** even though the Arc 12 delta row doesn't name it. The Domain/Agent threading (`domain_id`/`agent_id`, `_compose_system_prompt_additions`, tenant/domain/agent prompt layers in `LucielContext`/`_resolve_luciel_context`) is superseded v1 three-layer scaffold (v2 collapsed to a single Admin→Instance boundary per §3.7.2). Strip it. Strip the substring tool-detection (`"escalate_to_human" in raw_reply`). Record in closeout that the diff is larger than the delta row implies — this is documented, not a surprise.
6. **Closeout must record the cognition relocation as a documented interim deviation** from steady-state architecture: present in the tree, justified by §3.4, scheduled for Arc 14 absorption. Keep the "codebase reflects exactly what's shipped" invariant honest by naming the one thing that is ahead of its permanent home.

## What Arc 12 builds (delta row §6)
Tool registry expansion + v1 catalog tools; BYO webhook subprocess sandbox; sibling-Luciel composition runtime (cycle detection + per-inbound fan-out budget); `sibling_call_grants` table + grant-authoring API + Enterprise grant-approval workflow; tool UI; tool authorization at runtime.

## Explicitly OUT of scope (do not build; do not stub speculatively)
- The Arc 14 agentic loop internals (PLAN/ACT/REFLECT wiring, escalation gates, iteration bound). The broker + tools are built; the loop that calls them is Arc 14.
- Arc 12b: `permissions`/`custom_roles`/`role_permissions`/`user_role_assignments` tables, role-authoring, permission middleware. Four locked roles only.
- Arc 13: SES email adapter + Twilio SMS adapter bodies. `send_email`/`send_sms` are REGISTERED with full contracts this arc, but their `execute()` must not actually send — see 02_TOOL_CATALOG.md for the exact interim-body rule.
- Voice / WhatsApp / Slack adapters.

## The interim-body rule (the structure-vs-behavior line for tools)
A v1-catalog tool whose real implementation belongs to a later arc (e.g. `send_email`/`send_sms` → Arc 13 adapters) is **aligned, not drift**, IF AND ONLY IF: its full §3.3.1 contract is declared; its `execute()` returns a structured "not yet available — channel adapter ships Arc 13" `ToolResult`/dict and performs no side effect; and it carries a greppable `TODO(ARC13)` referencing the document anchor. A silent no-op the documents don't schedule is drift. The documents already schedule SES/Twilio for Arc 13, so a contract-complete tool whose body is explicitly Arc-13-deferred matches the documents. Tools whose behavior IS shippable now (`lookup_property` against admin CSV, `schedule_callback` enqueue, `push_to_crm` via the BYO/webhook-outbound path, `bring_your_own_webhook`, `call_sibling_luciel`, `book_appointment` insofar as a calendar integration exists) should be implemented for real to the extent their dependencies exist in the tree today; if a dependency does not exist yet, apply the same interim-body rule with the correct arc anchor and flag it.

## Build order (dependency-ordered — respect it)
WU1 Foundation → WU2 Authorization → WU3 v1 catalog → WU4 Sibling grants → WU5 Sibling runtime → WU6 BYO sandbox → WU7 chat_service sweep + cognition relocation → WU8 tests → (infra + UI handled separately). See 01_WORKUNITS.md.

## Definition of done (every WU)
- Code matches the documents and this spec.
- New/changed DB tables: hand-written Alembic migration chained off the current single head; RLS policy following §3.7.5 pattern (`SET app.admin_id`, fail-closed) for any tenant-scoped table; `instance_id` non-null indexed where customer/instance-scoped (Wall 3).
- Tests written and the full suite green (`pytest`). Do not mark a WU done with red or skipped-as-cover tests.
- No deferrals within the WU's own scope. No stale/dead code left behind in files you touch.
- Conventional, descriptive commit per WU.
