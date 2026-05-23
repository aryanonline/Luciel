# Arc 6 Commit 3 — `admin_widget_domains` Design Decisions

**Authored:** 2026-05-23
**Commit:** Arc 6 Commit 3 (migration authoring; prod apply deferred to Commit 10b)
**Owners:** Aryan + partner (judgement delegated 2026-05-23 7:02 PM EDT)

This document records the decisions taken at the Arc 6 schema half so that future arcs (and future readings of this code) can audit the reasoning without spelunking through chat history.

---

## Decision 1 — FK shape (`admin_id` vs `subscription_id` vs both)

**Locked:** `admin_id VARCHAR(100) NOT NULL REFERENCES admins(id) ON DELETE CASCADE`. Single FK. No `subscription_id` column.

**Options considered:**

1. **`admin_id` only** (chosen). Matches the V2 vocabulary; Free tier (no Stripe row) can register domains; Commit 6 Tenant→Admin rename has zero touch on this table.
2. `subscription_id` only — would either require a synthetic "free subscription" row (rejected in Commit 1 numeric lock) or break the Free tier flow entirely. Also breaks symmetry with how Pro/Enterprise admins keep their allowlist if a subscription lapses.
3. Both FKs with `subscription_id NULLABLE` — adds a second column with unclear semantics (which one wins on conflict?); the aggressive-cleanup doctrine rejects this kind of latent ambiguity.

**Why CASCADE on the FK, not RESTRICT:**

The codebase has two established FK-to-`admins.id` patterns:

- `scope_assignments.admin_id` → `ON DELETE RESTRICT`
- `user_invites.admin_id` → `ON DELETE RESTRICT`
- `instances.admin_id` → `ON DELETE RESTRICT`

All three use RESTRICT because they represent durable user-side state that must be deliberately torn down before the parent Admin is removed (active permission grants; outstanding invites; long-lived user-data tenancies).

`admin_widget_domains` is different: domain rows are **derived configuration** with no value standalone. If the Admin is gone, the allowlist has no meaning — there is no widget instance left to gate. CASCADE is correct here.

## Decision 2 — Column-level constraints

**`domain` is `VARCHAR(253)`** per RFC 1035 (max DNS hostname length: 253 octets excluding the trailing dot). The app layer normalises to lowercase + apex form before insert; the schema's `CHECK (domain = lower(domain))` enforces the lowercase contract so a buggy writer cannot silently insert mixed-case rows that would bypass the UNIQUE constraint.

**`CHECK (length(domain) > 0 AND domain !~ '[[:space:]]')`** is a cheap sanity backstop. The app layer does heavier validation (apex parsing, public-suffix-list check, no path / no scheme / no port), but the schema-level CHECK guarantees zero-length and whitespace-containing rows never land regardless of which code path inserts.

## Decision 3 — Uniqueness shape

**`UNIQUE (admin_id, domain)`** — composite. Two reasons:

1. **One Admin cannot register the same domain twice.** Domain entries are conceptually unique within an Admin's allowlist; the database is the right place to pin that contract.
2. **Two different Admins can independently allowlist the same hostname.** Consider a marketplace where multiple operators all embed widgets at `marketplace.example`. The widget request resolver routes by `admin_id` so the per-Admin allowlist semantically owns its own copy of any given domain.

No global `UNIQUE (domain)`. No partial unique indexes. The composite UNIQUE is sufficient and the simplest correct shape.

## Decision 4 — Index strategy

**`ix_admin_widget_domains_admin_id ON (admin_id)`** is the only secondary index. It supports the hot per-request lookup:

```sql
SELECT 1 FROM admin_widget_domains
WHERE admin_id = :admin_id AND domain = :domain;
```

The query plan: index scan on `admin_id` (high selectivity in steady state — most Admins will have ≤10 domains; the largest Enterprise might have ~100), then linear filter on `domain`. The composite UNIQUE constraint also provides a B-tree on `(admin_id, domain)` that could serve this query, but it is the UNIQUE constraint's index and we do not rely on it for query planning.

We do **not** add a global index on `domain` alone because there is no business query that asks "which Admin allowlists `example.com`?" — the widget request always knows the target Admin from the request envelope.

## Decision 5 — Tier limit enforcement is at the app layer

**CANONICAL §14 says Pro is rate-capped on domain count.** That cap is enforced in the `BillingService` / `TierProvisioningService` rewrite at Commit 5, not in the schema:

- **Why not a CHECK constraint:** the Pro cap might change between $349/mo and a future $499/mo plan; schema migrations to change a number are expensive and risky.
- **Why not a trigger:** triggers split the enforcement story across two surfaces (Python + plpgsql) and make the rate-limit logic harder to reason about.
- **Why the app layer is sufficient:** the only writers to `admin_widget_domains` are (a) the Free signup route at Commit 8 (one domain max, enforced inline), (b) the admin console at a future commit (rate-checked against §14), and (c) sales-ops provisioning for Enterprise (deliberately bypassed). All three are well-known code paths.

## Decision 6 — Forward-only with safe downgrade

Unlike Arc 5 Revision C (which is forward-only because it drops tables and data), Revision A is **birth-only**: the table does not exist before this migration, so `downgrade()` can safely `drop_table()` without data loss.

This matters at Commit 10b. If `alembic upgrade head` runs in prod and something goes wrong before the new backend code activates, `alembic downgrade -1` is a clean rollback path. The post-migration application code (Commits 5–9) will not be live yet, so dropping the table has no callers.

## Decision 7 — Naming convention

Revision id: `arc6_a_admin_widget_domains` (matches the Arc 5 `arc5_a/b/c` pattern).

File name: `arc6_a_admin_widget_domains.py` (the Alembic CLI's autogenerated `<hash>_<slug>.py` pattern was deliberately not used here — the Arc 5 chain established the `arc<N>_<letter>_<noun>.py` form as the canonical convention for arc-locked migrations, and Arc 6 inherits it).

FK constraint name: `fk_admin_widget_domains_admin_id` (explicit; never let PG auto-name an FK that downstream migrations might need to drop — the Arc 5 Revision C work-around for `user_invites.tenant_id`'s auto-named FK demonstrated why explicit names matter).

Index name: `ix_admin_widget_domains_admin_id`. CHECK names: `ck_admin_widget_domains_domain_lowercase`, `ck_admin_widget_domains_domain_shape`. UNIQUE: `uq_admin_widget_domains_admin_id_domain`. All explicit, all stable, all introspectable.

---

## What this document does not decide

- **API surface** for managing the allowlist (route paths, payload shapes, auth) — that lands at Commit 5 (BillingService) and Commit 8 (free signup).
- **Widget runtime behavior** when a domain is not allowlisted — that lands at the widget-side code which is outside Arc 6 scope.
- **Migration of any existing widget-domain data** — there is none to migrate; the widget was previously gated by `domain_configs` (deleted at Arc 5 Revision C) and any allowlist semantics there were not preserved (per the V2 collapse doctrine).
- **Cap numbers** for Pro vs Enterprise — those are in CANONICAL §14 and are app-layer concerns.
