# Arc 11 — Decisions Log

Distilled from `ARC11_PLAN.md` §11 (decisions) and §12 (production
verification). One page so a future auditor can see the locked-in
choices without reading the whole plan.

---

## Q1 — Two-table migration: LOCKED to Option A

**Decision (2026-05-28T14:11):** Migrate to the doctrinal two-table
shape: new `knowledge_sources` (provenance) + rename
`knowledge_embeddings` → `knowledge_chunks` (vectors), FK from chunks
to sources.

**Reasoning:** Production verification showed
`knowledge_embeddings` held zero rows, so the cost of Option A
collapsed to a rename + an additive FK migration with no backfill.
Doctrine drift was the only material cost of any other path; Option A
costs the same now as it would cost later. Doctrine wins.

**Where it landed:** Steps 1 (additive schema), 2 (rename), 3
(repository/retriever/ingestion refactor), 4 (RLS + HNSW). Locked
backwards-compat alias `KnowledgeEmbedding = KnowledgeChunk` at the
model level so call sites migrate incrementally.

---

## Q2 — Worker reuse: LOCKED to reuse `luciel-worker-service`

**Decision (2026-05-28T14:11):** The new `embed_source` Celery task
registers into the existing broker and runs in the existing worker
ECS task family. No new task family.

**Reasoning:** Architecture v1 §4.3 says "pgvector storage + IO
negligible until ~10M chunks." Spinning a dedicated worker family at
Free/Pro scale would be over-engineering. If volume forces a split
later, that's a future config change (route the queue to a different
service via SQS subscription), not a re-architecture.

**Where it landed:** Step 6 (`app/worker/tasks/embed_source.py`
registered into `celery_app.include[]`). Worker drains both
`luciel-memory-tasks` and `luciel-knowledge-tasks` queues
(`td-worker-rev34-arc11.json` `-Q` flag).

---

## Q3 — Legacy `agent_id` cleanup: LOCKED to defer

**Decision (2026-05-28T14:11):** Do NOT remove the legacy `agent_id`
column from `knowledge_chunks` in Arc 11. Mark it DEPRECATED in the
model docstring; a dedicated post-Arc-11 PR removes the column and
its read-compat code together.

**Reasoning:** Initial read suggested a small annotation removal.
Deeper inspection found `agent_id` is woven through `retriever.py`,
`knowledge_repository.py`, and `ingestion.py` on the read-side
compatibility path for legacy pre-Step-24.5 rows. Zero such rows
exist in production. The compat path can be removed safely — but
bundling that behavioural refactor with the structural sources/chunks
split would double Arc 11's blast radius for no doctrinal gain.

**Where it landed:** Step 3 left `agent_id` in place; logged in
[`_arc11/CLEANUP_CANDIDATES.md`](./CLEANUP_CANDIDATES.md) as item #1.

---

## Production verification (2026-05-28T14:11)

Queried live RDS via ECS Exec into `luciel-backend` task. Findings
that calibrated the plan:

| Check | Value | Implication |
|---|---|---|
| `count(*) FROM knowledge_embeddings` | 0 | Backfill is trivial; rename is safe |
| `count(*) FROM knowledge_embeddings WHERE agent_id IS NOT NULL` | 0 | No legacy compat rows to preserve |
| `count(*) FROM admins` | 0 | Pre-launch. No customer data at risk during migration. |
| `count(*) FROM instances` | 0 | Same. |
| `count(*) FROM traces` | 0 | Extending `traces.source_ids_used` is a clean additive migration. |
| pgvector version | 0.8.1 | HNSW supported (≥0.5.0); no extension upgrade needed |
| S3 knowledge bucket | does not exist | Arc 11 creates `luciel-knowledge-prod-ca-central-1` |
| ECS cluster | `luciel-cluster` with `luciel-backend-service` + `luciel-worker-service` running | Worker reuse confirmed viable |
| ECS Exec | enabled on both tasks | Smoke-probe path for §8.5 audit |
| Region | ca-central-1 confirmed for all resources | Matches Architecture §4.2 |

---

## Security finding flagged at verification

During verification, the DB connection URL (containing the
`luciel_app` role's password in cleartext) was echoed to the ECS
Exec session output stream. Standard Fargate behavior (SecureString
SSM params decrypt into task env), but it confirms the long-lived
agent IAM key + the DB password both deserve rotation after Arc 11
closes. See
[`_arc11/SECURITY_FOLLOWUPS.md`](./SECURITY_FOLLOWUPS.md).
