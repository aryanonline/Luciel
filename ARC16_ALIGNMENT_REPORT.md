# ARC 16 — End-of-Arc Alignment Report

**Arc:** Graph Knowledge Store + Hybrid Retrieval
**Date:** 2026-06-02
**Branch base:** `main` @ `abd222c` (Arc 15 BE closeout)
**Migration head:** `arc16_c_knowledge_graph_store` (single head, 130 migrations, round-trips clean)
**Canonical docs:** VANTAGEMIND_VISION_v1, _ARCHITECTURE_v1, _CUSTOMER_JOURNEY_v1 (post-rewrite, re-audited 2026-06-02)
**Environment:** Built + live-tested end-to-end on local Postgres 17 + pgvector. **Zero AWS spend** — nothing in this arc touched AWS (founder directive).

---

## 0. Verdict

ARC 16 is **functionally complete and live-verified** against real Postgres. The graph knowledge store, recursive-CTE traversal, hybrid retrieval engine, entity-extraction pipeline, and end-to-end ingestion wiring are all built, wired, and tested. The system is aligned with the three canonical documents on every point ARC 16 touches.

**Two honest, explicitly-scoped gaps remain** (§6), neither of which is a code defect:
1. The LLM extraction call + the 2 internal-retrieve verification tests need a **real OpenAI key** — verified by structure/stubs here, live-validated via the runbook (§7).
2. Deployment is intentionally deferred (founder directive: deploy only once the whole system is aligned and demoable). This report certifies code/schema/contract alignment; the deploy-time alignment (image, live RDS, env) is a runbook step (§7).

No regressions: my changes were stashed and the pre-existing failures reproduced without them (§5).

---

## 1. Deliverables — built and verified

| ARC 16 deliverable | Artifact | Live-tested |
|---|---|---|
| Graph store (PG recursive CTEs, no vendor) | `app/models/knowledge_graph.py`, migration `arc16_c` | ✔ 4/4 `test_arc16_graph_store_live` |
| Recursive-CTE traversal (depth bound + cycle guard) | `app/repositories/knowledge_graph_repository.py` | ✔ multi-hop + cycle proven |
| Hybrid retrieval engine (graph→vector→merge, vector fallback, never-raise) | `app/runtime/knowledge_retrieval.py` | ✔ 5/5 `test_arc16_hybrid_retrieval_live` |
| Orchestrator routed through hybrid path | `app/runtime/orchestrator.py` `_retrieve` | ✔ 19/19 wiring tests |
| Entity-extraction pipeline (LLM extract + deterministic resolution) | `app/knowledge/graph_extractor.py` | ✔ 3/3 population + 5/5 adapter |
| Ingestion → graph wiring (additive, opt-in, never-fail-ingest) | `app/knowledge/ingestion.py` | ✔ 3/3 `test_arc16_ingestion_graph_wiring_live` |
| Retrieval contract #4 (scope triple on every chunk) | `app/knowledge/retriever.py` `RetrievedChunk` | ✔ live scope-triple test |

**ARC 16 test total: 35 passed, 2 skipped (embedder-gated). Knowledge/runtime regression: 158 passed.**

---

## 2. Alignment to the canonical documents (every point ARC 16 touches)

**Vision §3.3 / Architecture §3.2.1 + Locked Decisions 4–6:**
- ✔ **Locked 4 — PG recursive CTEs, no vendor.** Graph is two Postgres tables; traversal is recursive-CTE SQL. No Neo4j/Memgraph. Bounded for the ≤1M-edge target via scope-prefixed traversal indexes.
- ✔ **Locked 5 — domain-agnostic entities.** `entity_type` / `relationship_type` are free-text strings inferred at ingest. No hardcoded ontology, no enum. Proven by the med-spa-shaped extractor test (Service/Practitioner/TREATS), not just real-estate.
- ✔ **Locked 6 — graph only on structured-filter intent.** `has_structured_filter_intent` gates the graph; pure-semantic queries bypass to vector-only. Proven both directions.
- ✔ **Correctness boundary.** Graph + vector operate only over ingested knowledge; every node/edge carries a `source_id` back-reference. Live records remain `lookup_property`'s job — the graph never ingests tool data.
- ✔ **Hybrid order (§3.4.1 RETRIEVE).** graph filter → vector ANN → graph-informed merge → `list[RetrievedChunk]` handed to the LLM. Same output shape as Arc 11, so grounding + escalation + context assembly are unchanged.

