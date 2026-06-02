# ARC 16 — System State Audit (Knowledge Subsystem)

Status: PRE-BUILD. No ARC 16 code written yet. This audit gates the build.
Audited: 2026-06-02. Branch: `main` @ `abd222c` (Arc 15 BE closeout, #134).
Canonical docs: VANTAGEMIND_VISION_v1, _ARCHITECTURE_v1, _CUSTOMER_JOURNEY_v1
(Space PDFs; internal titles confirm FINAL status — the `_FINAL.md` names in the
thread brief are the canonical internal names, the PDFs are those exports. No
stale-spec divergence.)

---

## 0. Verdict

The knowledge subsystem is in a **clean, well-documented Arc 11 state**. The
two-table model, scoping, HNSW index, RLS, and the never-raise retrieval contract
all match Architecture §3.2. ARC 16 is genuinely additive — I found **no blocking
code/schema drift**. There are **three things that must be settled before or
during ARC 16** (one is a real gate, two are contract shaping), and **one
environmental blocker** (no live AWS introspection from this sandbox) that is the
project's standing operating model, not a new problem.

The PostgreSQL-recursive-CTE graph lock (§3.2.1) is consistent with the
architecture — the "Graph DB" box in the §2.1 persistence diagram is the *logical*
store, explicitly bound to Postgres CTEs in §3.2.1. No vendor contradiction.

---

## 1. What is deployed (last-known-good, from repo artifacts)

| Item | State | Source |
|---|---|---|
| Backend image | `luciel-backend@sha256:b4c145eb…c4dff` | `td-backend-rev78.json` |
| Region | `ca-central-1` (single region, §4.2) | task-defs |
| Migration head | `arc15_c_drop_system_prompt_additions` (single head, 127 migrations, chain intact) | alembic scan |
| `knowledge_retrieval_enabled` | **`False`** (code default; NOT set in rev78 env/secrets) | `app/core/config.py:583` |

**Live introspection NOT performed** — see §5 (blocker). The above is the
last-known-good from committed task-def artifacts, not a live `aws ecs
describe-services` / `aws rds` read.

---

## 2. Current retrieval path (Arc 11, validated)

Chat path → `LucielOrchestrator.run()` (`app/runtime/orchestrator.py`):

1. **CONTEXT ASSEMBLY** gated on `knowledge_retrieval_enabled AND luciel_instance_id is not None` (orchestrator.py:127). Flag-off ⇒ retrieval never runs.
2. `_retrieve()` (orchestrator.py:765) opens a **tenant-scoped session** via `bind_tenant_scope(admin_id, instance_id)`, builds `KnowledgeRetriever`, calls `retrieve_with_sources(...)`. **Never raises** — any failure ⇒ `[]`.
3. `KnowledgeRetriever.retrieve_with_sources` (retriever.py:116) → `embed_single(query)` → `KnowledgeRepository.search_similar`.
4. `search_similar` (knowledge_repository.py:222): union-scoped (instance-private ∪ tenant-shared ∪ global), INNER JOIN `knowledge_sources`, filters `superseded_at IS NULL`, `soft_deleted_at IS NULL`, `pending_downgrade_archived_at IS NULL`, **source `ingestion_status='ready'`**, orders by `embedding <=> query` (cosine). Matches Architecture §3.2 retrieval flow steps 1–4.
5. Results → `RetrievedChunk` (carries `content`, `source_identifier`, `chunk_id`, `distance`, `formatted`). `collect_source_pks` dedupes source PKs into `traces.source_ids_used`.
6. `ContextAssembler.build_prompt(..., retrieved_chunks=)` emits a `KNOWLEDGE_CONTEXT:` stanza, capped at 8KB.

**This is the seam ARC 16 plugs into.** The hybrid path inserts the graph filter
*ahead of* the vector call inside this flow; the existing `search_similar` becomes
the vector lane.

## 2a. Schema (matches model + migration)

- `knowledge_sources` (provenance) + `knowledge_chunks` (embeddings). Both scoped `admin_id` + `luciel_instance_id`. ✔ §3.2.
- Indexes: HNSW `ix_knowledge_chunks_embedding_hnsw` (m=16, ef_construction=64) via `arc11_d3`; composite `ix_knowledge_chunks_scope_source` (admin_id, instance_id, source_id); FK index on `source_id`. ✔ §3.2.
- RLS: `knowledge_chunks` (policies from `arc9_c3_3`/`c4_3b`, renamed in `arc11_d2`) + `knowledge_sources` (`arc11_d1`), fail-closed on `app.admin_id`. ✔ §3.7.5.

## 2b. Admin raw-knowledge view (the trust surface — §3.2.2)

`app/api/v1/admin_knowledge.py`:
- `GET /sources/{id}/chunks` (preview_chunks, line 664): reads chunks by `source_id`+`admin_id`, ordered by `id`, lifecycle-filtered. **This is what admins see.**
- `POST /internal/v1/retrieve` (line 1064, platform_admin): runs the **real** retriever + `EXPLAIN ANALYZE` — the verification surface.

Because ARC 16 does **not** alter chunk storage (graph is a sibling structure over
the same chunks/sources), this surface stays accurate by construction. I will
re-confirm this as an explicit check at end-of-arc (Contract #5).

---

## 3. Items to settle for ARC 16 (NOT blockers to starting, but in-scope)

**3.1 — Retrieval-contract gap (real, must fix in ARC 16).**
Behavioral Contract #4 requires every returned chunk to carry
`(admin_id, instance_id, source_id)`. Today `search_similar` **does** SELECT all
three (knowledge_repository.py:334–337), but `RetrievedChunk` **drops admin_id and
instance_id** — it keeps only `source_identifier`. ARC 16 must thread `admin_id`
and `luciel_instance_id` onto the merged-context object so deterministic scoping is
*verifiable at the output*, not just enforced at the filter. Low-risk: data already
flows back; this is plumbing, not a new query.

**3.2 — Graph/vector merge must preserve the never-raise contract.**
`_retrieve` and the retriever are firewalled (`return []` on any failure). The
graph filter stage MUST inherit this: a graph-CTE failure falls through to
vector-only, never crashes the turn. This is the Contract #2 "vector-only fallback
lane" — I'll implement the graph stage as strictly additive and independently
fail-closed-to-fallback.

**3.3 — Knowledge vs. tool-data wall (§3.2, §3.8).**
The graph must encode relationships over **knowledge** (sources/chunks), NOT
`lookup_property` structured data (that's a Connections artifact, never embedded,
Arc 17). ARC 16 graph edges reference `knowledge_sources`/`knowledge_chunks` only.
I will not let any property-CSV/MLS structured data into the graph store.

---

## 4. Minor doc/comment drift (non-blocking, will fix in-place if touched)

- `retriever.py:5` module docstring still says "(admin, domain, luciel_instance)" — `domain` was dropped in Arc 12 EX3. Stale comment only; behavior is correct.
- `knowledge.py:154–157`: the `embedding` pgvector column "was created manually via raw SQL… Do NOT run autogenerate against this table." **Load-bearing constraint for ARC 16:** my new graph migration must be hand-written and must NOT autogenerate against `knowledge_chunks`, or it will try to "add" the out-of-band embedding column. Noted and will be honored.

---

## 5. Environmental blocker (named per System Alignment Mandate)

**No live AWS introspection is possible from this build sandbox.**
- AWS CLI installed (`/usr/local/bin/aws`) but **no credentials** in env.
- The AWS connector (Pipedream) exposes only S3/SQS/SNS/DynamoDB/Lambda/Redshift/CloudWatch action tools — **no ECS, RDS, or SSM** read tools.

**Impact:** I cannot, from here, directly verify (a) the image the live ECS service
is running vs. rev78, (b) the live RDS schema vs. the Alembic head, or (c) whether
the live service overrides `knowledge_retrieval_enabled` via SSM.

**This is the project's standing model, not a regression.** The ARC 15 reports
state the same: "Postgres is unavailable in this environment… the live upgrade is
to be run in CI/staging." Alignment is enforced at the **CI migration-runner + ECS
update boundary** (`.github/workflows/ci.yml`), which runs `alembic upgrade head`
and the service update together.

**Resolution path (chosen, pending your call):** Build + verify ARC 16 against the
repo and a local/CI Postgres (real pgvector + recursive CTEs), land migrations
through the same CI pipeline ARC 11/15 used, and confirm deployed-state alignment
at the CI/deploy boundary. If you want me to verify the *live* prod state directly
(running image, applied migration head, the prod value of
`knowledge_retrieval_enabled`), I need one of: AWS credentials in this sandbox, or
you running 3 read-only commands and pasting output (`aws ecs describe-services`,
`SELECT version_num FROM alembic_version`, and the service's resolved flag value).

---

## 6. The `knowledge_retrieval_enabled=False` question (needs your decision)

If retrieval is flag-off in prod (likely, given rev78), then **Arc 11 vector
retrieval is currently dark in production**, and the ARC 16 hybrid path will also be
dark until the flag flips. That's fine for *building and testing* ARC 16, but it
changes what "the hybrid retrieval path is live and tested end-to-end" (Contract
#7c) can mean at end-of-arc:

- If you want ARC 16 to ship **enabled**, flipping `knowledge_retrieval_enabled=True` turns on BOTH Arc 11 vector retrieval and the new hybrid path simultaneously — that's a real behavior change for every tenant's chat, and it should be a deliberate, observed rollout, not a side effect of ARC 16.
- If you want ARC 16 to ship **behind the flag** (built, migrated, tested via `/internal/v1/retrieve`, but flag-off in prod), end-to-end validation happens on the internal verification endpoint, and the flag flip is a separate decision.

I recommend the second: land ARC 16 flag-off, validate via the internal endpoint
+ CI, and treat the flag flip as its own gated rollout. **Confirm before I build.**
