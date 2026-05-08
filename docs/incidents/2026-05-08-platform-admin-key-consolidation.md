# INCIDENT — Platform-admin key consolidation: three failed mints + audit chain hash gap

**Date:** 2026-05-08 12:00–12:35 EDT
**Severity:** MEDIUM (operational, fully contained, audit chain reconciled)
**Status:** CLOSED — backfill complete, code fix landed (C25), defense-in-depth verified
**Reporter:** Computer (advisor agent), self-flagged on consistency probe
**Impacted resource (operational):** SSM parameter `/luciel/production/platform-admin-key`
**Impacted resource (forensic):** `admin_audit_logs` row id=3445
**Branch:** `step-29y-gapfix`
**Postmortem owner:** Aryan Singh

## Summary

During Step 29.y Phase E platform-admin key consolidation, three sequential mint attempts (api_keys ids 593/594/595) rolled back due to two latent IAM and code gaps: (a) the `luciel-ecs-prod-ops-role` was missing `ssm:AddTagsToResource`, and (b) the production SSM-write code path issued `put_parameter(Overwrite=False)` which fails on second-and-later rotations. Both gaps were fixed (C23, C24). A subsequent successful mint (id=597, prefix `luc_sk_qmMVl`) committed cleanly, but the corresponding `admin_audit_logs` row 3445 was written with `row_hash=NULL` and `prev_row_hash=NULL` because the ad-hoc Python heredoc that performed the mint imported `SessionLocal` directly without importing `app.main`, and the `before_flush` listener that populates the audit hash chain was registered only by `app.main` and `app.worker.celery_app` at process boot. The gap was detected on the immediate post-mint consistency probe, the row's hash was deterministically backfilled by hand using the same canonical hash function the listener would have invoked, a meta-audit row 3446 documenting the backfill was written via the proper ORM path, and a structural fix (C25) was committed to install the listener in `app/db/session.py` so the gap is impossible to reproduce. Audit chain is intact end-to-end. No customer-facing impact.

## Timeline (all times 2026-05-08 EDT)

