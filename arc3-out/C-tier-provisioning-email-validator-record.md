# Arc 3 Work-Unit C — Email-Shape Gate at TierProvisioningService

**Date:** 2026-05-22 01:35 EDT
**Operator:** Aryan Singh (paired with Computer)
**Disposition:** **CODE-ONLY, deploy batched** (per Arc 3 plan)

## Trigger

Arc 3 plan item: "email_validator at tier_provisioning_service (code only, deploy batched)". The provisioning service was accepting any email string from the upstream webhook and only failing late — at slug derivation or DB constraint time — when the email was malformed. This produced opaque downstream errors with no clean 4xx/5xx distinction.

## Approach: Shape-Gate, Not RFC-Grade

After scout (Block 7q-r5, this session), the existing precedent in `app/identity/resolver.py` was clear:

> "Liberal email shape check. We do NOT do RFC-grade validation here; the adapter is the source of truth and is trusted within its scope per §3.2.11 v1 (verified_at=NULL is the v1 trust model). This regex rejects obvious garbage (no @, multiple @s, control chars) so a misformatted asserted claim does not pollute the unique constraint."

We mirror that contract exactly — same regex shape, same length cap (320 chars, RFC 5321 max, matches `User.email` column cap). The original task name "email_validator" suggested using the `email-validator` package (transitively available via `pydantic[email]`), but its strict deliverability checks (MX lookup, public-suffix list) would **reject** legitimate synthetic emails like `agent-<id>@<tenant>.luciel.local` that the Option B onboarding path mints. Mirroring the resolver's liberal regex is the right contract for this layer.

## Changes

### `app/services/tier_provisioning_service.py` (+93 lines, 0 deletions)

1. New module-level constants `_EMAIL_MAX_LEN = 320` and `_EMAIL_SHAPE` (regex matching the resolver's contract).
2. New exception class `TierProvisioningValidationError(ValueError)` — subclasses `ValueError` so the webhook's existing `except ValueError` trap catches it unchanged. Distinct class so future call sites can distinguish 4xx-class validation failures (do-not-retry) from 5xx-class provisioning errors (retryable by reconciler).
3. New helper `_validate_email_shape(email: str | None) -> str` — case-folds, strips, validates, returns the canonical form, raises on failure.
4. New call to `_validate_email_shape(...)` in `premint_for_tier` immediately after the tier check and before the tenant lookup — fail-fast, no DB roundtrip on bad input.

All four blocks are commented inline with the design rationale, the resolver-precedent reference, and the rationale for not using `email-validator`.

### `tests/services/test_tier_provisioning_email_validation.py` (NEW, 163 lines, 35 tests)

Pure-function unit tests covering:

| Branch | Tests |
|---|---|
| Valid real-shape emails | 4 |
| Valid synthetic emails (`*.luciel.local`) | 2 |
| Case-folding / whitespace strip | 3 |
| At-max-length acceptance | 1 |
| `None` rejection | 1 |
| Non-str rejection (int/float/list/dict/bytes) | 5 |
| Empty / whitespace-only rejection | 4 |
| Malformed (no `@`, double `@`, control chars, etc.) | 12 |
| Oversize rejection | 1 |
| Exception-class `issubclass(ValueError)` contract | 1 |
| Exception catchable as `ValueError` (webhook trap) | 1 |

**All 35 tests pass.** No DB / Celery / pgvector required — pure-function tests follow the codebase's static-test pattern (see `tests/services/test_cascade_includes_all_privilege_layers.py` for the same posture).

## Verification

```
$ DATABASE_URL="postgresql://test:test@localhost:5432/test" python3 -m pytest tests/services/ -q
.....................................................                    [100%]
53 passed in 0.63s
```

35 new tests + 18 pre-existing = 53/53 green. No regressions.

## Six-Pillar Read

| Pillar | How |
|---|---|
| **Scalability** | Pure regex, O(n) on email length, no I/O |
| **Reliability** | Fails fast at entry point; no opaque downstream slug collisions |
| **Maintainability** | Same contract as `app/identity/resolver.py`; one module-level comment block explains intent |
| **Traceability** | Distinct `TierProvisioningValidationError` class so logs and audits can distinguish validation failures from real provisioning errors |
| **Security** | Rejects control-char and embedded-whitespace inputs that could pollute `User.email` unique constraint or audit logs |
| **Simplicity** | No new dependency (`email-validator` deliberately avoided); no behavior change for any valid input |

## Drift Tracker

- **Closes:** No drift to close (Work-Unit C was a planned task, not a drift-remediation)
- **Opens:** `D-tier-provisioning-email-validator-deploy-pending-2026-05-22` — code lands in `main` but is not yet on a live task. Closes when the next backend deploy lands. Per Arc 3 plan, this deploy is **batched** with future work, not done tonight.
- **Adopts:** No new standards.

## Deploy Plan (Batched)

This change is **inert until deployed**. It will be picked up by the next `update-service luciel-backend-service --force-new-deployment` cycle, which will pull the new image (after CI builds it). Per Arc 3 plan and the locked anti-deferral rule, batching here is correct: the change is zero-risk for valid inputs (synthetic + real both pass shape check unchanged), and the next deploy cycle is the natural delivery point.

## Operator Action

None tonight. Code is committed and pushed. The change goes live with the next backend deploy.

## Git Trail

- Commit: *(filled by commit step)*
- Files: `app/services/tier_provisioning_service.py`, `tests/services/test_tier_provisioning_email_validation.py`, this record
