# Arc 8 — Commit 2 Prod Deploy Record (WU-2 Stripe checkout deliverability + premint kwarg fix)

**Date:** 2026-05-24 (Sunday)
**Wave:** WU-2 (Checkout deliverability)
**Image:** `arc8-c2-08fd4ff`
**Digest:** `sha256:0d7bcaeb3747246f1ba711d06c207e85b81b74d8704490471341041e5782d1c4`
**Size:** 224.9 MB
**Commit:** `08fd4ff` (`Arc 8 C2: Stripe checkout email-deliverability MX-gate + premint kwarg fix`)

## Doctrine

Arc 8 C2 hardens the post-checkout owner-mint path on two fronts in a
single commit. Both touch `app/services/tier_provisioning_service.py`
and shipping the deliverability gate onto the broken floor of the
discovered kwarg drift would have deployed dead code into a method
that is never reached on the happy path.

### Drift 1 (planned): D-stripe-checkout-no-email-validation-2026-05-18

The Step 30a.5 live smoke walk surfaced this: a Stripe checkout
completed with the literal placeholder address
`aryan+smoke-30a5@yourdomain.com`, the webhook accepted it, the
owner user was minted, and the welcome email failed at SES with no
recovery path for the customer. The drift's resolution path option
(a) — pre-mint MX-record validation — is what we shipped:
`email_validator.validate_email(..., check_deliverability=True)`
wired into `premint_for_tier` AFTER the existing shape gate.

The gate is intentionally **soft-fail**: Stripe has already collected
payment by the time pre-mint runs, so aborting on a DNS blip would be
strictly worse for the customer than logging a structured warning and
letting Support reach out. The function returns one of five status
sentinels:

| Sentinel | Meaning |
|---|---|
| `DELIVERABILITY_OK` | MX lookup passed |
| `DELIVERABILITY_BYPASS_SYNTHETIC` | `*.luciel.local` skip |
| `DELIVERABILITY_BYPASS_DISABLED` | feature flag off |
| `DELIVERABILITY_FAILED` | NXDOMAIN / no MX / invalid TLD |
| `DELIVERABILITY_ERROR` | resolver flap; soft-pass |

Outcome is surfaced on `premint_for_tier`'s return dict under
`email_deliverability`. Today's consumer is CloudWatch; tomorrow's
consumer is the welcome-email send path (Arc 8 C5 polish target).

### Drift 2 (discovered during C2 recon): D-tier-provisioning-tenant-id-kwarg-mismatch-2026-05-24

P0-class. Discovered while reading
`app/services/tier_provisioning_service.py` to plan the deliverability
gate placement. Both production callers of `premint_for_tier` —
`BillingWebhookService._on_checkout_completed` and
`api.v1.billing.signup_free` — still pass `tenant_id=...` to a method
that the Arc-5 doctrine pass renamed to `admin_id=`. Every paid AND
free signup since Arc 5 has silently raised `TypeError` at the pre-mint
walk; both callers' `except Exception:` traps swallow it; the customer
ends up with an Admin row but no `ScopeAssignment` and no primary
`Instance`.

The fix at this commit is a backward-compat kwarg alias on the service:
the signature now accepts BOTH `admin_id=` and `tenant_id=` (one must
be supplied; `admin_id` wins if both are passed). The two callsites
will be migrated to the canonical `admin_id=` kwarg in a follow-up
Arc-8 commit; this commit is the "don't leave prod broken while the
file is open" minimal fix.

Existing unit tests in `tests/api/test_arc6_signup_free.py:338` stubbed
the service with `def premint_for_tier(self, **_kwargs):` which masked
the real kwarg mismatch — the stub accepts anything, so the test
passed while production failed. The masking is the explanation for why
this defect survived Arc 5 → Arc 7 close.

## Bundle Contents

### C2 — Email-deliverability gate + premint kwarg alias
- `app/services/tier_provisioning_service.py`:
  - New `_check_email_deliverability(email) -> (status, detail)` helper
    with five outcome sentinels (above) and a never-raises contract.
  - Late-imports `email_validator` inside the function so a sandbox
    without the library still imports the module cleanly.
  - `premint_for_tier` calls the helper after the shape gate;
    outcome stored on the return dict under `email_deliverability`.
  - Backward-compat kwarg alias on `premint_for_tier`:
    `*, admin_id: str | None = None, ..., tenant_id: str | None = None`
    with explicit precedence (`admin_id` wins) and a clear TypeError
    when neither is supplied.
- `app/core/config.py`:
  - `email_deliverability_check_enabled: bool = True` kill switch.
  - `email_deliverability_check_timeout_seconds: float = 2.0` to
    protect the Stripe-webhook 30s ACK budget against a slow/hostile
    resolver.
