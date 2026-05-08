# Luciel Drift Register

This file is the canonical, in-repo register of all named drift tokens. Per the Luciel working doctrine, every fix lands as one named drift token + one named commit + a verify gate. Tokens are immutable once recorded; supersession is captured via a new entry that references the prior token, never by editing.

Source-of-truth precedence: code > commit > recap > prior recaps > chat. This register is a **commit-derived index**, not a substitute for `git log`. The commit hash column is authoritative; the prose is a convenience pointer.

## Schema

| Column | Meaning |
|---|---|
| Token | Drift token, format `D-<slug>-YYYY-MM-DD` |
| Commit | Hash on the branch where the fix landed |
| Branch | Branch the commit is on at time of entry |
| Status | `closed` (fix landed + verified), `open` (acknowledged, not yet fixed), `deferred` (carry-forward to a future step), `superseded-by-<token>` |
| Pillar / Cluster | Verification pillar or 29.y cluster the token belongs to, if any |
| Summary | One-line description; full rationale lives in the commit body |

## Active entries

### Step 29.y gap-fix series (2026-05-07)

Branch: `step-29y-gapfix` (forked from `step-29y-impl`).

| Token | Commit | Status | Pillar / Cluster | Summary |
|---|---|---|---|---|
| `D-actor-permissions-comma-fragility-2026-05-07` | `1099f4f` | closed | P23 (audit chain) | JSON-serialize new `actor_permissions` writes; dual-format read; historical rows untouched to preserve hash chain |
| `D-audit-note-length-unbounded-2026-05-07` | `30878b0` | closed | P23 (audit chain) | Cap audit `note` at 256 chars at `AdminAuditRepository.record()` boundary with truncation marker |
| `D-scope-policy-action-class-gap-2026-05-07` | `5d99db8` | closed | P14 (scope policy) | Add `ScopePolicy.enforce_action(...)` primitive; no callers wired in this commit |
| `D-worker-audit-write-failure-not-alerted-2026-05-07` | `491d427` | closed | P23 (audit chain) | `WORKER_AUDIT_WRITE_FAILED` structured log marker + thread-safe process-local failure counter |
| `D-findings-docs-not-in-repo-2026-05-07` | `d2572db` | closed | meta | Reconstruct `docs/findings/` index for phases 1b/1d/1e/1f/1g from code + commit history |
| `D-cluster-4b-deferral-undocumented-2026-05-07` | `4c8522a` | closed | Cluster 4b | `docs/STEP_29Y_DEFERRED.md` records E-6 / E-12 / E-13 carry-forward to Step 30b |
| `D-cluster-7-unaccounted-2026-05-07` | _this commit_ | closed | Cluster 7 | Cluster 7 has no commits, tests, or code citations on `step-29y-impl`; investigated, no evidence on branch; logged here as the canonical disposition |
| `D-step29y-impl-no-close-tag-2026-05-07` | _pending C8_ | open | meta | No `step-29y-complete` tag exists; `docs/STEP_29Y_CLOSE.md` will define the verify-then-tag checklist |
| `D-historical-rate-limit-typo-disclosure-2026-05-07` | _pending C9_ | open | P11 (rate-limit fail-mode) | Disclosure of historical rate-limit fail-mode typo; recap §11.2a entry |
| `D-canonical-recap-v3.4-omits-step-29x-29y-2026-05-07` | _pending C10_ | open | meta | Canonical recap v3.4 omits Step 29.x and Step 29.y; v3.5 will document 44 commits / ~5300 lines and reference all gap-fix tokens |
| `D-audit-verification-harness-retry-duplicates-2026-05-07` | C11 (`3798b32`) + C12 _this commit_ | closed | Cluster 4 (E-2) / DISC-2026-003 | 223 verification-harness duplicate `worker_*` audit rows blocked migration `d8e2c4b1a0f3`; first attempt deleted 166 rows but broke 60 hash-chain links (P23 FAIL); reverted via CSV restore and migration redesigned forward-only; full disclosure in `docs/DISCLOSURES.md` |
| `D-cluster4-e2-rework-as-forward-only-2026-05-07` | _this commit_ | closed | Cluster 4 (E-2) / DISC-2026-003 | Migration `d8e2c4b1a0f3` redesigned to add `AND created_at >= TIMESTAMPTZ '2026-05-08 04:00:00+00'` to its partial UNIQUE index, preserving Pattern E without exception (no historical row mutated, chain unbroken, control enforced from cutoff forward) |
| `D-celery-app-not-imported-on-uvicorn-boot-2026-05-07` | C13 _this commit_ | closed | P25 (worker pipeline liveness) / latent prod | `app/main.py` did not import `app.worker.celery_app`, so `@shared_task` on `extract_memory_from_turn` (and any other task) bound to Celery's default `current_app` whose default broker is `amqp://guest@localhost//`. Pillar 25 probe and any chat-turn async enqueue on a fresh uvicorn process would raise `kombu.exceptions.OperationalError` (port 5672 refused) instead of publishing to the configured Redis/SQS broker. Fix: import `celery_app` in `app/main.py` (canonical) and `app/memory/service.py` (defense in depth). Latent prod risk because verification harness imports `_infra_probes.py` which loads `celery_app` and masked the bug in dev — first real production chat turn after a clean uvicorn boot would have hit this |

### Carry-forward (deferred, not in scope this step)

| Token | Carry-forward to | Reason |
|---|---|---|
| `D-actor-permissions-storage-format-migration-step-30b-2026-05-07` | Step 30b | Full column rewrite + chain recompute requires prod migration + Pattern N alignment; outside code-only window |
| Cluster 4b items (E-6 / E-12 / E-13) | Step 30b | Prod IaC, cross-region replication, DR runbook; outside code-only window. See `docs/STEP_29Y_DEFERRED.md` |

## Maintenance contract

1. New drift tokens must be appended here in the same commit that introduces them.
2. Status changes (open → closed, etc.) are recorded by editing the row in a new commit; the commit message must reference the token.
3. Tokens are never deleted. Supersession is recorded by setting `Status = superseded-by-<new-token>` and adding a new row for the replacement.
4. Hashes in this file are pointers, not the source of truth. If a hash and `git log` disagree, `git log` wins and this file must be corrected.
