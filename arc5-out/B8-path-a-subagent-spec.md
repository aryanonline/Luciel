# Arc 5 B8 — Path A subagent spec

**Branch:** `arc-5-path-a` (already created and pushed; tracking `origin/arc-5-path-a`).  
**Base commit:** `e273614`.  
**Working directory:** `/home/user/workspace/luciel`.  
**Repo:** `aryanonline/Luciel` on GitHub.  
**Doctrine reference:** `docs/DRIFTS.md` → `D-arc5-aggressive-cleanup-doctrine-amendment-2026-05-23` (the parent), `D-arc5-b2-incomplete-instance-service-not-collapsed-2026-05-23` (the work-unit driver — read this stanza top-to-bottom before you touch any code).

## Scope (locked — DO NOT EXCEED)

Execute the V2 hierarchy collapse: delete `scope_level`, `scope_owner_tenant_id`, `scope_owner_domain_id`, `scope_owner_agent_id`, and all `Agent` / `DomainConfig` references from `app/` and `tests/`, then delete the `app/models/aliases.py` compatibility shim. The V2 doctrine is `Admin → Instance → Lead`. There is no Domain layer. There is no Agent layer. Every `Instance` hangs off exactly one `Admin` via `admin_id`. Every Admin gets exactly one Instance on Free/Pro signup; Enterprise gets exactly one minted at provisioning time too (the multi-instance allowance is for self-serve later creation, not at signup).

## What this scope does NOT include

- **Do NOT author Revision C migration** — that's the next main-turn commit.
- **Do NOT touch prod** — no AWS calls, no boto3, no `docker`/`buildah`, no environment variables.
- **Do NOT push to `main`** — only push to `arc-5-path-a`.
- **Do NOT add new business behavior** — this is a pure collapse, not a feature.
- **Do NOT modify the 5 files already committed at `e273614`** — `subscription.py`, `billing_service.py`, `billing_webhook_service.py`, `invite_service.py`, `entitlements.py` are V2-correct. The exception is `invite_service.py:570` where the `Agent(...)` call needs to be replaced — the V2 invite flow mints a `ScopeAssignment` binding the invited user to the existing `Admin`, no `Agent` row needed.

## Vision recap (locked — internalize before coding)

- **V2 tiers:** Free, Pro, Enterprise.
  - Free: 1 instance, 1 seat, 100 leads/mo, no Stripe customer, flat $0, 30-day audit.
  - Pro: 10 instances, 25 seats, 5000 leads/mo, flat monthly, 365-day audit.
  - Enterprise: unlimited, hybrid billing, contract audit, SSO, composition + sharing.
- **V2 hierarchy:** `Admin (1) → Instance (n)` where n ∈ {1 if Free, ≤10 if Pro, ∞ if Enterprise}. `Instance (1) → Lead (n)`. Nothing else.
- **What dies:** `Tenant`, `Domain`, `Agent`, `LucielInstance` (renamed to `Instance`), all `scope_level` / `scope_owner_*` columns, all `DomainConfig` references, `domain_id` FK columns on the 5 surviving tables that still have them (`conversations`, `identity_claims`, `scope_assignments`, `sessions`, `user_invites`).
- **What survives via direct collapse:** `Admin` (was `TenantConfig`), `Instance` (was `LucielInstance`).
- **Aliases shim contract:** `app/models/aliases.py` currently re-exports `Tenant=Admin`, `TenantConfig=AdminConfig`, `LucielInstance=Instance`, plus `_RemovedV1Class` sentinels for `Agent` and `DomainConfig`. **Delete this shim entirely at the end** — every consumer must import directly from `app.models.admin` / `app.models.instance`.

## File-by-file work plan (in commit order)

### Commit A1 — `app/repositories/luciel_instance_repository.py` rewrite