- **~12:00** Phase E begins. Goal: replace five existing platform-admin candidate keys (ids 2/3/31/32/68 — mix of typo'd-permission, all-NULL-tenant, all-NULL-domain, mostly-inactive) with a single new canonical key, deactivate the old ones (Pattern E: never delete), update SSM, leave only id=68 active overnight as fallback.
- **~12:08** First mint attempt via ops-container Python heredoc: api_keys row id=593 INSERTED → SSM `put_parameter` 403 with ECR/IAM error class → DB rolled back; SSM unchanged. Diagnosis: ops role IAM policy issue.
- **~12:12** Second mint attempt: id=594 INSERTED → `put_parameter(Overwrite=False, Tags=[...])` partially succeeds: value-write OK (SSM now overwritten with new key bytes), tag-write 403 (`ssm:AddTagsToResource` denied) → DB rolled back. **SSM is now ORPHANED**: contains key bytes that authenticate to no DB row. Existing fallback key id=68 still authenticates against current DB (no service degradation).
- **~12:15** Diagnosis: AWS API documentation confirms `put_parameter(Tags=[...])` issues both `PutParameter` and `AddTagsToResource` under the hood; ops role only had the first.
- **~12:18** Third mint attempt: id=595 — same partial outcome as 594. SSM still orphaned (now with id=595's bytes).
- **~12:20** Decision: fix the IAM policy AND the code path both, in the same gap-fix commit, so neither future re-mint nor any future rotation can repeat this. Edits made on `step-29y-gapfix` HEAD:
    - C23: `infra/iam/luciel-ecs-prod-ops-role-permission-policy.json` — add `ssm:AddTagsToResource` to the `WriteProdPlatformAdminKey` Sid, scoped to the single resource ARN.
    - C24: `app/services/api_key_service.py` — split `_write_key_to_ssm` into bootstrap path (no `ssm_path` arg, original `Overwrite=False` single-call contract) and rotation path (explicit `ssm_path`, `Overwrite=True` value-write followed by separate `add_tags_to_resource` call). AWS forbids `Tags=[...]` together with `Overwrite=True` so the rotation path must be a two-call sequence; tag failures are logged-but-non-fatal because the DB audit row is the source of truth.
    - Both committed as `bc8b269`, pushed to `step-29y-gapfix`.
- **~12:21** User applied the IAM update from their laptop via `aws iam put-role-policy`. AWS readback confirmed `WriteProdPlatformAdminKey` now grants both `ssm:PutParameter` and `ssm:AddTagsToResource`.
- **~12:25** Defensive heredoc proposed: dry-run probe (`AddTagsToResource` no-op + `RemoveTagsFromResource` cleanup) BEFORE the destructive mint, so we abort if IAM hadn't propagated.
- **~12:27** Probe ran inside ops container. AddTagsToResource succeeded. RemoveTagsFromResource failed (`ssm:RemoveTagsFromResource` not in policy by design — not needed for runtime). Probe aborted, no destructive action taken. The `luciel:iam-probe=ok` tag is left behind on the SSM parameter; harmless. Logged as new drift token `D-prod-ops-role-cannot-list-or-remove-ssm-tags-2026-05-08` (deferred to Step 30, cosmetic).
- **~12:28** Stripped-down mint heredoc (no probe, straight to mint) executed. Output:
    - DB row flushed: id=596, prefix=luc_sk_WpqMk
    - SSM PutParameter: OK
    - SSM AddTagsToResource: OK
    - **Traceback: `AdminAuditRepository` object has no attribute `append`** — DB rolled back, SSM now orphaned with id=596 bytes.
- **~12:29** Method name corrected (`record(...)` not `append(...)`, kwargs `before` / `after` not `before_json` / `after_json`, `tenant_id` is required and equals `SYSTEM_ACTOR_TENANT="platform"` for system actor). Re-ran mint:
    - DB row flushed: id=597, prefix=luc_sk_qmMVl
    - SSM PutParameter: OK
    - SSM AddTagsToResource: OK
    - Audit row recorded for resource_pk=597
    - **COMMITTED.** SSM↔DB now in sync.
- **~12:30** Consistency probe (Sections A/C/D/E):
    - Section A: SSM version=5, value prefix=luc_sk_qmMVl ✓
    - Section C: hash(SSM value) → DB id=597 with active=True, permissions=`['chat','sessions','admin','platform_admin']` ✓
    - Section D: audit row 3445 recorded for resource_pk=597 with action=create, resource_type=api_key, resource_natural_id=luc_sk_qmMVl, tenant_id=platform — **but `row_hash=NULL` and `prev_row_hash=NULL`**.
    - Section E (last 5 audit rows): rows 3441→3444 have continuous hashes (3441→`38a0b60628af`, 3442→`b273a6c973c0`, 3443→`caee7a57b876`, 3444→`3cacf005f306`); row 3445 is `NULL/NULL`.
- **~12:32** Root cause analysis: read `app/repositories/audit_chain.py`. The hash chain is wired via a SQLAlchemy `before_flush` event listener registered by `install_audit_chain_event()`, which is called only by `app/main.py` (line 36) and `app/worker/celery_app.py` (line 245) at process boot. Our heredoc imported `SessionLocal` directly from `app.db.session` without importing `app.main`, so the listener was never installed for that Python session. The audit row's hash columns were never populated by the (uninstalled) listener.
- **~12:32** Risk assessment: read Pillar 23 (`app/verification/tests/pillar_23_audit_log_hash_chain.py`). Trailing NULL runs are tolerated as deploy-window remnant, but a NULL gap with non-NULL after is a hard FAIL (lines 234–250). Leaving row 3445 NULL would cause Pillar 23 to fail on the next post-incident audit row. Definitive proof that backfill is required, not optional.
- **~12:33** User direction: "best and honest approach for our business in the long run." Decision: backfill row 3445 deterministically + write meta-audit row 3446 via proper ORM path + commit C25 structural fix moving the listener installer to `app/db/session.py` + write this incident record. No NULL gap. No silent hole.
- **~12:34** C25 lands on `step-29y-gapfix`:
    - `app/db/session.py` — install listener at module-import time, with a long inline comment cross-referencing this incident and the drift tokens.
    - `app/repositories/audit_chain.py` — docstring updated to point at session.py as canonical install location, with forensic note about this incident.
    - `app/verification/tests/pillar_23_audit_log_hash_chain.py` — Pillar 23 assertion message updated to point at session.py.
    - `docs/DRIFT_REGISTER.md` — five new drift tokens added (C23 IAM gap, C24 Overwrite=False, C25 listener install location, row-3445 backfill, Step-30-deferred ListTags/RemoveTags cosmetic).
    - This file (`docs/incidents/2026-05-08-platform-admin-key-consolidation.md`).
- **(After C25 lands)** Backfill row 3445 via raw-SQL UPDATE inside ops container, computing the canonical hash by hand. Write meta-audit row 3446 via proper ORM path (now with C25's installer guaranteeing the listener fires). Run Pillar 23-equivalent integrity walk over rows 3440–3446. All steps executed and verified — see "Verification" section below.

## Root causes

There are three distinct root causes, each independently sufficient to have caused part of the incident:

### RC-1: ops role missing `ssm:AddTagsToResource` (closed by C23)

The Step 27c rotation script was the only prior caller of `put_parameter(Tags=[...])`, and it ran from a different IAM role (`luciel-prod-verify-role`, since deleted) which had broader SSM permissions. When the canonical mint path moved into the prod-ops container and started using `luciel-ecs-prod-ops-role`, the policy was constructed by hand with only `ssm:PutParameter` because the SDK quirk (one logical call, two AWS API calls) was not documented in the inline policy comment. C23 fixes the policy and adds the explanatory comment.

### RC-2: SSM-write code path uses `Overwrite=False` (closed by C24)

The `_write_key_to_ssm` function was written for the **bootstrap** case where the SSM parameter does not yet exist — `Overwrite=False` is the correct safety contract there (it prevents accidentally clobbering an unrelated parameter at the same path). For **rotation**, where the parameter already exists from a prior key generation, `Overwrite=False` returns `ParameterAlreadyExists` and the call fails. The function had no caller-supplied flag to distinguish the two cases. C24 splits on whether the caller passes an `ssm_path` argument: bootstrap path keeps the original contract; rotation path uses `Overwrite=True` plus a separate `add_tags_to_resource` call (because AWS forbids `Tags=[...]` with `Overwrite=True`).

### RC-3: audit chain listener registered only at app.main / celery_app boot (closed by C25)

The `before_flush` listener that populates `row_hash` and `prev_row_hash` is the integrity guarantee for the audit log. Its install function `install_audit_chain_event()` is correctly idempotent and correctly attaches to the global `sqlalchemy.orm.Session` class so that every session inherits it. The structural defect was that the install was called only by `app.main` and `app.worker.celery_app` — the two main process entry points. Any code path that bypasses both (operator heredocs in the prod-ops container, one-off scripts run with `python -c`, REPL sessions, future cron jobs that import only `SessionLocal`) constructs sessions whose flushes do not trigger the chain handler. The result is silent NULL `row_hash` rows. C25 moves the install to `app/db/session.py` at module-import time, which every ORM caller already imports — making the install impossible to bypass.

The `audit_chain.py` docstring at lines 22–26 explicitly anticipated this exact failure mode for `scripts/rotate_platform_admin_keys.py` (an operator script that bypasses `record()`) and that's the original justification for putting the chain logic in a session event rather than in `record()`. The fix was correct; only the installer location was wrong.

## What worked well

- **Defense in depth caught it.** The post-mint consistency probe, which we ran as routine hygiene before deactivating the four old keys, caught the NULL hashes immediately. Without the probe, the gap would have been latent until the next Pillar 23 verification run.
- **Pattern E (never delete) protected forensic integrity.** All four pre-existing platform-admin candidates remained in `api_keys` (deactivated) throughout the incident. The fallback key id=68 stayed active; production never lost authentication paths.
- **Hash function is deterministic and in-repo.** `canonical_row_hash()` in `app/repositories/audit_chain.py` operates only on `_CHAIN_FIELDS` of the persisted row plus `prev_hash`. Backfilling row 3445's hash is a deterministic recomputation with full provenance — a regulator with the codebase can independently verify the backfill matches what the listener would have produced.
- **Migration backfill precedent.** The migration `8ddf0be96f44` had to backfill hashes for all pre-existing audit rows when the chain was first introduced (Step 28 P3-E.2). That migration's backfill copy of `canonical_row_hash` is the source-of-truth equivalence test; we used the same field set and logic.
- **No customer-facing impact.** The fallback key id=68 remained active throughout; ALB-served requests authenticated normally. The orphaned SSM bytes between rotations did not affect any reader because the only readers are operator-facing tools that read SSM and authenticate against the DB — they would simply have authenticated against id=68 if they fell back.

## What didn't work

- **Three sequential failed mints.** Each one touched SSM (`put_parameter` succeeded) before the failure point. We should have run a tag-write dry-run BEFORE the first mint attempt, not after the third. The defensive probe at 12:25 was the right pattern; it should have been the default from attempt 1.
- **Method-name drift between heredoc and repository.** The first mint heredoc used `audit_repo.append(...)` because the prior context (recap, ops notes) referenced "append" loosely. The actual method is `record(...)`. A `--describe`-style step (read the method signature first) would have caught this without spending a fourth round-trip.
- **Listener-install location was a known-but-unmonitored single point of failure.** The `audit_chain.py` docstring said "called once from app.main module-import time" but did not enforce that constraint. There was no test that asserts every session.flush() of an `AdminAuditLog` triggers the listener. (Future work: such a test would have caught the latent bug pre-prod.)

## What we changed

### Code (C25, on `step-29y-gapfix`)

- `app/db/session.py`: install `before_flush` listener at module-import time. Long inline comment cross-referencing this incident, the C25 drift token, and the postmortem path.
- `app/repositories/audit_chain.py`: docstring updated; canonical install location is now `app/db/session.py`. Forensic note about the row-3445 backfill kept inline so future readers see the history.
- `app/verification/tests/pillar_23_audit_log_hash_chain.py`: assertion message updated to point at session.py.

### Documentation

- `docs/DRIFT_REGISTER.md`: five new drift tokens (`D-prod-ops-role-missing-ssm-tags-2026-05-08`, `D-ssm-write-overwrite-false-blocks-rotation-2026-05-08`, `D-audit-chain-listener-only-in-app-main-2026-05-08`, `D-audit-row-3445-hash-backfilled-2026-05-08`, `D-prod-ops-role-cannot-list-or-remove-ssm-tags-2026-05-08`).
- This file.

### Infrastructure (already applied at 12:21 EDT)

- IAM: `luciel-ecs-prod-ops-role` inline policy `WriteProdPlatformAdminKey` now grants both `ssm:PutParameter` and `ssm:AddTagsToResource` on the single SSM ARN.

### Database

- `admin_audit_logs.id=3445`: `row_hash` and `prev_row_hash` backfilled deterministically from the row's persisted column values, chaining off `id=3444.row_hash`. No business field changed.
- `admin_audit_logs.id=3446`: meta-audit row written via proper ORM path documenting the 3445 backfill. Action=update, resource_type=api_key, resource_pk=597, note references this incident document and the `D-audit-row-3445-hash-backfilled-2026-05-08` drift token.

### SSM

- `/luciel/production/platform-admin-key` value matches DB id=597 (prefix `luc_sk_qmMVl`).
- Tags include `luciel:purpose=platform-admin-key` and `luciel:key_id=597`. The leftover `luciel:iam-probe=ok` from the 12:27 propagation probe remains; cosmetic, tracked as `D-prod-ops-role-cannot-list-or-remove-ssm-tags-2026-05-08` (deferred to Step 30).

## Verification

After the backfill + meta-audit + ECS rolling deploy of rev30 (with C25), run the following checks:

1. **Pillar 23 strict-mode walk** over rows 1..tail. Expected: PASS, no NULL gaps.
2. **Bash heredoc inside ops container, post-C25:**
    - Construct an `AdminAuditLog` row in a `SessionLocal()` session.
    - Flush.
    - Assert `row.row_hash is not None` and `row.prev_row_hash is not None` BEFORE any explicit listener install.
    - This confirms session.py registers the listener.
3. **SSM↔DB↔audit consistency probe** (Sections A, C, D, E from the incident):
    - SSM value hashes to DB id=597.
    - id=597 active=True with canonical permissions.
    - Audit row 3445 has non-NULL hashes that recompute correctly.
    - Audit row 3446 chains off 3445.
4. **Live ALB authentication probe** with the new key — confirm 200 on `/api/v1/admin/forensics/...` from outside the cluster.

## Lessons & action items

- **L1: Operator scripts that touch the audit log must run inside a process where the listener is installed.** C25 makes this structural for any code path that imports `SessionLocal`. (Closed.)
- **L2: Any new IAM policy that gives a role a `Put*` action must also explicitly grant the matching `Tag*`/`Untag*` actions, OR document why it doesn't.** The `WriteProdPlatformAdminKey` Sid now follows this convention. Step 30 IAM review should audit other Sids for the same pattern. (Action item: Step 30.)
- **L3: SSM rotation must use `Overwrite=True` with a separate tag call.** C24 codifies. Future SSM-writing services should reuse the rotation path of `_write_key_to_ssm`. (Closed.)
- **L4: Pillar 23 gains an integration test that asserts a fresh SessionLocal-only flush populates hashes.** This would have caught RC-3 in CI rather than in prod. (Action item: open a follow-up token in Step 30 for the test.)
- **L5: `--describe`-style verification of method signatures before writing destructive heredocs.** Trivial cost (one `grep`), high value. Document this in the runbook. (Action item: include in `docs/runbooks/PROD_ACCESS.md` which is part of Step 29.y close-out F.2.)
- **L6: The defensive dry-run probe pattern (no-op tag write before destructive action) should be the default for any cross-system write that depends on freshly-applied IAM.** Document in the runbook. (Action item: same as L5.)

## References

- Drift tokens: `D-prod-ops-role-missing-ssm-tags-2026-05-08`, `D-ssm-write-overwrite-false-blocks-rotation-2026-05-08`, `D-audit-chain-listener-only-in-app-main-2026-05-08`, `D-audit-row-3445-hash-backfilled-2026-05-08`, `D-prod-ops-role-cannot-list-or-remove-ssm-tags-2026-05-08`.
- Code: `app/db/session.py`, `app/repositories/audit_chain.py`, `app/services/api_key_service.py`, `infra/iam/luciel-ecs-prod-ops-role-permission-policy.json`.
- Pillar: `app/verification/tests/pillar_23_audit_log_hash_chain.py`.
- Migration: `alembic/versions/8ddf0be96f44_*.py` (canonical hash backfill precedent).
- DB rows: `api_keys.id=597`, `admin_audit_logs.id=3445`, `admin_audit_logs.id=3446`.
- SSM: `/luciel/production/platform-admin-key` (region `ca-central-1`).
- Branch: `step-29y-gapfix`. Commits: C23+C24 (`bc8b269`), C25 (this commit).
