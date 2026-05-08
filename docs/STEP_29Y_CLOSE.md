# Step 29.y Close Checklist

This document defines the verify-then-tag procedure for closing Step 29.y. It exists because the `step-29y-impl` work landed (44 commits, ~5400 insertions past `step-29-complete`) without a `step-29y-complete` tag, and the gap-fix series on `step-29y-gapfix` is the alignment work that closes that gap. Drift token: `D-step29y-impl-no-close-tag-2026-05-07`.

The checklist is operator-runnable — every step is either a single command, a file inspection, or a pass/fail decision.

## Preconditions

Before running this checklist:

- [ ] `step-29y-gapfix` contains all 10 gap-fix commits (C1 through C10).
- [ ] Working tree is clean: `git status` reports nothing modified or staged.
- [ ] You are in a local development environment with project dependencies installed (the sandbox where these gap-fix commits were authored does not have the runtime deps).
- [ ] `step-29-complete` tag exists and points at `89afbae`. Confirm with `git rev-parse step-29-complete`.

## Phase 1 — Branch hygiene

1. Confirm branch state:
   ```
   git status
   git log --oneline step-29-complete..step-29y-gapfix | wc -l
   ```
   Expected: clean tree, 54 commits (44 from `step-29y-impl` + 10 gap-fix).

2. Confirm all 10 gap-fix drift tokens are present in commit subjects:
   ```
   git log --oneline step-29y-impl..step-29y-gapfix | grep -c "D-"
   ```
   Expected: 10.

3. Confirm `docs/DRIFT_REGISTER.md` lists all 10 tokens with statuses recorded.

## Phase 2 — Verification suite

Run the verification suite in JSON-report mode against current dev. Output goes to a dated file under `docs/verification-reports/` per the existing convention.

```
python -m app.verification --json-report docs/verification-reports/step29y_gapfix_2026-05-07.json
```

### Pass criteria

- [ ] All 25 pillars (P1 through P25) report status `FULL`.
- [ ] No pillar reports `PARTIAL` or `FAIL`.
- [ ] The JSON report file is written to `docs/verification-reports/step29y_gapfix_2026-05-07.json`.
- [ ] Pillar P23 (audit chain) `FULL` — this is the single most important gate; the C1 hybrid approach was specifically designed to keep P23 green by leaving historical row hashes untouched.

### If any pillar is not FULL

Do **not** push or tag. Open the JSON report, identify the failing pillar, and either:

- File a new drift token (format `D-<slug>-YYYY-MM-DD`), append it to `docs/DRIFT_REGISTER.md`, and land a fix commit on `step-29y-gapfix` before re-running this phase.
- Or, if the failure is environmental (missing dep, stale local DB), document the environment fix in your run notes and re-run.

## Phase 3 — Targeted test gates

Run the gap-fix-specific test suites added in C1 through C4. These exercise the exact code paths the gap-fix series modified.

```
pytest tests/integrity/test_actor_permissions_format.py
pytest tests/integrity/test_audit_note_length_cap.py
pytest tests/policy/test_scope_enforce_action.py
pytest tests/integrity/test_worker_audit_failure_counter.py
```

- [ ] All four files green.

## Phase 4 — Tag

Once Phase 2 and Phase 3 are both green:

1. Push the branch:
   ```
   git push origin step-29y-gapfix
   ```

2. Open a merge PR (or fast-forward locally per your existing workflow) that brings `step-29y-gapfix` into the integration branch the team uses for Step 30 prep.

3. Tag the merge point as `step-29y-complete`:
   ```
   git tag -a step-29y-complete -m "Step 29.y complete: 44 step-29y-impl commits + 10 gap-fix commits, 25/25 pillars FULL"
   git push origin step-29y-complete
   ```

4. Update `docs/DRIFT_REGISTER.md`: change `D-step29y-impl-no-close-tag-2026-05-07` row from `open` to `closed` and record the tag commit hash. Land that as a follow-up commit on the next branch.

## Phase 5 — Step 30 hand-off

After tagging:

- [ ] Carry-forward tokens in `docs/DRIFT_REGISTER.md` (`D-actor-permissions-storage-format-migration-step-30b-...`, Cluster 4b items) become the seed list for Step 30b sequencing.
- [ ] `docs/STEP_30_PREFLIGHT_GAP_REPORT.md` is the existing planning artifact for Step 30b run-up. Reconcile it with the carry-forward list as the first action of the next session.
- [ ] The May 25 broker meeting leave-behind, if generated, references `step-29y-complete` as the verified baseline.

## What this checklist does not cover

- Production deployment, AWS / SSM operations, or any step that mutates infrastructure. The Step 29.y window is code-only by design.
- Re-running the full pillar suite against staging or prod. That is Step 30b's responsibility.
- The `actor_permissions` storage-format migration. That is deferred under its own drift token; see `docs/STEP_29Y_DEFERRED.md`.
