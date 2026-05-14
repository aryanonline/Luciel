# Cascade Purge Gap — Discovered 2026-05-14 while drafting Stripe activation scaffold

**Status:** OPEN — to be added to `docs/DRIFTS.md` §3 as `D-cancellation-cascade-incomplete-conversations-claims-2026-05-14` in tomorrow's doc-truthing commit, alongside other discoveries from this session.

**Discovery context:** While tracing `BillingWebhookService._on_subscription_deleted` to honestly answer Stripe activation form Section 3 (refund / cancellation policy), agent walked `AdminService.deactivate_tenant_with_cascade` end-to-end. Found that the cascade visits memory_items / api_keys / luciel_instances / agents / agent_configs / domain_configs / tenant_config (7 layers, leaf-first, all atomic) — but **does not visit conversations, identity_claims, or messages**, despite those tables having tenant_id + active columns and being subject to the same Pattern E soft-delete discipline.

## What the cascade does today (sourced)

`app/services/admin_service.py` lines 824-953, `deactivate_tenant_with_cascade`:

| Layer | Table | Visited by cascade? |
|---|---|---|
| 1 (broadest) | `memory_items` | ✓ |
| 2 | `api_keys` | ✓ |
| 3 | `luciel_instances` | ✓ |
| 4 | `agents` | ✓ |
| 5 | `agent_configs` (legacy) | ✓ |
| 6 | `domain_configs` | ✓ |
| 7 (root) | `tenant_config` | ✓ |
| — | `conversations` | ✗ has tenant_id + active, NOT visited |
| — | `identity_claims` | ✗ has tenant_id + active, NOT visited |
| — | `messages` | ✗ scoped via conversation_id only |

## What this means in customer terms

When a customer cancels their Luciel subscription today:

1. Stripe's portal cancellation sets `cancel_at_period_end=true`. Customer keeps full access through end of period.
2. At period end, Stripe fires `customer.subscription.deleted`. Our webhook fires `_on_subscription_deleted`.
3. Subscription row flips to `active=False`, `status=canceled`. Audit row recorded.
4. `deactivate_tenant_with_cascade` flips 7 layers to `active=False`. Audit rows recorded.
5. **Customer's conversations, identity_claims, and messages remain `active=True` in the database**, under a deactivated tenant.

The data is **unreachable through the application layer** because every read path runs scope enforcement (ARCHITECTURE §4.7 / §4.9), and the scope checks reject access to a deactivated tenant. So the customer cannot access their own data, and no other tenant can access it.

But the rows are still in the database. A direct SQL query (admin tool, support staff, future debugging, security audit) would see the data.

## Why this is a real gap (not just a curiosity)

Three reasons:

1. **PIPEDA Principle 5 (limit retention).** The `memory_items` cascade comment at `admin_service.py` line 393-396 explicitly cites PIPEDA Principle 5 as the justification for soft-deactivate + scheduled hard-purge. Applying that doctrine to memory_items but NOT to conversations / identity_claims / messages is internally inconsistent. If memory_items needs PIPEDA-justified soft-deactivation, conversations need it too — conversation transcripts are the most sensitive data in the platform.

2. **The "future scheduled job" mentioned in the comment does not exist yet.** Line 395-396 says "A future scheduled job hard-purges inactive rows after the configured retention window." Agent has not located such a job in the codebase. Soft-deactivated data therefore persists indefinitely today.

3. **The cascade was designed leaf-first to be exhaustive.** Pattern E doctrine plus the explicit "broadest leaf first" structure (line 836-843) implies the designer intended this to be complete. The omission of conversations / identity_claims / messages is more likely an oversight than a deliberate carve-out.

## Why this is not a crisis

Three reasons:

1. **The data is application-layer-unreachable.** No API path returns it for a deactivated tenant. The scope checks at ARCHITECTURE §4.7 are tight.
2. **The data is not exposed to other tenants.** It's not a confidentiality leak — it's a retention-policy gap.
3. **It's defensible under PIPEDA today** because the access surface is closed. PIPEDA's expectation is that personal data not be retained beyond the purpose it was collected for. Customer transcripts collected to qualify leads, when the customer cancels, no longer have an active purpose — but they have an audit purpose (was a chargeback dispute filed? was there a complaint? did the customer reactivate?), which is a defensible reason for retention.