- Rename file to `app/repositories/instance_repository.py` (use `git mv`).
- Replace `LucielInstanceRepository` class name with `InstanceRepository`.
- Drop `scope_level` + `scope_owner_tenant_id` + `scope_owner_domain_id` + `scope_owner_agent_id` from every method signature, every SQL filter, every audit-row column. Replace with `admin_id` (str, FK to `admins.id`).
- Drop the `validate_parent_scope_active` helper entirely. V2 has no parent-scope-active concept; the `admin_id` FK is RESTRICT-on-delete and the model carries `active: bool`. Validation collapses to `select(Admin).where(Admin.id == admin_id, Admin.active.is_(True))` or simply trusting the FK.
- The `create()` method becomes: `create(self, *, admin_id, instance_slug, display_name, description=None, active=True, audit_ctx, autocommit=True) -> Instance`.
- The `get_by_pk()` / `get_by_admin_and_slug()` / `list_for_admin()` methods replace the legacy lookup paths.
- Update `app/repositories/__init__.py` if it has an explicit export list.
- Imports update: `from app.models.instance import Instance` (not via aliases).
- Commit message format: `arc5 Commit A1 (Path A): collapse luciel_instance_repository → instance_repository (V2 admin_id surface)` with a body that lists every public method renamed and references `D-arc5-b2-incomplete-instance-service-not-collapsed-2026-05-23`.

### Commit A2 — `app/services/instance_service.py` rewrite

