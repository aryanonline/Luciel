# Arc 11 — Cleanup Candidates

Items the team deliberately left for later because bundling them
into Arc 11 would have doubled the blast radius for no doctrinal
gain. Each entry shows where it was found, why it was deferred, and
the suggested arc / PR shape to handle it.

Sources: `ARC11_PLAN.md` §11 (locked deferrals) + §13 (per-step
carry-forward).

---

## 1. Legacy `agent_id` column + read-compat code

- **Found in:** §11 Q3 LOCKED decision, surfaced during Step 3
  refactor planning.
- **Context:** `KnowledgeChunk.agent_id` is a `String(100)` column
  woven through `retriever.py`, `knowledge_repository.py`, and
  `ingestion.py` on the read-side compat path for pre-Step-24.5
  rows. Zero such rows exist in production.
- **Why deferred:** Removing it requires updates to three modules
  plus a behavioural refactor of the search inheritance fan-out.
  Bundling that with the Arc 11 structural split would double the
  Arc 11 blast radius for no doctrinal gain.
- **Suggested fix:** A dedicated post-Arc-11 PR (sequence between
  Arc 11 close and Arc 12 start) that drops the column AND the
  read-compat branches together. The model docstring already marks
  the column DEPRECATED; the cleanup PR makes it real.

---

## 2. `KnowledgeChunk.source_record` → `source` relationship rename

- **Found in:** Step 1 report (relationship name collision with the
  pre-existing legacy free-text `source` string column).
- **Context:** `KnowledgeChunk` already has a `source: Mapped[str |
  None]` (legacy free-form human-readable reference column). Arc 11
  Step 1 added the new FK relationship and named it `source_record`
  to avoid the attribute collision.
