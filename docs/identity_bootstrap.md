# Identity Bootstrap Doctrine — Arc 9 C22

**Status:** Authoritative as of Arc 9 C22 (May 2026).
**Owner:** Backend platform.
**Supersedes:** Arc 9 C20 inline-SECDEF pattern, Arc 9 C21 per-call SECDEF helpers.

---

## 1. Problem this layer solves

Before C22, identity discovery was scattered across at least five call sites
that each issued their own privileged read against the database to answer
"who is this user and what can they touch?":

- `app.api.v1.auth._resolve_tenant_for_user` (login)
- `app.middleware.session_cookie_auth` (per-request cookie auth)
- `app.repositories.scope_assignment_repository.list_for_user` (RLS-empty fallback)
- `app.services.billing_service` (Stripe-race fallback)
- `app.services.admin_service` (tier-aware entitlement checks)
- `app.services.user_service` (user lookup paths that touch scope)

Each site rolled its own SECDEF function, its own connection-pool handling,
its own RLS-empty-GUC fallback, and its own error semantics. Symptoms in
production:

1. Silent-empty reads when a caller forgot to set the tenant GUC (RLS
   filtered everything out, returning `[]` with HTTP 200).
2. Inconsistent fail-open vs. fail-closed semantics across endpoints.
3. Frontend HTTP 500 from `instance_service.list_for_scope` — a method that
   never existed on the V2 service surface (carried over by mistake from
   a pre-Arc-5 refactor).
4. Free-tier customers blocked at the tier guard because the guard
   assumed every admin had a `Subscription` row, which V2's lazy-create
   policy explicitly avoids.

C22 closes the loop by consolidating identity discovery into one named
boundary: `app.identity.bootstrap.IdentityBootstrap`.

---

## 2. The contract

```python
from app.identity import IdentityBootstrap, IdentitySnapshot

snapshot: IdentitySnapshot = IdentityBootstrap(db).resolve(user_id)
```

`IdentitySnapshot` is a frozen dataclass with these fields:

| Field                  | Type                       | Meaning                                                  |
|------------------------|----------------------------|----------------------------------------------------------|
| `user_id`              | `UUID`                     | Echo of the input — convenience for logging.             |
| `canonical_tenant_id`  | `str \| None`              | The admin_id this user resolves to. `None` = no scope.   |
| `canonical_tier`       | `str \| None`              | `"free" \| "pro" \| "enterprise"`. `None` = no Admin row.|
| `active_scopes`        | `tuple[ScopeAssignmentRow, ...]` | All non-ended scope rows owned by `user_id`.         |

`IdentitySnapshot.has_scope` and `IdentitySnapshot.canonical_role` are
convenience read-only properties derived from `active_scopes`.

### Wire format

`IdentityBootstrap.resolve` issues exactly one call to the SECDEF
function `arc9_c22_bootstrap_identity(uuid)`, which returns a table of:

```
canonical_tenant_id  text
canonical_tier       text
scope_assignment_id  uuid
tenant_id            uuid
domain_id            uuid
role                 text
started_at           timestamptz
ended_at             timestamptz
ended_reason         text
ended_note           text
ended_by_api_key_id  uuid
active               boolean
```

The first row carries `canonical_tenant_id` and `canonical_tier`; all rows
carry one scope assignment. If the user has no Admin row, the function
returns zero rows and the snapshot has `canonical_tenant_id = None`,
`canonical_tier = None`, `active_scopes = ()`.

The SECDEF function is owned by `luciel_ops` and has `GRANT SELECT ON
public.admins TO luciel_ops` (added in migration
`arc9_c22_identity_bootstrap`) so the tier join can complete inside the
function regardless of the calling session's RLS posture.

### Fail-closed semantics

| Situation                              | Behaviour                                                     |
|----------------------------------------|---------------------------------------------------------------|
| User does not exist                    | Snapshot with all-`None` fields; callers must treat as 401.   |
| User exists but has no Admin row       | `canonical_tier = None`, `active_scopes = ()`. Treat as 402.  |
| User exists, Admin row, no Subscription| `canonical_tier = "free"` (read from `Admin.tier`).           |
| User exists, paid Subscription         | `canonical_tier` reflects `Subscription.tier`.                |
| GUC unset at call site                 | Snapshot is still complete — SECDEF bypasses RLS by design.   |

The doctrine is: **no caller in the stack may invent identity facts.**
If you need to know who someone is or what tier they have, ask the
bootstrap. If the bootstrap says "no scope," do not paper over it.

---

## 3. The Free-tier corollary

`Subscription` rows exist only for paid tiers (V2 lazy-create doctrine).
Free admins never have a Subscription. This means every tier-aware code
path must:

