# Arc 12 — agent_id / domain_id Full Excision (founder-directed, in-arc, NON-DEFERRABLE)

Founder decision (2026-05-28): the superseded v1 three-layer (tenant/domain/agent) scaffold must be FULLY excised — code AND live schema — inside Arc 12. The system must be aligned with the documents (v2 = single Admin→Instance boundary, §3.7.2) at arc close. No deferral.

## Hard facts established from the live repo (these drive the migration shape)
- **`agent_id` IS in the audit hash chain.** `app/repositories/audit_chain.py` lists `"agent_id"` in the canonical-content field set (line ~94); historical `admin_audit_logs` rows were hashed WITH `agent_id` (note at line ~114). Dropping the column naively changes the canonical hash input and breaks historical chain verification. **This is the most dangerous step and is handled LAST, explicitly.**
- **`agent_id` is still live-WRITTEN and live-READ** outside the chat path: `api_key.agent_id` is set from admin payloads; auth middleware (`app/middleware/auth.py`, `session_cookie_auth.py`) stamps `request.state.agent_id` from the API key; `memory_repository`, `session_repository`, `trace_repository`, `dashboard_service`, `admin_forensics`, `audit_log` API all READ/FILTER by it. So it is NOT globally dead — only dead from the WU7-collapsed chat path. Excision must rewrite identity/auth/forensics/dashboard too.
- **`agent_id` is in an active RLS filter** (`alembic/versions/arc9_c3_3_rls_knowledge_embeddings.py`). RLS policy must be rewritten to the v2 (admin_id + instance_id) shape BEFORE/WITH the column drop, fail-closed, following the §3.7.5 pattern. RLS rewrite errors = tenant leak — the one failure the architecture exists to prevent.
- **Live columns to remove:** `memory.agent_id` (indexed), `session.agent_id` (indexed), `trace.agent_id`, `api_key.agent_id`, `admin_audit_log.agent_id` (hash-chained). Plus any `domain_id` columns. ~61 app/ files reference agent_id/domain_id.

## Sequencing doctrine (safety-ordered; each step independently tested + green before the next)
The order is chosen so the system is NEVER left with a broken RLS policy or an unverifiable audit chain at any intermediate commit.

**EX1 — Code-level callsite + signature sweep (no schema change).**
Remove `agent_id`/`domain_id` params from service/repository/API signatures and callsites across app/, collapsing to admin_id + instance_id (luciel_instance_id). The chat path (WU7) already passes None — now remove the params entirely. Identity/auth: stop stamping `request.state.agent_id`; rewrite `api_key` minting to not require agent_id. Forensics/dashboard/audit-log API: drop agent_id query filters/response fields (or map to instance_id where that was the real intent). After EX1, NOTHING in app/ writes or reads agent_id/domain_id except the ORM column definitions themselves. Full pytest green. This step is reversible (no data loss).

**EX2 — RLS rewrite for knowledge embeddings (and any other agent_id-referencing policy).**
Rewrite the RLS policy that references agent_id to the v2 admin_id+instance_id shape per §3.7.5 (fail-closed, USING + WITH CHECK). Migration chained off the current head. Verify RLS still fail-closes (zero rows without app.admin_id) and that legacy rows remain correctly isolated. This MUST land before the knowledge agent_id column (if any) is dropped. Tenant-isolation tests green.

**EX3 — Drop the non-audit-chain columns.**
Forward migration: drop `memory.agent_id`, `session.agent_id`, `trace.agent_id`, `api_key.agent_id`, and any `domain_id` columns + their indexes. Each with a downgrade re-add. These are not hash-chained, so the drop is a standard (irreversible-against-data, but reversible-schema) migration. Confirm no remaining code references (EX1 guarantees this). Models updated to drop the Mapped columns. Full pytest green.

**EX4 — Audit-chain column handled explicitly + LAST.**
`admin_audit_log.agent_id` is in the canonical hash set. Two valid approaches — choose and DOCUMENT:
  (A) **Remove `agent_id` from the canonical hash field set AND drop the column, with a chain reseal:** recompute row_hash/prev_row_hash for all historical rows under the new canonical set, in a controlled migration, preserving append-only semantics and emitting an audit-of-the-reseal record. Highest fidelity to "column gone everywhere," but rehashes history.
  (B) **Keep `agent_id` OUT of new rows' canonical content and drop the column going forward while preserving historical verifiability** via a versioned canonical-hash function (rows before the cut verify with the old field set incl. agent_id from an archived value; rows after verify without). Preserves the original chain untouched.
  The founder mandate is "column gone from the system." If the column is physically dropped, historical agent_id values needed for (B)'s old-set verification are gone — so (A) (reseal) is the consistent choice IF we must drop the physical column. Flag the reseal as a documented, audited integrity operation in the closeout. RLS on admin_audit_logs must remain intact throughout. Audit-immutability + chain-verification tests green after reseal.

## Gate
After EX1–EX4: grep for `\bagent_id\b`/`\bdomain_id\b` across app/ returns ONLY (a) historical migration files (never edited) and (b) the EX4 reseal's documented archival, if any. Full pytest green with the failure-bisect from WU8. RLS fail-closed verified. Audit chain verifies end-to-end.

This excision is folded into Arc 12's closeout as IN-SCOPE founder-directed work (not a documented deviation) — but the EX4 audit-chain reseal IS recorded as a deliberate integrity operation with its rationale.