**Vision §3.3 scoping / Architecture §3.7 (isolation walls):**
- ✔ Instance-default + admin-shared (via sibling) + **cross-admin never**. Both knowledge_chunks and the new graph tables enforce this at the RLS fence.
- ✔ Graph tables born with the correct RESTRICTIVE (tenant + instance) + PERMISSIVE base-grant posture, FORCE RLS. Verified by the RLS fuzz suite (now covering them) and a cross-tenant leak test.

**Vision §3.3 / Architecture §3.2.2 (raw knowledge view / trust contract):**
- ✔ The admin raw-view surface (`admin_knowledge.py`) is chunk-only and references no graph tables — unchanged by ARC 16. The hybrid path only ever returns a subset/reordering of stored chunks, so "admins see what Luciel sees" holds by construction.

---

## 3. Drift found and fixed (alignment debt this arc cleared)

ARC 16 was the first work to run several never-executed live tests, which surfaced **three real pre-existing defects** (none introduced by ARC 16), all fixed:

1. **Cross-tenant knowledge hole** (`arc16_a`). `knowledge_chunks` had a PERMISSIVE `admin_id IS NULL OR …` policy — a cross-tenant read carve-out contradicting Vision §3.3 "Across Admins: never." Flipped to RESTRICTIVE strict-tenant; the dead "global" union leg removed from the retriever + `search_similar`. Proven: a NULL-admin chunk is now invisible to every tenant.
2. **`knowledge_sources` deny-all bug** (`arc16_b`). The table had only RESTRICTIVE policies and **zero PERMISSIVE** → deny-all for every tenant under the real `luciel_app` role. This would have broken the admin raw-view source list the moment retrieval went live. Added the missing PERMISSIVE base-grant (mirroring `instances`). Proven: tenants see their own sources, only their own, still can't write others'.
3. **Live test harness rot.** The Arc 11 live RLS suite + the arc9 RLS fuzz suite had **never run** (stale `admins.email` column, non-parameterizable `SET LOCAL = %s`, `polname` vs `policyname`, psycopg2-vs-psycopg driver, incomplete GUC binding, Starlette-Request mock, stale `knowledge_embeddings` table name). All fixed. **The arc11 RLS suite is now 10/10; the arc9 fuzz suite is 4/4 and now covers the graph tables.**

---

## 4. System-consistency checklist (Behavioral Contract #7)

