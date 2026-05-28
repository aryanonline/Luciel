# Arc 11 — Drift From Doctrine

Items where the Arc 11 implementation deviates from
Vision / Architecture / Customer Journey, with a justification for
each deviation and a proposed amendment to the doctrine document on
the next revision cycle.

Source: `ARC11_PLAN.md` §0 reconciliations + per-step reports.

---

## D1 — Free tier role matrix: code implements all four roles uniformly

**Doctrine:** Vision v1 §7 reads `"Free: View + delete (single
role)"`.

**Implementation:** Code (Step 7) implements the full four-role
matrix (`admin_owner`, `admin_manager`, `instance_operator`,
`read_only_viewer`) on every tier. Free admins typically hold
`admin_owner`; the other three roles exist on the tier but the
single-seat ceiling (Vision §7: `"Cross-team isolation: n/a (1
seat)"`) means they're effectively unused.

**Justification:** Vision §7's "Cross-team isolation: n/a (1 seat)"
is the operative constraint — a single-seat tenant only ever has
one role. The line about "(single role)" reads as a description of
the seat constraint, not a structural difference in how Free is
implemented. Implementing the full matrix uniformly is simpler than
a tier-conditional role schema, and the seat ceiling enforces the
same behaviour.

**Suggested doctrine amendment:** VISION_v2 should reword the Free
row to read `"Four-role matrix; Free admins typically hold
admin_owner (single seat)."` This kills the apparent contradiction
without changing the user-visible behaviour.

---

## D2 — `knowledge_sources` RLS is RESTRICTIVE; `knowledge_chunks` is PERMISSIVE

**Doctrine:** Architecture v1 §3.7.1 + Arc 9 RLS doctrine
(`alembic/versions/arc9_c11_tenant_restrictive.py`) say "RESTRICTIVE
when admin_id is NOT NULL; PERMISSIVE when nullable for legacy
compat" — but this is implicit; the doctrine document doesn't
explicitly codify the rule.

**Implementation:** Step 4's `arc11_d1_rls_knowledge_sources.py`
installs a RESTRICTIVE policy on `knowledge_sources` (where
`admin_id` is NOT NULL by schema). The pre-existing
`knowledge_embeddings_tenant_isolation` policy on
`knowledge_chunks` (renamed post-Step-2) remains PERMISSIVE because
`admin_id` is nullable there for legacy platform-curated rows.

**Justification:** The asymmetry is intentional and correct. Arc 9
C11 explicitly excluded `knowledge_embeddings` from the strict-tenant
flip for the NULL-permissive read carveout. Step 4's static-shape
test (`tests/security/test_arc11_rls_migrations_shape.py`) locks the
choice.

**Suggested doctrine amendment:** ARCHITECTURE_v2 should codify the
rule explicitly in §3.7.5: `"Rule of thumb: a customer-data table's
RLS policy is RESTRICTIVE when its tenant-key column is NOT NULL,
and PERMISSIVE (with the NULL-permissive carveout) when the column
is nullable for legacy compatibility."` This makes the asymmetry
discoverable from the doc instead of requiring a code-archaeology
dive.

---

## D3 — Retriever wiring is Arc 11, not Arc 14

**Doctrine conflict at plan-time:** The Arc 11 brief in the thread
said "wiring the retriever into the live agentic loop — ARC 14."
Architecture v1 §6 says Arc 11 ships **"retriever-into-orchestrator
wiring."**

**Implementation:** Step 8 (`arc11/h-orchestrator`) ships the
retriever wiring behind a feature flag that defaults closed
(`knowledge_retrieval_enabled = False`). Arc 14 owns the full
agentic loop (PLAN / ACT / REFLECT, escalation judgment, tool
dispatch) and the flag flip.

**Justification:** Architecture v1 wins per Vision §10 ("if code,
doctrine, or roadmap diverges from this vision, this document
wins"). Plan §0.1 reconciled the brief vs Architecture explicitly.

**Suggested doctrine amendment:** No change needed. The
Architecture document is correct; the original brief was wrong.
Flag the brief language for cleanup if/when it gets refactored
into a v2 brief format.

---

## D4 — `KnowledgeSource.luciel_instance_id` is BIGINT; `instances.id` is INTEGER

**Doctrine:** ARC11_PLAN.md §2.1 specifies
`luciel_instance_id BIGINT NOT NULL REFERENCES instances(id) ON
DELETE RESTRICT`.

**Implementation:** Followed the plan literally. The FK target
column is `INTEGER` (`SERIAL`), so Postgres implicitly upcasts on
join.

**Justification:** The plan specifies BIGINT. Postgres accepts the
mixed-width FK silently (no correctness issue). Step 1 flagged the
inconsistency as a minor finding.

**Suggested doctrine amendment:** Tighten ARC11_PLAN.md §2.1 to
`luciel_instance_id INTEGER` to match every other reference to
`instances.id` in the codebase. Or alternatively migrate
`instances.id` to BIGINT in a future arc — but that's a destructive
schema change for a non-problem, so the doc-side tightening is
cheaper.

---

## D5 — Knowledge mounted in existing Configure tab, not a 5-pillar layout (frontend)

**Doctrine:** Customer Journey §4.4 / §10.4 / §18.4 describe a
"5-pillar configuration screen" (Channels, Tools, Knowledge,
Escalation, Personality).

**Implementation:** Step 9 (`arc11/knowledge-base-ui` in
Luciel-Website) mounts Knowledge inside the existing 3-tab layout
(Configure / Test / Deploy) on `LucielInstanceDetail.tsx`.
Channels / Tools / Escalation / Personality sections do NOT exist
yet — they're owned by Arcs 12-15.

**Justification:** The 5-pillar layout is Arc 15's job per the
roadmap. Arc 11 owns Knowledge functionality; bundling the layout
refactor would have doubled the frontend diff for no functional
gain. Explicit `TODO(Arc-15)` comment is in
`LucielInstanceDetail.tsx`.

**Suggested doctrine amendment:** No change. Arc 15 implements the
5-pillar layout; the Journey doc describes the eventual end state.

---

## D6 — `POST /sources` accepts paste text as `Form(text=...)` not JSON body

**Doctrine:** ARC11_PLAN.md §3.1 says "Accepts `multipart/form-data`
(file upload) OR `application/json` with a `{\"text\": \"...\"}`
body (paste)."

**Implementation:** Step 7 made both branches multipart — file
upload via `File()` and paste via `Form(text=...)`. The frontend
submits either field in the same form.

**Justification:** Mixing JSON bodies and multipart on the same
FastAPI route is awkward (FastAPI requires you to switch between
`Body(...)` and `Form(...)` decorators per branch). Step 7 flagged
the deviation explicitly; the frontend (Step 9) was built against
the multipart-only contract.

**Suggested doctrine amendment:** Update ARC11_PLAN.md §3.1 to say
"Accepts `multipart/form-data` with either a `file` field or a
`text` field." Or alternatively split into two routes
(`POST /sources` for file, `POST /sources/text` for paste JSON) —
but that's a backwards-incompatible change to the now-shipped
Step 7 API and the Step 9 frontend, so the doc-side amendment is
cheaper.

---

## Cross-cutting note

D1, D3, D4, D5, D6 are doctrine-side amendments — the
implementation is right; the doc(s) need to catch up.

D2 is a doctrine codification — the implementation is right; the
doctrine document should make the rule explicit so the next
implementer doesn't have to re-derive it.

Nothing here is a behavioural drift that needs code rollback.
