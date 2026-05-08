# Luciel Security Disclosures

This file is the canonical, in-repo log of security-relevant historical defects in Luciel that were silently present in production (or production-equivalent) and have since been remediated. The intent is honesty: any defect with potential security or integrity impact that escaped detection long enough to reach a deployed environment is logged here, not just patched.

The format mirrors a CVE-style entry but is internal. Each entry pairs an immutable disclosure ID with the named drift token (see `docs/DRIFT_REGISTER.md`) and the remediation commit. Entries are append-only.

This file is referenced from §11.2a of the canonical recap.

## Disclosure index

| ID | Date | Token | Severity | Status |
|---|---|---|---|---|
| DISC-2026-001 | 2026-05-07 | `D-historical-rate-limit-typo-disclosure-2026-05-07` | High | Remediated on `step-29y-impl` (`7e783a5`); disclosed in `step-29y-gapfix` C9 |

---

## DISC-2026-001 — Rate-limiter env-var typo silently disabled cluster-shared rate limiting in production

**Date logged:** 2026-05-07
**Drift token:** `D-historical-rate-limit-typo-disclosure-2026-05-07`
**Remediation commit:** `7e783a5` (Step 29.y Cluster 5 / B-1, "rate-limit fail-mode hardening")
**Verification gate:** Pillar P11 (rate-limit fail-mode), behavioral + AST tests landed in `a98525a`
**Severity (internal):** High — control was advertised as enforced, was silently bypassed in prod
**Exploitability:** No authenticated attacker required. Any caller hitting a per-route limit could exceed it linearly with the number of running ECS tasks behind the ALB.
**Customer impact:** None observed; no abuse incident recovered from logs. Upper-bound theoretical: a `60/minute` route would have permitted `N × 60/minute` cluster-wide where `N` is the active task count.

### Root cause

The rate-limiter module read its storage URL via `os.getenv('REDISURL')` — missing underscore. The actual exported environment variable in production was `REDIS_URL`. The `getenv` call therefore resolved to `None` on every process start. SlowAPI silently fell through to its `memory://` backend, which is per-process. Cluster-shared rate limiting was never in effect for the entire production lifetime of the affected build window.

### Why it escaped

1. The fallback was silent. SlowAPI does not raise or log when it falls through to `memory://`; it just uses it.
2. There was no startup assertion that the configured backend matched the intended backend.
3. Local dev ran a single process, so the per-process behavior was indistinguishable from the intended cluster-shared behavior.
4. No integration test asserted that two simultaneous workers shared a counter.

### Remediation (`7e783a5`)

Three changes, all on `step-29y-impl`:

1. **Env-var name corrected** to `REDIS_URL` (matching the actual exported name) with a fail-loud assertion at module load: if the configured storage URL is unreachable at boot, the application now refuses to start with that misconfiguration silently in place.
2. **Pool hardening** — `storage_options` carries `retry_on_timeout=True`, 1.5s connect / 1.5s read socket timeouts, 30s `health_check_interval`. Single-RTT blips ride over; truly unreachable storage fails fast so the fallback middleware engages instead of stalling the request.
3. **Differentiated fail-mode** — limiter constructed with `in_memory_fallback_enabled=True` so reads transparently degrade to per-process limiting when the primary backend dies (read fail-open). The fallback middleware classifies escaping exceptions and returns `503 + Retry-After` only for write methods (`POST/PUT/PATCH/DELETE`), preserving write-quota integrity. Non-write methods re-raise rather than silently masking errors as `200`s. The classifier is narrow on purpose: `redis` or specific connection-failure phrases; generic `timeout` alone is not enough, since a route handler raising `ValueError('request timed out for user X')` would otherwise be misclassified as a backend error.

### Verification

- Behavioral tests assert that two test-mode limiter instances sharing a backend block at the configured threshold and that the fallback middleware returns `503 + Retry-After` only on write methods. See commit `a98525a` (Cluster 5 B-1 tests).
- AST tests assert that the env-var name `REDIS_URL` (with underscore) is the only string the module reads for storage configuration, blocking regression of the original typo.
- Pillar P11 (rate-limit fail-mode) is part of the 25-pillar verification suite gated by `docs/STEP_29Y_CLOSE.md` Phase 2.

### Disclosure rationale

This entry exists for three reasons:

1. **Honesty.** A control we advertised as enforced was not enforced. That fact must be in the repo, not just in chat history.
2. **Customer-facing record.** When the May 25 broker meeting and any future enterprise conversation asks about Luciel's security posture, the answer points at this file as evidence that defects are remediated and disclosed, not buried.
3. **Regression-prevention contract.** Any future change that touches the rate-limiter env-var read path is required to keep the AST test green; the test exists specifically to prevent the typo from reappearing.

### Status

Closed. Remediation landed on `step-29y-impl`. Pillar P11 verifies the fix. This disclosure is the missing public-facing record.
