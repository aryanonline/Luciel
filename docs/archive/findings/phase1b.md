# Phase 1b — Rate-limit fail-mode

Reconstructed from code citations and resolution commits on `step-29y-impl`. See [`README.md`](./README.md) for methodology.

## B-1 — Rate-limit fail-open on misconfigured Redis URL

### Code citations
- `tests/security/test_rate_limit_failmode.py:3` — "The audit (findings_phase1b.md B-1, PHASE3_REMEDIATION_REPORT row 5)"

### Resolution commits (on `step-29y-impl`)
- `7e783a5` — Step 29.y Cluster 5 (B-1): rate-limit fail-mode hardening
- `0f827e5` — Step 29.y Cluster 5 (B-1): tests/security package marker
- `a98525a` — Step 29.y Cluster 5 (B-1): behavioral + AST tests for rate-limit fail-mode

### Reconstructed summary

The pre-29.y rate limiter read its Redis URL from an environment variable named `REDISURL` (no underscore). Whenever the variable was misnamed, missing, or unparseable, the limiter silently fell back to permissive mode (fail-open). Anyone who accidentally typoed the env var deployed a rate limiter that did nothing.

The Cluster 5 fix:
- Renames the env-var read to the correct `REDIS_URL` constant.
- Establishes an explicit fail-mode contract — connection failures in steady state become 503, NOT silent pass-through.
- Adds AST and behavioral tests in `tests/security/test_rate_limit_failmode.py` that pin the contract.

The historical fail-open window is itself a Step 29.y gap-fix disclosure entry — see Commit 9 (`D-historical-rate-limit-typo-disclosure-2026-05-07`).