The gap is a **policy gap** more than a **compliance gap**. The right fix is to either:
- (a) extend the cascade to include conversations / identity_claims / messages, OR
- (b) build the "future scheduled job" mentioned in the memory_items comment to hard-purge inactive rows after a configurable retention window (e.g. 90 days), OR
- (c) both — extend cascade for the soft-delete and add the retention job for the hard-purge.

Option (c) is the right design. It's exactly what was foreshadowed at `b8e74a3c1d52` (migration docstring line 18-19, "the retention worker") and what the user proposed today as 1b.

## Resolution path

Step 30a.2 (or 30b — to be decided when designed). Scope:

1. **Extend `deactivate_tenant_with_cascade`** to add three more layers between memory_items and api_keys:
   - layer 1a: `conversations` (soft-deactivate where `tenant_id == X AND active == True`)
   - layer 1b: `messages` (soft-deactivate where `conversation_id IN (deactivated conversation ids)`)
   - layer 1c: `identity_claims` (soft-deactivate where `tenant_id == X AND active == True`)
2. **Build retention worker** that scans `*.active == False AND deactivated_at < (now - retention_window)` across all tenant-scoped tables and hard-deletes those rows. Configurable per-table retention window. Audit-log the hard-deletes.
3. **Update ARCHITECTURE §4.4** (soft-delete by default) to explicitly enumerate which tables participate in cascade vs. which are reached via parent-relationship transitively. Today the doctrine is implicit; we should make it explicit.
4. **Update ARCHITECTURE §3.2.13** (billing) to describe the cancellation flow including data-purge timeline.
5. **Open data retention policy on the website** so customers can read it before signup. Stripe activation form Section 3 should reference this URL.

## Impact on today's Stripe activation form

Section 3 (refund / cancellation policy) should be worded **truthfully against today's behavior**:

> "Customers can cancel anytime from their account portal. Cancellation takes effect at the end of the current billing period. After cancellation, customer access is revoked and the customer's tenant is deactivated. Customer data is retained in deactivated form for audit and reactivation purposes and is purged per our data retention policy."

This is true today (cancellation revokes access, tenant is deactivated, data retained for audit). It does not over-promise (does not claim immediate hard-purge). It allows future Step 30a.2 work to tighten the wording.

**Do NOT** word the activation form as "customer data is purged at end of cancellation period" — that is not true today and would be a misrepresentation to Stripe and to future customers.

## Cross-references for tomorrow's DRIFTS entry

When this drift lands in `docs/DRIFTS.md` §3, cross-reference:
- CANONICAL_RECAP §12 Step 30a (subscription lifecycle introduced)
- CANONICAL_RECAP §12 Step 30a.1 (tier-cadence fanout, still soft-delete only)
- CANONICAL_RECAP §12 Step 30a.2 (future — closes this drift)
- ARCHITECTURE §3.2.13 (billing subsystem)
- ARCHITECTURE §4.4 (soft-delete by default)
- ARCHITECTURE §4.7 (scope enforcement)
- ARCHITECTURE §4.9 (PIPEDA posture)
- Code: `app/services/admin_service.py:824-953` (cascade method)
- Code: `app/services/admin_service.py:377-486` (memory_items cascade with PIPEDA comment)
- Code: `app/services/billing_webhook_service.py:478-548` (cancellation handler)
- Migration: `alembic/versions/b8e74a3c1d52_step30a_subscriptions_table.py` line 18-19 ("retention worker" foreshadowing)

## Stable drift ID (proposed)

`D-cancellation-cascade-incomplete-conversations-claims-2026-05-14`

Date is discovery date per doctrine. Slug describes the condition (cascade is incomplete for conversations + identity_claims + messages), not the fix. ID is immutable once minted in tomorrow's commit.
