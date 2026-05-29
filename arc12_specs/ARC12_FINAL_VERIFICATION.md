# ARC 12 — Final System Verification Record

This supersedes the test/verification section of ARC12_CLOSEOUT.md. Every claim below was executed by the agent in a provisioned environment (Postgres 17 + Redis + the running app + the website build) — NOT taken from subagent reports.

Branch: `arc12/tool-registry-sibling-byo` (backend) + `arc12/tool-ui` (website). Single Alembic head: `arc12_ex4_reseal_audit_chain_drop_agent_domain`.

## Backend — verified live
| Check | Method | Result |
|---|---|---|
| Migrations apply on REAL Postgres | fresh DB + `alembic upgrade head` | EXIT 0 → head reached |
| Migration bisect vs Arc 11 baseline d4baf14 | ran both | baseline clean; branch had 2 Arc-12 bugs → FIXED (see below) |
| Migration reversibility | `alembic downgrade -1` → `upgrade head` | both EXIT 0 |
| Full unit suite | `pytest tests/` (sqlite) | 1923 passed / 0 failed / 61 skipped |
| App boots on real DB+Redis | `uvicorn app.main:app` | startup complete, 93 paths, /health 200 |
| RLS fail-closed (4 new tables) | `luciel_app` (NOBYPASSRLS), no GUC | 0 rows on all 4 tables |
| Cross-tenant isolation | seed adminA+adminB rows, switch `app.admin_id` | A sees only A, B sees only B |
| Wall-1 WITH CHECK | adminA inserts row tagged adminB | rejected: "new row violates row-level security policy" |
| Arc 12 routes registered + auth-enforced | live OpenAPI + unauth curl | 7 routes present; 401 unauthenticated |
| Audit append-only post-reseal | `luciel_worker` least-priv smoke | CHECK 0-3 PASS: SELECT/INSERT ok, DELETE/UPDATE refused |
| Audit hash chain verifies after reseal | runtime verifier over resealed rows | CHAIN VERIFIES (new field set) |
| BYO sandbox egress allowlist | live dispatch to non-allowlisted host | success=False, error_class=egress_denied |
| BYO real subprocess happy path | live dispatch to allowlisted local server | success=True (real subprocess spawned) |
| BYO output-schema rejection (no retry) | server returns schema-violating body | success=False, error_class=schema_output |

### Two release-blocking migration bugs found by the real-Postgres run and FIXED
1. **EX3 scope_assignment** — `bootstrap_identity` SECDEF function compared `scope_role` ENUM to string `'owner'` → `InvalidTextRepresentation`. Fixed to `'admin_owner'`. (sqlite never caught it — no enum enforcement.)
2. **EX4 reseal** — self-audit row used `admin_id='platform'` but no migration seeds the `platform` sentinel admin → `ForeignKeyViolation` on a fresh DB (would have failed the prod deploy mid-migration). Fixed by idempotent `ON CONFLICT DO NOTHING` seed of the sentinel admin before the reseal record.
Both bisect-proven Arc-12-introduced (baseline d4baf14 migrates clean).

## Frontend — verified in-environment
| Check | Result |
|---|---|
| `npm run build` (vite) | EXIT 0, no type errors |
| `npm run test` (vitest) | 146 passed / 0 failed (incl. ToolsSection ×10, SiblingGrantsSection ×3) |
| Contract parity | website TS client calls exactly the 7 backend routes confirmed live — no ghost routes either direction |
| domain_id contract drift | removed (WU8b) — no stale domain_id on swept contracts |

## Infrastructure — verified
| Check | Result |
|---|---|
| Arc 12 infra-as-code changes | ZERO (git diff d4baf14..HEAD on cfn/, infra/, td-*.json empty) |
| Env/SSM parity | latest backend task-def (rev78) carries all required secrets/env; Arc 12 added NO new required env (BYO reuses REDIS_URL; guardrails are in-code constants) |
| CFN templates | parse via cfn-lint; only pre-existing W2001 (unused-param) warnings + 1 false-positive E1050 on a description string — none Arc-12-introduced, none real defects |
| Stale artifact removed | UTF-16 `td-backend-rev45.json` (unreferenced, superseded by rev78, broken encoding) — deleted |
| EX4 reseal deploy note | recomputes admin_audit_logs row-by-row under advisory lock — run in a maintenance window if the table is large |

## Documents — reconciled (see DOC_RECONCILIATION.md)
Canonical docs are Space PDFs (founder-owned, not in repo). Provided verbatim amendment text for SETTLED items (BYO topology §4.1/§4.3 → in-container; three-layer scope retirement; composition-guardrails wording) and a clean list of GENUINE founder policy decisions (B1-B6) left undecided.

## Net state
- Backend, frontend, and infra-as-code are aligned with each other and with the documents, except the 6 items in DOC_RECONCILIATION Part B which are genuine founder POLICY decisions (not defects/drift). Every defect and every drift item that was mine to resolve has been resolved and re-verified.
- Nothing is deferred that was in Arc 12's scope. The branch is merge-ready pending: (a) founder decisions B1-B6, (b) applying the Part-A doc amendments, (c) merge + image rebuild + `alembic upgrade head` deploy (EX4 reseal in a maintenance window).