- ✔ **Schema matches model.** `alembic upgrade head` runs clean against real Postgres; `knowledge_graph.py` models match the `arc16_c` tables; round-trips up/down.
- ✔ **Migration integrity.** Single head, 130 migrations, no multiple-heads, no unapplied drift.
- ✔ **Hybrid retrieval live + tested E2E.** graph→vector→merge proven; vector-only fallback proven; never-raise proven (graph failure degrades, doesn't crash).
- ✔ **Raw knowledge view accurate.** Chunk-only surface, untouched by graph layer.
- ✔ **Isolation preserved + extended.** HNSW index + scope composite intact on chunks; graph tables pass the same FORCE-RLS fuzz invariants as all 17 other tenant tables.
- ✔ **Retrieval contract #4.** Every retrieved chunk carries `(admin_id, instance_id, source_id)`; every graph node carries the same triple.
- ⚠ **Frontend ↔ backend contracts:** ARC 16 adds **no new API endpoints** and changes no existing response shape (the hybrid swap is internal to `_retrieve`; the admin raw-view API is unchanged). So there is no new frontend surface to align for this arc. The admin graph-management UI (viewing/editing extracted entities) is **not in ARC 16 scope** — it is a future arc. Flagged so it is not mistaken for a gap.
- ⏸ **Deployed image / live RDS / env vars:** deferred by founder directive (no deploy until full-system demo readiness). Deploy-time alignment is the runbook (§7), not this arc.

---

## 5. Regression proof (no new breakage)

The full knowledge + runtime + db suite was run with ARC 16 changes **stashed** (clean Arc 15 tree): the same 8 failures + collection errors reproduced (the C6.3 ops-session tests, `test_alembic_head_is_arc12b`, the arc9 fuzz suite pre-fix, and `test_c6_4_ops_role_behavioural` collection error). **These are pre-existing never-run-live test debt, independent of ARC 16.** With ARC 16 restored, all ARC-16-adjacent suites pass and the arc9 fuzz suite (which I repaired) now passes 4/4.

**Pre-existing issues NOT in ARC 16 scope (flagged, not fixed):**
- `tests/db/test_c6_3_ops_session.py` — 7 failures (ops-session engine/GUC tests, never run live).
- `tests/db/test_c6_4_ops_role_behavioural.py` — ImportError at collection.
- (`test_alembic_head_is_arc12b` was the one head-tracking test legitimately affected by my migrations; updated to `arc16_c` and passing — see §6 item 1.)

---

## 6. Honest open items

1. **`test_alembic_head_is_arc12b` head pin — FIXED.** This test tracks the single live head; my migrations advanced it to `arc16_c`, so I updated the pin (the test's own design is to track the current head). Passes.
2. **LLM extraction — live model not exercised here.** `LLMEntityExtractor`'s parse path is fully tested with stubs (clean JSON, markdown-fenced, malformed→empty, exception→empty). The actual model call needs a real key; validated via the runbook (§7). The deterministic population path (resolution, dedup, attribution, supersede, never-raise) is fully live-tested.
3. **2 internal-retrieve tests skip** without a real `OPENAI_API_KEY` (they embed the query). Honest skip, not failure.

None of these is a code defect; all are external-dependency or out-of-subscope-test items.

---

## 7. Your-machine runbook (to reproduce + the deploy-readiness path)

**Reproduce the full ARC 16 build locally (matches what was verified in the sandbox):**
```bash
# 1. Local Postgres 16 + pgvector (your compose file pins pgvector/pgvector:pg16)
docker compose up -d   # NOTE: rebuild the 5-week-old luciel-postgres dev container fresh;
                       # its fixtures predate the current migration head.

# 2. Apply the full chain (includes arc16_a/b/c)
export DATABASE_URL="postgresql+psycopg://postgres:postgres@localhost:5433/luciel"
alembic upgrade head            # head == arc16_c_knowledge_graph_store

# 3. Run the live suites (real Postgres)
export LUCIEL_LIVE_POSTGRES_URL="postgresql://postgres:postgres@localhost:5433/luciel"
pytest tests/db/test_arc16_*_live.py tests/db/test_arc11_knowledge_rls.py \
       tests/db/test_arc9_ws4b_rls_fuzz.py tests/knowledge/test_arc16_llm_extractor.py

# 4. Validate the LLM extraction end-to-end (the one piece needing a key)
export OPENAI_API_KEY="<your real key>"
pytest tests/db/test_arc11_internal_retrieve_live.py   # the 2 embedder-gated tests run now
# + an ad-hoc ingest with LLMEntityExtractor wired to your LLM client to eyeball extracted entities
```

**Deploy-readiness alignment (when you choose to deploy, per your directive):**
At that point, the remaining alignment items are exactly the ones this report could not verify from a local sandbox: deployed image == merged branch, live RDS schema == `arc16_c`, env vars (notably `knowledge_retrieval_enabled` — still defaults False; flipping it turns on retrieval for every tenant and should be a deliberate, observed rollout), and a smoke check that the admin raw-view + a structured-filter query behave per the Customer Journey for Pro/Enterprise tiers.

---

## 8. The `knowledge_retrieval_enabled` flag (unchanged from the audit)

Still defaults `False`. ARC 16 is built behind the same flag as Arc 11 — the hybrid path is dark until the flag flips. Recommendation stands: land flag-off, validate via the internal endpoint + these live tests, and treat the flag flip as its own gated rollout. The flip turns on BOTH Arc 11 vector retrieval and the ARC 16 hybrid path simultaneously for every tenant.
