# Phase 1d — Audit-chain integrity

Reconstructed from code citations and resolution commits on `step-29y-impl`. See [`README.md`](./README.md) for methodology.

## D-8 — `admin_audit_logs.row_hash` and `prev_row_hash` not enforced NOT NULL

### Code citations
- `alembic/versions/c5d8a1e7b3f9_step29y_cluster3_audit_row_hash_not_null.py:10` — "findings_phase1d.md D-8 documents a deploy-window NULL-row-hash"

### Resolution commits (on `step-29y-impl`)
- `fc79527` — Step 29.y Cluster 3 (D-8): row_hash + prev_row_hash NOT NULL on model
- `d413a0b` — Step 29.y Cluster 3 (D-8): admin_audit_logs.row_hash + prev_row_hash NOT NULL
- `2fd3ae3` — Step 29.y Cluster 3 (D-9 + D-8): _CHAIN_FIELDS + NOT NULL invariants

### Reconstructed summary

The Step 28 P3-E.2 audit hash chain (Pillar 23) populates `row_hash` and `prev_row_hash` via a SQLAlchemy `before_flush` event handler. During a rolling deploy, the OLD container image (without the event) and the NEW image briefly run side-by-side; rows inserted by the OLD image have NULL hashes, which Pillar 23 tolerates as a deploy-window artefact.

D-8: nothing prevented a *non-deploy-window* NULL from sneaking in (e.g. a future code path that bypassed the event). The DB-level guarantee was missing. The Cluster 3 fix:
- Adds `nullable=False` on the model fields.
- Adds an Alembic migration `c5d8a1e7b3f9` that enforces NOT NULL at the schema level.
- The migration includes a self-adapting probe (Pillar 23 reads `information_schema` to learn the actual column nullability and adapts its assertion) so the same Pillar build works pre- and post-migration. See `fa19e38` in Cluster 8.

## D-9 — `_CHAIN_FIELDS` drift hazard

### Code citations
- `tests/integrity/test_audit_chain_fields_in_sync.py:3` — "findings_phase1d.md D-9 documents a silent drift hazard"

### Resolution commits (on `step-29y-impl`)
- `2fd3ae3` — Step 29.y Cluster 3 (D-9 + D-8): _CHAIN_FIELDS + NOT NULL invariants
- `41ef3a4` — Step 29.y Cluster 3: tests/integrity package marker

### Reconstructed summary

If a future migration adds a column to `admin_audit_logs` and the maintainer forgets to extend `app.repositories.audit_chain._CHAIN_FIELDS`, all existing rows continue to verify (their chain content is unchanged) but every new row has a hash input that omits the new column. A forensic auditor diffing the schema against the hash inputs would catch this only after the fact.

The Cluster 3 fix adds `tests/integrity/test_audit_chain_fields_in_sync.py`, a CI-time AST + reflection test that asserts `set(_CHAIN_FIELDS) == set(AdminAuditLog table columns) - {audit-internal columns}`. Any future column add that forgets to update `_CHAIN_FIELDS` fails CI loudly.

Note: this gap-fix Commit 1 (`D-actor-permissions-comma-fragility-2026-05-07`) preserves the chain by NOT touching `_CHAIN_FIELDS`. Historical rows recompute identically because their column values are not rewritten.
