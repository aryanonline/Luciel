# Baseline Test State — pre-change (2026-06-04)

## Environment (local, sandbox)
- PostgreSQL 17 + pgvector 0.8.0; DB `luciel`; `postgres/postgres@localhost:5432`.
- Redis 8 (daemonized).
- Python venv at `backend/.venv`, `pip install -e ".[dev]"` + `psycopg2-binary`.
- Migrations: `alembic upgrade head` → all 130 apply cleanly; single head `arc18_conversation_budget_metering`.
- Test env vars: `DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/luciel`,
  `REDIS_URL=redis://localhost:6379/0`, `MODERATION_PROVIDER=null` (documented dev-only),
  `ENABLE_STUB_LLM_PROVIDER=true` (e2e stub, dev-only), `CHANNELS_LIVE_PROVISIONING_ENABLED=false`,
  `MAIL_INBOUND_DOMAIN=luciel-mail.com`.
- Run: `python -m pytest -q --ignore=scripts -o addopts=""` (the `scripts/smoke_test.py` collection
  error is a live-HTTP-server script, not a unit test — correctly excluded).

## Result: 2446 passed · 5 failed · 66 skipped · 2517 collected

## The 5 failures are ALL pre-existing maintenance artifacts, NOT product defects:
1. `test_arc18_invoice_paid_reset … test_overage_reported_with_rounded_units_then_counter_reset`
   — **Order-dependent flake.** Passes 5/5 when the file runs alone; fails only in full-suite due to
   shared Redis/DB state. Product code is correct. → fix: test isolation (flush key in fixture).
2. `tests/db/test_arc12b_custom_roles_migration.py::test_alembic_head_is_arc12b`
   — **Stale head pin.** Asserts head==`arc15_c`; real head is `arc18`. The test's own docstring says it
   "tracks the current head" but was never updated past Arc 15. → fix: update pin to `arc18` (and make
   it head-agnostic via `ScriptDirectory.get_current_head()` to stop recurring).
3-5. `tests/integrity/test_arc11_audit_script.py` (3 tests) — the `scripts/arc11_*` audit script exits 1.
   Same class: the script pins an expected alembic head / expects conditions not present locally.
   → fix: align the script's head expectation; make head-agnostic.

## CI reality (manifest §06 confirmation)
`.github/workflows/ci.yml` is intentionally "backend-free": it runs only AST tests + a `/health` import,
NOT this live suite. The real verification gate is the `luciel-verify` ECS task running
`python -m app.verification` against a deployed backend. This is why the live suite's head-pins drift —
nothing in CI exercises them. The §5.5 smoke-probe suite + rollback gate (manifest TIER infra) is the
intended automated post-deploy gate and does not yet exist as code.

This baseline is the green reference. Any post-change run must keep these 2446 green and additionally
turn the 5 artifacts green (they are in scope as "in-sync" test-maintenance drift).