- Mirror the new repo signature. `InstanceService.create_instance(self, *, admin_id, instance_slug, display_name, description=None, audit_ctx, ...) -> Instance`.
- Drop `validate_parent_scope_active` calls.
- Drop the `scope_level` / `scope_owner_*` exception-translation branches.
- Keep `DuplicateInstanceError` (now triggered by `(admin_id, instance_slug)` unique constraint).
- Keep `ParentScopeInactiveError`? Drop it — rename to `InactiveAdminError` if needed by exactly one callsite (admin is soft-deleted, can't create instance). If no callsite uses it, delete outright.
- Imports update: `from app.models.instance import Instance`.

### Commit A3 — `app/schemas/luciel_instance.py` rewrite

- Rename file to `app/schemas/instance.py` (use `git mv`).
- Replace `LucielInstanceCreate` / `LucielInstanceRead` / `LucielInstanceUpdate` Pydantic models with `InstanceCreate` / `InstanceRead` / `InstanceUpdate`.
- Drop `scope_level` discriminator field, drop `scope_owner_tenant_id` / `scope_owner_domain_id` / `scope_owner_agent_id` fields, drop the discriminator validators.
- Add `admin_id: str` field (constrained per the `Admin.id` column shape — UUID string).
- Keep `instance_slug` validator (URL-safe slug, 2-100 chars).
- Update every importer.

### Commit A4 — `app/policy/scope.py` collapse or delete

- V2 has no scope hierarchy. The original `scope.py` enforces a 3-level `tenant > domain > agent` permission lattice.
- Replacement: a single predicate `validate_admin_owns_instance(db, *, admin_id, instance_pk) -> None | raises NotOwnedError`. If no callsite needs that predicate (the new repo can do the SELECT directly), **delete the file outright**.
- Audit the callsites first — `grep -rn 'from app.policy.scope\|app.policy.scope' app/` — and decide: collapse to predicate vs delete. Whichever you choose, the file should not contain `scope_level` anywhere.

### Commit A5 — `app/repositories/agent_repository.py` deletion

- Delete the file outright. The `agents` table drops in Revision C. The `Agent` ORM class was already deleted at B1. There is no V2 equivalent — the V2 hierarchy goes Admin → Instance directly, no Agent layer.
- Update any importers to use `Admin` or `Instance` queries instead.
- `app/repositories/__init__.py` update if applicable.

### Commit B1 — API route bodies

- `app/api/v1/admin.py`: rewrite `/admin/instances` (already path-renamed at B3) route bodies to drop `scope_level` query param + request-body fields. The new contract is `GET /admin/instances?admin_id=…&active=true` returns instances filtered by admin_id (today's auth dependency already resolves the calling admin's id from JWT — so the `?admin_id=` param may be dropped entirely if every list is scoped to the calling admin).
- `app/api/v1/admin_forensics.py`: same collapse for forensic reads.
- `app/api/v1/verification.py`: same collapse for verification surface.
- Imports update: drop `from app.models.aliases import SCOPE_LEVEL_*`.

### Commit B2 — Service consumers (admin + dashboard + api_key + chat)

- `app/services/admin_service.py`: drop the 3 inline `from app.models.aliases import Agent` imports + 7 `scope_level` refs. Rewrite the affected methods to V2 (admin-scoped queries, no Agent joins). If a method's entire purpose was scope-hierarchy traversal (e.g., `list_agents_in_domain`), delete the method outright.
- `app/services/dashboard_service.py`: drop Agent import; rewrite dashboard aggregations to be admin-scoped (count instances per admin, etc.).
- `app/services/api_key_service.py`: drop Agent import + scope_owner refs. API keys in V2 are bound to `(admin_id, instance_id)` — no domain/agent dimension.
- `app/services/chat_service.py`: drop Agent + scope refs. Chat in V2 is scoped to `(admin_id, instance_id)`.

### Commit B3 — Tier provisioning + invite + worker

- `app/services/tier_provisioning_service.py` **full rewrite per V2:**
  - Drop the v1 tier imports (`TIER_INDIVIDUAL/TEAM/COMPANY`).
  - Drop `from app.models.aliases import Agent`.
  - Drop `from app.models.aliases import SCOPE_LEVEL_AGENT, SCOPE_LEVEL_DOMAIN, SCOPE_LEVEL_TENANT`.
  - Drop `_ensure_primary_agent` method entirely — there is no Agent layer in V2.
  - Drop the Company-tier Domain-scope branch (lines 350-369) and the Company-tier Tenant-scope branch (lines 373-392).
  - Rewrite `premint_for_tier(self, *, admin_id, tier, primary_user, audit_ctx)`:
    1. `tier in (TIER_PRO, TIER_ENTERPRISE)` validation. Free admins lazy-mint via signup elsewhere — this service is only for paying provisioning.
    2. `_ensure_owner_scope_assignment(admin, primary_user, audit_ctx)` — keep this, it's V2-correct.
    3. `self.instance.create_instance(audit_ctx=audit_ctx, admin_id=admin.id, instance_slug='primary', display_name=f'{admin.display_name} Luciel', description='Pre-minted at signup — primary instance.', created_by='tier_provisioning')` — exactly one instance, regardless of tier.
    4. Return `{'admin_id': admin.id, 'tier': tier, 'instance': {...}}`.
  - The `tenant_id` parameter becomes `admin_id`. `get_tenant_config()` becomes `admin_service.get_admin(admin_id)`. Variable rename `tenant` → `admin` throughout.
- `app/services/invite_service.py` line 570 area: drop the `Agent(...)` constructor call. V2 invite flow:
  1. User accepts invite.
  2. Mint `ScopeAssignment(user_id, admin_id, role=invite.role)`.
  3. No Agent row created. No Domain context.
- `app/worker/tasks/memory_extraction.py`: drop `from app.models.aliases import Agent`. Rewrite the memory-extraction task to operate on `Instance` scope only — `session.query(Instance).where(Instance.id == instance_pk)`.

### Commit C1 — Verification harness (5 pillars + fixtures)

- `app/verification/tests/pillar_02_scope_hierarchy.py`: the entire purpose of this pillar was to assert the 3-level hierarchy. **Delete the file outright** — no V2 equivalent. Update `verification/__main__.py` to not register pillar_02.
- `app/verification/tests/pillar_04_chat_key_binding.py`: collapse to admin+instance binding only. Drop scope_owner / scope_level assertions. The V2 binding is `(admin_id, instance_id)`.
- `app/verification/tests/pillar_08_scope_negatives.py`: rewrite to assert cross-admin isolation (admin A's instances are not visible to admin B). Drop scope_level negatives — they don't exist.
- `app/verification/tests/pillar_21_cross_tenant_scope_leak.py`: rename concept "cross-tenant" → "cross-admin"; collapse to admin-vs-admin isolation. Drop domain/agent dimensions.
- `app/verification/tests/pillar_25_worker_pipeline_liveness.py`: scope the worker test to admin_id only. Drop `scope_level` / `scope_owner_*` parameters from fixtures.
- `app/verification/fixtures.py`: rewrite fixture factories to mint Admin + Instance only. Drop Agent / Domain fixture factories outright.
- `app/verification/__main__.py`: drop pillar_02 registration; update fixture-prep flow.

### Commit C2 — Delete `app/models/aliases.py` outright

- Verify no remaining `from app.models.aliases` imports across `app/`, `tests/`, `scripts/`, `worker/`. Run `grep -rn 'app.models.aliases' .` and confirm zero hits.
- `git rm app/models/aliases.py`.
- If any historical references remain in comments or docstrings, update them to mention "deleted at Arc 5 Commit C2".

### Commit D1 — Final validation pass

- Run `python3 -c "import app"` — must exit 0.
- Run `python3 -m ast app/**/*.py` (via find loop) — every file must AST-parse.
- Run `grep -rnE "scope_level|scope_owner_(tenant|domain|agent)_id|TIER_(INDIVIDUAL|TEAM|COMPANY)|from app.models.aliases|DomainConfig|class Agent\b" app/ tests/ scripts/ worker/ --include="*.py"` — every hit reported in commit-message body. **Zero hits outside historical-migration files (`alembic/versions/`) and comments is the closure bar.**
- Commit message lists every commit on the branch in order and references the parent drift.

## Test fixture and historical migration boundaries

- **Historical migrations** (`alembic/versions/0*-c*.py`) are immutable — do NOT touch them. They reference `scope_level` / `Agent` / `Domain` legitimately because they migrated forward from those shapes. The V2 collapse is forward-only.
- **Arc 5 migrations** (`alembic/versions/arc5_a_*.py`, `alembic/versions/arc5_b_*.py`) — also do NOT touch. Revision C will be authored in the main turn after merge.
- **`alembic/env.py`** — do NOT touch unless it imports from `aliases.py`.

## Commit discipline (non-negotiable — partner gate)

- **One commit per work-unit above** (A1 through D1 = 11 commits total).
- **After every commit, run `python3 -c "import app"`** with `DATABASE_URL=postgresql://stub` env. If it fails, do NOT proceed to the next commit — fix the import first.
- **After every commit, run `git push origin arc-5-path-a`** so the partner can see progress incrementally.
- **Commit signing:** `git -c user.name="Aryan Singh" -c user.email="aryans.www@gmail.com" commit -m "..."`.
- **Push:** `git push origin arc-5-path-a` (NOT main).
- **Stop condition:** D1 lands with zero V1 grep hits and `import app` exits 0. Do NOT author Revision C. Do NOT touch prod. Do NOT merge to main. Return control to main turn.

## Failure mode

If any commit fails import validation and you cannot fix it within 3 attempts on the same file:

1. Save the failing file's diff to `arc5-out/path-a-stuck-{commit}.diff` via `git diff > arc5-out/path-a-stuck-{commit}.diff`.
2. Save the import-error traceback to `arc5-out/path-a-stuck-{commit}.traceback.txt`.
3. Commit what you have on a sub-branch `arc-5-path-a-stuck-{commit}`.
4. Exit with a `WORK-UNIT-STUCK-AT-{commit}` summary listing what's been committed, what's stuck, and what evidence is in the `arc5-out/path-a-stuck-{commit}.*` files.

Do NOT silently exit. Do NOT skip files. Do NOT plow through a broken import. The partner will pick up the stuck work-unit in the main turn and surgically resolve.

## Vision-mode reminders

- Aggressive cleanup posture — delete first, ask questions never. The doctrine has chosen the V2 shape; preserving v1 shapes is incoherence.
- Symmetry between code, docs, and prod is sacred. When you delete a thing in code, the corresponding line in DRIFTS / CANONICAL / ARCHITECTURE either gains a strikethrough or stays alive intentionally (the parent drift owns CANONICAL/ARCHITECTURE edits, not this work-unit).
- Six pillars discipline: scalability, reliability, maintainability, traceability, security. The collapse improves all five — fewer concepts, fewer paths, fewer surfaces, stricter shape.
- Address the partner as "partner" in commit-message bodies if you need to reference his direction.

## Recovery context (in case of compaction mid-subagent)

- Branch: `arc-5-path-a`.
- Last commit before subagent starts: `e273614`.
- Parent drift in DRIFTS.md: `D-arc5-b2-incomplete-instance-service-not-collapsed-2026-05-23`.
- Aliases shim file to delete at C2: `app/models/aliases.py`.
- New V2 ORM classes (already exist): `app/models/admin.py` (`Admin`, `AdminConfig`), `app/models/instance.py` (`Instance`).
- V2 tier constants (already committed): `app/models/subscription.py` (`TIER_FREE`, `TIER_PRO`, `TIER_ENTERPRISE`, `ALLOWED_TIERS`, `TIER_INSTANCE_CAPS`).
