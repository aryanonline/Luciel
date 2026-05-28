# Arc 12 — Running Closeout Gap List (for founder review)

Maintained live during the build. Folded into the final closeout report at WU8/arc close.

## Document gaps / contradictions found (founder decision needed)
1. **`lookup_property` data source has no owning arc.** Architecture §3.3.2 names the source as "MLS or admin-uploaded CSV" but no arc in §6 owns the property-source infrastructure (admin CSV upload UI / MLS connector). Tool ships with a contract-complete interim body labeled UNASSIGNED. **Founder: assign an arc.**
2. **`push_to_crm` native-CRM paths have no owning arc.** §3.3.2 names "HubSpot, Salesforce, custom webhook." The custom-webhook path can ride Arc 12 WU6's BYO outbound mechanism; the native HubSpot/Salesforce connector paths are unassigned in §6. **Founder: assign an arc (or confirm BYO-webhook is the only v1 CRM path).**

## Documented interim deviations from steady-state architecture (justified, tracked)
- **Cognition relocation (WU7).** escalate / save_memory / summarize relocated from the tool registry to a minimal always-on cognition module (Decision #20, §3.4). Marked interim; absorption into `LucielOrchestrator.run` is an Arc 14 exit criterion. Present in tree ahead of its permanent home — named here per founder ruling 6.
- **Interim tool bodies.** send_email/send_sms (Arc 13 adapters), call_sibling_luciel (WU5 body), bring_your_own_webhook (WU6 body), book_appointment/schedule_callback (dependency-gated). All contract-complete, no side effect, greppable TODO(<ARC>). Aligned per the documents' own arc schedule, not drift.

## Pre-existing defects (NOT introduced by Arc 12; flagged for cleanup decision)
- **6 × `test_rls_c4_3*` failures** hardcode a stale absolute alembic.ini path (`/home/user/workspace/luciel/`). Machine-specific path in a test = drift. Predates Arc 12. **Decision: fix in Arc 12 sweep or schedule?** (Founder rule says no deferrals — leaning toward fixing at WU8 since the suite must be green to close.)
- **3 × `test_arc11_audit_script` failures** env-dependent. Same question.

## Larger-than-delta-row diffs (disclosed per founder ruling 5)
- **chat_service.py sweep (WU7):** removing superseded Domain/Agent three-layer scaffold + substring tool-detection. The Arc 12 delta row (§6) does not name chat_service; this is in scope under the non-deferrable file-wide alignment mandate. Diff will be larger than the delta row implies.
- **admin_tier_overrides.max_composition_depth column drop (WU2):** schema cleanup of a Decision-#19-violating column; not named in the delta row but required for schema-vs-documents alignment.

## Entitlements vs Vision §7 reconciliation
- TODO at WU8: verify app/policy/entitlements.py tier capability table matches Vision §7 exactly; flag any discrepancy before close (per arc completion rule).
