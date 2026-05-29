# Arc 12 — Canonical Document Reconciliation

The three canonical documents (VISION / ARCHITECTURE / CUSTOMER_JOURNEY) live as Space PDFs (founder-owned source of truth) and are NOT in the repo, so they cannot be edited from here. This file gives **exact, ready-to-apply amendment text** for the items where the IMPLEMENTATION is correct and the DOCUMENT is merely behind (apply verbatim), and separates them from the genuine founder POLICY decisions (which I have NOT pre-decided).

Verified as-built facts this reconciliation is based on (all confirmed against the running system):
- Composition tier behavior: Free ⇒ authoring rejected (call_sibling_luciel unavailable); Pro ⇒ grant lands `live` immediately; Enterprise ⇒ grant lands `pending_approval`, admin_owner approves → `live`. (sibling_call_grant_service.py)
- Cognition always-on set (relocated to app/cognition/, NOT in the tool registry): escalate (human handoff), save_memory, session_summary. (Decision #20, §3.4)
- Fan-out budget = `SIBLING_FAN_OUT_BUDGET = 12`, runtime-internal constant, not admin-configurable, not surfaced. Cycle detection per (caller,callee) pair per inbound message. No depth limit, no edge cap (Decision #19).
- BYO sandbox: in-container subprocess (asyncio.create_subprocess_exec), 30s SIGKILL, egress allowlist, per-endpoint Redis circuit breaker, 2-retry transport-only backoff, input/output JSON-Schema validation, tool_execution_log row per invocation. (§3.3.5)

---

## PART A — SETTLED: apply this amendment text verbatim (implementation is correct; doc is behind)

### A1. Architecture §4.1 + §4.3 — BYO sandbox topology
**Current text describes:** a separate "Subprocess sandbox pool" / "a small Fargate task family that scales with BYO webhook traffic."
**As built (correct, §3.3.5 envelope fully satisfied):** in-container subprocess isolation inside the existing backend/worker Fargate task — one OS subprocess per BYO invocation, hard-killed at the 30s boundary, no shared state with the worker, egress allowlist + per-endpoint circuit breaker.
**Replace the §4.1 line item with:**
> "BYO webhook execution isolation — in-container subprocess sandbox. Each `bring_your_own_webhook` invocation spawns a dedicated OS subprocess inside the backend/worker task (no shared state with the worker), enforced by a hard 30s SIGKILL timeout, a per-endpoint circuit breaker (Redis-backed), and a registered-domain egress allowlist. No separate Fargate task family is provisioned at v1; the subprocess model meets the §3.3.5 isolation envelope without a dedicated service. Revisit a separate task family only if BYO webhook traffic volume warrants horizontal isolation."
**Replace the §4.3 cost line with:** remove "a small Fargate task family that scales with BYO webhook traffic"; BYO adds no new always-on AWS resource at v1 (subprocess runs within existing task capacity; circuit-breaker state reuses the existing ElastiCache/Redis).
> NOTE: This is the only place the documents asserted infra the system does not build. Everything else in §4 matches. (Founder may instead direct building the separate family — see Part B if so; but the recommendation is to amend the doc to in-container.)

### A2. Architecture §3.7.2 / data model — three-layer (tenant/domain/agent) scope fully retired
**Current text (various sections) still references the (tenant_id, domain_id, agent_id) three-layer scope hierarchy in places.** As of Arc 12 the system is a single **Admin → Instance** boundary; `domain_id`/`agent_id` are fully excised from code, schema (all columns dropped), RLS policies, and the audit hash-chain (resealed). Any remaining doc prose describing domain/agent levels should be struck and replaced with the Admin→Instance model already canonical in §3.7.2. (This finishes a collapse a prior arc began and never completed.)

### A3. Architecture §3.3.4 — composition guardrails wording
Confirm the doc states the customer-facing composition graph has **no depth limit and no edge cap** (Decision #19), with cost controlled solely by (a) internal cycle detection per (caller,callee) pair per inbound message and (b) a runtime-internal per-inbound fan-out budget (not admin-configurable, not surfaced). If the doc anywhere still implies a configurable depth/cap, strike it. (Implementation matches Decision #19; retire any `max_composition_depth` mention — that field is removed from code.)

---

## PART B — GENUINE FOUNDER DECISIONS (not pre-decided; the system is built to be consistent whichever way you choose)

### B1. §4.1/§4.3 topology — confirm A1, OR direct a separate Fargate sandbox family.
Recommendation: accept A1 (in-container). The separate-family option is only worth it under high BYO volume; it is not needed to meet §3.3.5.

### B2. `lookup_property` data-source owning arc — UNASSIGNED in §3.3.2.
§3.3.2 names "MLS or admin-uploaded CSV" but no arc in §6 owns the property-source infrastructure. The tool ships contract-complete with an interim body (anchor=UNASSIGNED). **Assign an arc** (likely a future data-ingestion arc, NOT Arc 14 — Arc 14 is the agentic loop, not a data plane).

### B3. `push_to_crm` native-CRM (HubSpot/Salesforce) owning arc — UNASSIGNED in §6.
The custom-webhook path rides Arc 12 WU6's BYO outbound. Native connectors have no arc. **Assign an arc, or confirm BYO-webhook is the only v1 CRM path** (in which case amend §3.3.2 to say native CRM connectors are post-v1).

### B4. Dispatch-time tier re-check (TODO(ARC14)).
Tier is enforced at the WU2b authorize-on-instance API (an admin cannot create an authorization row for a tier-locked tool); the runtime broker relies on the row's existence (default-deny) rather than re-checking tier at dispatch. **Confirm authorization-row-as-tier-proxy is acceptable for v1**, or direct that the Arc 14 loop add a belt-and-suspenders dispatch-time tier check. (Built consistent with the former.)

### B5. EX4 audit-chain reseal — already your locked decision (recorded, no action).
Historical row_hashes were recomputed under the v2 field set; one-way by design. Logged via ACTION_AUDIT_CHAIN_RESEALED. Listed here only for the §5.3 audit-doctrine record.

### B6. Egress allowlist is application-layer only.
No VPC security-group network restriction on the BYO subprocess outbound. Sound for v1 (allowlist enforced in-process before the request). **Optional future hardening** — flag for a security arc, not an Arc 12 blocker.
