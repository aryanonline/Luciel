# ARC 12 — Closeout & Alignment Verification

Branch: `arc12/tool-registry-sibling-byo` (53 commits off Arc 11 head `d4baf14`). **NOT merged — founder closes the arc.** Single Alembic head: `arc12_ex4_reseal_audit_chain_drop_agent_domain` (14 Arc-12 migrations). Website: branch `arc12/tool-ui`.

## 1. What Arc 12 delivered (against the §6 delta row)
- **Tool registry expansion + §3.3.1 contract** (WU1): `LucielTool` migrated to tool_id/display_name/description/input_schema/output_schema/requires_tier/requires_channels/execution_mode/async execute(input, context). `max_composition_depth` retired (Decision #19). JSON-Schema validator added.
- **v1 tool catalog** (WU3): exactly 8 configurable tools — book_appointment, send_email, send_sms, lookup_property, schedule_callback, push_to_crm, call_sibling_luciel, bring_your_own_webhook. requires_tier=(pro,enterprise); send_email/sms requires_channels; BYO execution_mode=subprocess. Cognition NOT in the registry (Decision #20).
- **Per-instance tool authorization** (WU2 + WU2b): `instance_tool_authorizations` table (RLS, default-deny, Wall-1/Wall-3 scoped); `DefaultDenyToolAuthorizer` broker gate at dispatch (stable interface for Arc 14); admin API to list/authorize/revoke tools per instance (role-gated owner+manager).
- **Sibling-Luciel composition** (WU4 + WU5): `sibling_call_grants` table (§3.3.4 columns, composite index, partial-unique constraint, RLS); grant-authoring API with Wall-2 scope-on-both-endpoints rule + Enterprise approval workflow (Free rejected / Pro live / Enterprise pending→approve); runtime dispatch with cycle detection + per-inbound fan-out budget (runtime-internal, not admin-configurable, not in UI), master-switch + live-grant lookup; sibling-access audit row + tool_execution_log row; deactivation cascade (§3.6.1 step 3) wired to the real table. No depth/edge cap (Decision #19).
- **BYO webhook subprocess sandbox** (WU6): full §3.3.5 envelope — in-container subprocess (one per invocation), 30s SIGKILL, input/output JSON-Schema validation, 2-retry exp backoff (transport-only), per-endpoint Redis circuit breaker (5/60s open, 60s half-open, close on success), egress allowlist, audit row per invocation. `byo_webhook_endpoints` + `tool_execution_log` tables.
- **Tool UI** (website): two-band config surface (display-only built-in cognition band + add-on tools checkboxes with tier-greying/channel-annotation) + sibling-grant authoring UI (scoped-instance dropdowns, approval-state badges, role-gated approve).
- **Tool authorization at runtime** (WU2): broker verifies (admin_id, instance_id, tool_id) default-deny before dispatch; interface stable for Arc 14.

## 2. Founder-directed in-arc work beyond the delta row (disclosed)
- **Cognition relocation** (WU7): the 3 cognition behaviors (escalate/save_memory/summarize) evicted from the registry and relocated to `app/cognition/` — a minimal, always-on, non-tier-gated interim module, behavior-preserving. **DOCUMENTED INTERIM DEVIATION:** marked TODO(ARC14); absorption into `LucielOrchestrator.run` is an Arc 14 exit criterion. Present in the tree ahead of its permanent home, justified by §3.4, per founder ruling.
- **chat_service v2 sweep** (WU7): removed superseded Domain/Agent three-layer scaffold + substring tool-detection. Larger diff than the delta row implies — disclosed per founder ruling.
- **Full agent_id/domain_id excision** (EX1-EX4, founder-directed): code (EX1a-d), RLS rewrite (EX2), 9 non-audit-chain column drops (EX3, one table per migration), and the **admin_audit_logs hash-chain RESEAL** (EX4, founder-locked) — removed agent_id/domain_id from `_CHAIN_FIELDS`, dropped the columns, recomputed all historical row hashes under advisory lock from GENESIS, emitted an ACTION_AUDIT_CHAIN_RESEALED traceability record. Uniqueness guarantees preserved on v2 column sets (identity_claim, scope_assignment). **Verified: zero agent_id/domain_id ORM columns remain.**

## 3. Seven-dimension alignment check (founder completion rule)
1. **Deployed code = merged branch:** N/A until founder merges; branch is self-consistent, no in-flight unmerged work; website on `arc12/tool-ui`.
2. **Container image = latest build:** task-defs reviewed (WU14); no Arc-12 change requires an image/env change beyond what's documented; image rebuild + deploy is a founder action at merge.
3. **DB schema = latest migration:** single head `arc12_ex4_reseal_audit_chain_drop_agent_domain`; standard `alembic upgrade head` deploy path covers all 14 migrations. **EX4 reseal is long-running — recommend a maintenance window for large admin_audit_logs (WU14 flag).**
4. **Env/SSM = documented config:** no new env/SSM params (all Arc-12 knobs are runtime-internal constants; circuit breaker reuses settings.redis_url). `.env.example` corrected (removed 6 stale pre-Arc-12 fields).
5. **Frontend tool UI = backend contracts:** Tool UI built against the real WU2b + WU4 routes; website domain_id contract drift removed (WU8b); no ghost routes.
6. **Tier entitlements = Vision §7:** internally consistent on load-bearing axes; two soft items flagged for review (below).
7. **Three canonical docs internally consistent with the system:** consistent except the flagged items below — none resolved unilaterally.

## 4. Tests & REAL-DATABASE verification (run by the agent in-environment, not on subagent report)
**A live Postgres 17 + Redis were provisioned and `alembic upgrade head` was run against a REAL database — this caught two release-blocking migration bugs the sqlite-based test suite (1923 passing) completely missed, because sqlite does not enforce Postgres enums or FKs the same way:**
1. **EX3 scope_assignment (FIXED):** the recreated `arc9_c22_bootstrap_identity` SECURITY DEFINER function compared the `scope_role` ENUM against the string literal `'owner'` (the pre-cleanup_c role name) → `InvalidTextRepresentation`. Fixed to `'admin_owner'` (the v2 enum label) in both upgrade + downgrade bodies.
2. **EX4 reseal (FIXED):** the reseal self-audit row inserted `admin_id='platform'`, but `admin_audit_logs.admin_id` is NOT NULL FK→admins.id RESTRICT and no migration seeds the `platform` sentinel admin → `ForeignKeyViolation` on any fresh DB (would have bricked the prod deploy). Fixed by idempotently seeding the `platform` system-actor admin (`ON CONFLICT DO NOTHING`) before the reseal record.
   - **Bisect proof:** Arc 11 baseline `d4baf14` migrates to head cleanly (exit 0); the Arc 12 branch failed before these fixes → both were Arc-12-introduced, now resolved.
VERIFIED on real Postgres after the fixes: `alembic upgrade head` EXIT 0 → head `arc12_ex4_reseal_audit_chain_drop_agent_domain`; all 4 Arc-12 tables present with RLS enabled; zero agent_id/domain_id columns in the live schema; EX4 reseal record written + platform sentinel seeded; **the audit hash chain VERIFIES under the runtime verifier with the new field set**; EX4 downgrade→re-upgrade round-trips clean (reversible as documented).

Full pytest suite (sqlite, self-configured): **1923 passed / 0 failed / 61 skipped**, run by the agent directly.

Earlier note (WU8a-verify, bisected against `d4baf14`): **0 failures** at the unit level. The earlier "11 pre-existing failures" were bisected: 6× rls_c4_3 (genuinely pre-existing stale-path — FIXED to repo-relative), 3× audit_script (Arc-12-INTRODUCED by the migration-head pin — FIXED), 2× lookup_property (Arc-12-introduced interim-body assertions — aligned). A subsequent 1-test failure from the founder-review lookup_property anchor correction was fixed in lockstep (code + both test assertion sites → UNASSIGNED). Suite green.

## 5. FLAGGED FOR FOUNDER REVIEW (not resolved unilaterally)
1. **§4.1/§4.3 vs implementation — BYO sandbox topology.** Architecture describes a separate Fargate "subprocess sandbox pool / small Fargate task family"; WU6 ships in-container subprocess isolation. §3.3.5 envelope fully met either way. Recommendation: keep in-container for v1, amend §4.1/§4.3 to match. **Founder: confirm + amend the doc, or direct a separate task family.**
2. **`lookup_property` data source has no owning arc** (§3.3.2 names "MLS or admin-uploaded CSV" but assigns no arc). Ships contract-complete, interim-body, anchor=UNASSIGNED. **Founder: assign an arc.**
3. **`push_to_crm` native-CRM paths** (HubSpot/Salesforce) have no owning arc; the custom-webhook path rides WU6's BYO outbound. Interim-body, anchor=ARC12_WU6 for the webhook path. **Founder: assign an arc for native CRM connectors.**
4. **Dispatch-time tier re-check deferred to Arc 14.** Tier is enforced at the WU2b authorize API (can't authorize a tier-locked tool); the runtime broker relies on the authorization-row's existence (default-deny) rather than re-checking tier at dispatch. Defensible for v1. **Founder: confirm authorization-row-as-tier-proxy is acceptable for v1.**
5. **EX4 reseal is a one-way integrity operation** — historical audit row_hashes were recomputed under the v2 field set; downgrade is schema-only (v1 hashes unrecoverable by design). This was the founder-locked choice (reseal over versioned-verifier). Recorded as a deliberate, audited integrity operation.
6. **Egress allowlist is application-layer only** (no VPC security-group restriction). Sound for v1; flagged for future network-layer hardening (WU14).

## 6. Explicitly NOT done (correct per scope)
- Arc 14 agentic loop internals (the broker + tools are built; the loop that calls them is Arc 14). `LucielOrchestrator.run` remains a stub.
- Arc 13 SES/Twilio channel adapter bodies (send_email/send_sms registered contract-complete with interim bodies, TODO(ARC13)).
- Arc 12b custom roles (four locked roles only).
- Voice/WhatsApp/Slack adapters.