- **Why deferred:** Renaming the relationship requires the legacy
  string column to be dropped first (item #5 below). They unblock
  each other.
- **Suggested fix:** Pair with item #5. Once the column is gone,
  rename `KnowledgeChunk.source_record` → `KnowledgeChunk.source`
  with a single grep-and-replace across the repo.

---

## 3. `"knowledge_embeddings"` `data_category` string

- **Found in:** Step 2 report (rename triage).
- **Context:** The string `"knowledge_embeddings"` is still used as
  a stable `data_category` identifier across
  `app/policy/retention.py` (dict key),
  `app/policy/retention_rules.py`,
  `app/schemas/retention.py`,
  `app/models/retention.py`, and
  `app/services/onboarding_service.py`. It is also persisted in
  `retention_policies.data_category` rows in production.
- **Why deferred:** Renaming the identifier requires a paired data
  migration: `UPDATE retention_policies SET data_category =
  'knowledge_chunks' WHERE data_category = 'knowledge_embeddings'`,
  plus a Python-side rename of the constants, in the same release.
- **Suggested fix:** Post-Arc-11 PR (or as part of Arc 11 close
  audit if zero production rows exist with the legacy
  `data_category`). The migration is one-line; the code side is a
  grep-and-replace. Verify with a paired alembic test.

---

## 4. Legacy stringy `source_id` dual-write (`f"src-{source_pk}"`)

- **Found in:** Step 3 report (ingestion dual-write convention).
- **Context:** During the Arc 11 cutover, every chunk row carries
  BOTH the new `source_fk` (BIGINT FK to `knowledge_sources.id`) and
  the legacy stringy `source_id` (synthesised as `f"src-{pk}"` when
  the caller doesn't supply one). Lets legacy read paths
  (`data_export_service`, `downgrade_archive_service` fallback
  branches) keep working until the column drop.
- **Why deferred:** The legacy column drop (item #5) blocks this.
- **Suggested fix:** When item #5 lands, also stop writing the
  synthesised `src-{pk}` strings from `IngestionService._ingest_text`.
  Grep for the literal `f"src-{...}"` after item #5 — only the
  ingestion + worker remain.

---

## 5. Legacy stringy `source_id` column drop + `source_fk` rename

- **Found in:** ARC11_PLAN.md §2.2 step 5 (originally planned as
  "Step 11"); Step 8 report carry-forward; Step 9 report.
- **Context:** The original plan reserved "Step 11" inside Arc 11
  for: drop the legacy `source_id` String column on
  `knowledge_chunks`; rename `source_fk` → `source_id`; set NOT
  NULL. As the arc progressed, Step 11 became the
  production-deploy step instead. The cleanup is now a post-Arc-11
  PR.
- **Why deferred:** Bundling a destructive schema change with the
  production deploy step would couple two distinct risks (deploy
  rollback + schema rollback). Cleaner to ship Arc 11 dormant,
  flip the flag at Arc 14, run for some interval, then drop the
  legacy column.
- **Suggested fix:** New arc (call it "Arc 11.5 cleanup") or
  bundle into Arc 14's open-flag work:
  1. New alembic migration `arc115_drop_legacy_source_id.py` that
     drops the column, renames `source_fk` → `source_id`, sets NOT
     NULL.
  2. Update `app/repositories/knowledge_repository.py` to use the
     new column name.
  3. Update `app/services/data_export_service.py` to remove the
     legacy fallback branches (Step 3 left them).
  4. Update `app/services/downgrade_archive_service.py` likewise.
  5. Drop item #4's dual-write logic from `IngestionService`.
  6. Drop item #2 here (rename `source_record` → `source`).

---

## 6. `sqlalchemy.ARRAY` → `dialects.postgresql.ARRAY` on `Trace.source_ids_used`

- **Found in:** Step 5 report (cosmetic — `.contains()` ergonomics).
- **Context:** `Trace.source_ids_used` is declared with the generic
  `sqlalchemy.ARRAY(BigInteger)`. The generic type doesn't expose
  `.contains()` at the SQL layer; Step 5's
  `TraceRepository.list_recent_traces_using_source` works around it
  with `.op("@>")(cast([id], ARRAY(BigInteger)))`.
- **Why deferred:** Purely ergonomic — the workaround compiles to
  the same SQL and the same query plan. No correctness issue.
- **Suggested fix:** Tiny PR that swaps the import on
  `app/models/trace.py` from `sqlalchemy.ARRAY` to
  `sqlalchemy.dialects.postgresql.ARRAY(BigInteger)`. Once that
  lands, change the repository's `.op("@>")(...)` call to the
  `.contains(...)` shape. No schema migration required.

---

## 7. `ScopeAssignment.role` → PG enum promotion

- **Found in:** Step 7 report (role-gating gotcha).
- **Context:** `ScopeAssignment.role` is a free-form `String(100)`
  per Step 24.5b doctrine. Arc 11 introduces four canonical role
  values (`admin_owner`, `admin_manager`, `instance_operator`,
  `read_only_viewer`) per Architecture §3.2.2 and stores them as
  plain strings. No DB-level enum constraint;
  `_resolve_role_on_instance` fail-closes on unknown strings.
- **Why deferred:** Step 24.5b explicitly deferred the enum
  promotion to "when role taxonomy stabilises." Arc 11 codifies the
  four canonical names; Arc 12 (which adds the
  `external_auditor` / `office_manager` custom roles per Journey
  §16) will likely have more names to add. Promoting now would
  require a paired schema migration; doing it after Arc 12's role
  set stabilises is cheaper.
- **Suggested fix:** Plan a "role taxonomy enum promotion" PR in
  the Arc-12-to-13 gap. Schema migration adds a check constraint;
  Python side promotes the constants to a `StrEnum`.

---

## 8. `request.state.role` middleware population

- **Found in:** Step 7 report (role resolution path).
- **Context:** Today the auth middleware does NOT populate
  `request.state.role`. Step 7's `_resolve_role_on_instance` falls
  back to a `scope_assignments` SELECT keyed on `actor_user_id` +
  `admin_id`. Works fine for cookie auth (Step 24.5b populates
  `actor_user_id`); API-key paths require the fallback to be
  reliable.
- **Why deferred:** Populating role at the middleware layer is a
  cross-cutting concern that touches `app/middleware/auth.py` and
  `app/middleware/session_cookie_auth.py`, and the underlying lookup
  optimization (cache role per request) is its own design
  conversation.
- **Suggested fix:** Paired with item #7. When the role taxonomy
  stabilises, push the resolution into the middleware once-per-
  request so route handlers don't need to repeat the lookup.

---

## 9. `src/lib/admin.ts::request<T>` + `src/lib/knowledge.ts::_multipartRequest` consolidation (frontend)

- **Found in:** Step 9 report (Luciel-Website).
- **Context:** The frontend's existing `request<T>` helper is JSON-
  only. The new knowledge upload routes require multipart, so
  `knowledge.ts` added a parallel `_multipartRequest` helper. Both
  coexist; consolidation is a refactor PR's worth of work.
- **Why deferred:** Touching `admin.ts` for a refactor would have
  unnecessarily expanded the Step 9 diff and risked breaking the
  existing 96-test suite.
- **Suggested fix:** Frontend cleanup PR — unify the two helpers
  under a single function that branches on `body instanceof FormData`.

---

## 10. Pre-existing TypeScript errors on frontend `main`

- **Found in:** Step 9 report (Luciel-Website).
- **Context:** `tsc --noEmit -p tsconfig.app.json` reports 6 errors
  on `main` (in `CloseAccountSection.tsx`, `Dashboard.tsx`,
  `SignupFree.test.tsx`, `lifecycle.test.ts`). None introduced by
  Arc 11; all pre-date the arc.
- **Why deferred:** Out of scope for Arc 11 — these are pre-arc
  legacy issues. Listing here so the close auditor doesn't mistake
  them for Arc 11 regressions.
- **Suggested fix:** Standalone cleanup PRs for each file, separate
  from Arc 11.

---

## Cross-cutting note on close-audit posture

Items #1–#5 are coupled: item #5 unblocks #1, #2, #3, #4. The
cleanest path is one "Arc 11.5 cleanup" branch that lands all five
together, with a single alembic migration + a coordinated code
refactor. Items #6–#10 are independent of each other and the rest.
