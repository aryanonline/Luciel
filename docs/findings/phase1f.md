# Phase 1f — Sessions / FK integrity

Reconstructed from code citations on `step-29y-impl`. See [`README.md`](./README.md) for methodology.

## F-7 — `sessions.api_key_id` lacks an FK constraint

### Code citations
- `app/api/v1/sessions.py:9` — "key in steady state, but findings_phase1f F-7 noted there is no FK"

### Resolution commits (on `step-29y-impl`)
None on `step-29y-impl`. The fix is sequenced for Step 30b alongside other FK-tightening migrations.

### Reconstructed summary

`sessions.api_key_id` references `api_keys.id` in steady-state access patterns but has no foreign-key constraint at the schema level. A direct DB write that inserts a session with a non-existent api_key_id, or a delete of an api_keys row without cascading to sessions, would silently break referential integrity.

The runtime guard in `app/api/v1/sessions.py` checks the FK existence in code on every session create, which is the current containment. The schema-level fix is queued for Step 30b.

This is the only `phase1f` finding cited in the codebase. If a future code change adds another `findings_phase1f` reference, add it here in the same commit per the maintenance contract in [`README.md`](./README.md).