- `tests/services/test_tier_provisioning_email_validation.py`:
  - 6 new deliverability tests (bypass-synthetic, kill-switch,
    happy-path, typo-injection, resolver-error soft-pass, unexpected
    exception soft-pass).
  - 3 new kwarg-alias tests (tenant_id alias, admin_id canonical,
    neither-supplied programmer error).
  - All `email_validator` calls patched per-test so CI stays green
    in the no-DNS sandbox.
- `docs/DRIFTS.md`: full closure stanza for
  `D-tier-provisioning-tenant-id-kwarg-mismatch-2026-05-24`. Sibling
  deliverability drift closure deferred to Arc 8 C7 envelope sweep
  (closure evidence is the test suite + the production CloudWatch
  warning lines after the next deploy).

No schema change. No SSM mutation. No IAM expansion.

## Deploy Sequence (S1–S10 — schema-free shape, identical to C1)

| Step | Action | Result |
|---|---|---|
| S1 | Pre-flight prod state snapshot | backend:90 / worker:44 on `arc8-c1-a0d304b` 1/1 stable; alembic head `arc7_b_admins_last_signup_ip` |
| S2 | RDS snapshot | SKIPPED — C2 has no schema change |
| S3 | Build `arc8-c2-08fd4ff` via buildah | OK, `email-validator-2.3.0` confirmed in pip install output |
| S4 | ECR push | digest `sha256:0d7bcaeb3747246f1ba711d06c207e85b81b74d8704490471341041e5782d1c4`, size 224.9 MB |
| S5–S7 | Register `luciel-migrate:*` with alembic upgrade head | SKIPPED — no migration |
| S8 | Register `luciel-backend:91` | OK (cloned `:90`, swapped image only) |
| S9 | Register `luciel-worker:45` | OK (cloned `:44`, swapped image only) |
| S10 | UpdateService rolling on both services | both COMPLETED in ~3min |

## Smoke Triplet (post-deploy)

```
$ curl -sS https://api.vantagemind.ai/health
{"status":"ok","service":"Luciel Backend"}

$ curl -sS https://api.vantagemind.ai/ready
{"status":"ready","checks":{"db":"ok","redis":"ok"}}

$ curl -sS https://api.vantagemind.ai/api/v1/version
{"app":"Luciel Backend","version":"0.1.0","git_sha":"unknown","status":"ok"}
```

All three green. The `git_sha=unknown` is a known build-arg gap (same
as C1); the image tag `arc8-c2-08fd4ff` is the source of truth and is
visible on the ECS task-def container image field.

## Live Evidence Pending

Two CloudWatch log signals confirm C2 once a real signup lands:

1. The `tier_provisioning: pre-minted admin=... tier=... instance=primary`
   INFO line — this line has NOT appeared in production logs since Arc 5
   because the kwarg-mismatch `TypeError` aborted before it. Its
   appearance is the closure-side evidence for
   `D-tier-provisioning-tenant-id-kwarg-mismatch-2026-05-24`.

2. The `tier_provisioning: email deliverability check failed
   email_domain=...` WARNING line on any future checkout with a typo'd
   address. This line is the operator signal that the
   `D-stripe-checkout-no-email-validation` gate is doing its job.

Both will surface naturally during the Arc 8 E2E test plan execution
(Arc 8 C6).

## Tests

44/44 green in `tests/services/test_tier_provisioning_email_validation.py`
(was 14 pre-commit; +6 deliverability + +3 kwarg-alias + shape-test
re-parametrise). 70/70 green across `tests/services/` +
`tests/api/test_ready.py`. The 40 pre-existing failures in
`tests/api/test_step24/30a/31_*.py` are shape-test debt confirmed
unrelated to this commit by re-running the same set against `main`
pre-edit (40 failed there too).

## Prod State After C2

| Resource | State |
|---|---|
| Alembic head | `arc7_b_admins_last_signup_ip` (unchanged — C2 schema-free) |
| Backend | `luciel-backend:91` on `arc8-c2-08fd4ff` 1/1 stable |
| Worker | `luciel-worker:45` on `arc8-c2-08fd4ff` 1/1 stable |
| Frontend | C8 `ffb7e18` (Luciel-Website, unchanged) |
| `/health` | 200 |
| `/ready` | 200 `{db:ok, redis:ok}` |
| `/api/v1/version` | 200 (git_sha=unknown — build-arg gap) |

## Next

Arc 8 C3 — Per-embed-key + per-instance rate-limit buckets with
composition rule. Closes
`D-pro-tier-rate-limit-abuse-surface-2026-05-23`.