1. Read `canonical_tier` from `IdentitySnapshot` (preferred), or
2. Read `Admin.tier` directly when a snapshot is not in scope,
3. And consult `TIER_INSTANCE_CAPS` from
   `app.models.subscription` for caps.

`app.services.admin_service._enforce_tier_scope` is the reference
implementation as of C22:

```python
from app.models.subscription import TIER_INSTANCE_CAPS, TIER_FREE

sub = self.db.query(Subscription).filter_by(admin_id=admin_id).first()
if sub is None:
    admin = self.db.query(Admin).filter_by(id=admin_id).first()
    if admin is None:
        raise HTTPException(402, "No admin profile found.")
    tier = admin.tier or TIER_FREE
else:
    tier = sub.tier

cap = TIER_INSTANCE_CAPS.get(tier)
if cap is None and tier not in TIER_INSTANCE_CAPS:
    raise HTTPException(402, f"Unrecognized tier: {tier!r}")
```

Fail-closed: missing Admin row, unrecognized tier — both 402, not 500.

---

## 4. Where the bootstrap is wired in (C22)

| Call site                                                   | What it used before                | What it uses now             |
|-------------------------------------------------------------|------------------------------------|------------------------------|
| `app.api.v1.auth._resolve_tenant_for_user`                  | C20 inline SECDEF call             | `IdentityBootstrap.resolve`  |
| `app.middleware.session_cookie_auth`                        | C20 inline SECDEF call             | `IdentityBootstrap.resolve`  |
| `app.repositories.scope_assignment_repository.list_for_user`| C21 `_list_for_user_secdef`        | `IdentityBootstrap.resolve.active_scopes` |
| `app.services.billing_service` (Stripe-race fallback)       | Direct table read                  | Preserved — bootstrap snapshot is consulted first by `_resolve_tenant_for_user` |
| `app.services.admin_service._enforce_tier_scope`            | Subscription-only check (broken Free) | Subscription-or-Admin.tier with `TIER_INSTANCE_CAPS` |
| `app.api.v1.admin.list_luciel_instances`                    | Dead `list_for_scope` (HTTP 500)   | `list_for_admin(admin_id=tenant_id, ...)` |

The C20/C21 SECDEF functions are intentionally left in place for one
release as defence-in-depth. A follow-up migration drops them once
this release proves stable in prod.

---

## 5. What this layer is not

- **Not an auth layer.** Bootstrap assumes you have already authenticated
  the user (cookie verified, API key matched). It only answers
  "given this verified user_id, what is their scope and tier?"
- **Not a cache.** Every call hits the DB. If we need caching, it goes
  inside `IdentityBootstrap`, not at the call sites.
- **Not a write path.** Bootstrap is read-only. Tenant provisioning,
  scope mutation, and tier upgrades go through their own services.
- **Not the GUC setter.** Callers that need to populate the
  `app.tenant_id` GUC for RLS continue to do so explicitly. A future
  helper (`IdentityBootstrap.activate(...)`) may collapse that step but
  is not in C22.

---

## 6. Test posture

C22 ships with two new tests (WS4):

1. **Happy-path E2E**: signup → login → list instances → create instance →
   list instances. Asserts no 5xx, no 422, no `[object Object]`, no
   silent-empty list after create.
2. **RLS fuzz**: For every read path that goes through RLS, assert that
   omitting the tenant GUC produces either a complete bootstrap-backed
   answer OR a hard 401/402 — never a silently empty `200 []`.

These tests run in CI on every PR to `main` and on every push to
`hotfix/*` branches.

---

## 7. Migration history

| Migration                              | What it does                                           |
|----------------------------------------|--------------------------------------------------------|
| `arc9_c20_resolve_tenant_for_user_secdef` | Original per-call SECDEF for login path             |
| `arc9_c21_list_scopes_secdef`             | Per-call SECDEF for scope listing                   |
| `arc9_c22_identity_bootstrap`             | Consolidated SECDEF returning full identity payload |

---

## 8. Style invariants

- Bootstrap is constructed with `IdentityBootstrap(db)` — pure DI, no globals.
- `IdentitySnapshot` is frozen. Don't mutate it. If you need a derived
  view, add a property.
- Don't add new SECDEF helpers. If a new identity fact is needed,
  extend `arc9_c22_bootstrap_identity` and the snapshot.
- Don't synthesize a virtual Subscription for Free users. Read
  `Admin.tier` and consult `TIER_INSTANCE_CAPS`.

---

*End of doctrine. Questions, drift, or proposed changes → open a PR
against this file with the rationale in the description.*
