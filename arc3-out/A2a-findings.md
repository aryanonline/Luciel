# Arc 3 Work-Unit A.2a — Findings

**Date:** 2026-05-21
**Closure drift:** `D-set-password-token-logged-plaintext-2026-05-17`
**Closing tag:** `arc-3-paired-prod-touch`
**Operator:** Aryan Singh (paired with VantageMind)
**Mode of execution:** Pattern O — ECS Fargate one-shot under `luciel-prod-ops:4`

---

## Scope

CloudWatch token-backlog audit identified 19 unique JWT JTIs leaked to
`/ecs/luciel-backend` in the discovery window **2026-05-13 → 2026-05-20**
(the welcome-set-password email path was emitting full tokens in URL
query strings prior to the Step 30a.4 patch).

The 19 JTIs were extracted to `arc3-out/leaked-welcome-jtis.txt` and
fed to `scripts/arc3_revoke_leaked_invites.sql` via the dry-run path
of `scripts/arc3_revoke_leaked_invites_run.py`, running inside the
prod VPC under the dedicated `luciel-prod-ops` task-def.

ECS task: `510e44d2fafc433bb2e81e2a6fbc1236`
Log stream: `arc3-prod-ops/luciel-prod-ops/510e44d2fafc433bb2e81e2a6fbc1236`
Container exit code: `0`
SQL transaction outcome: `ROLLBACK` (dry-run, no UPDATE executed)

---

## Bucket breakdown

| Bucket in `user_invites.status` | Count | Disposition |
|---|---|---|
| `pending` | **0** | No row-level remediation needed |
| `accepted` | 2 | Already redeemed by real users; revoking would lock those accounts out (wrong move) |
| `revoked` | 4 | Already terminal; idempotent no-op |
| `expired` | 0 | — |
| **No row in `user_invites` at all** | **13** | Not invite tokens — see "Residual exposure" below |
| **Total leaked JTIs scanned** | **19** | |

## Verdict

**No row-level remediation required on `user_invites`.** A live execution
of `arc3_revoke_leaked_invites.sql` against the prod database with
this JTI set would flip zero rows, by construction. Running it
anyway would add a no-op audit entry and consume the rest of the
Pattern-O budget for no traceability gain — declined on
ship-default grounds.

## Residual exposure (the 13 unmatched JTIs)

The 13 JTIs that did not match any row in `user_invites` are almost
certainly **not invite tokens** — the CloudWatch tokenizer scan
matches any JWT-shaped string in logs, and the leaked surface
includes login JWTs, password-reset JWTs, and welcome-email JWTs
in addition to invite JWTs. Those latter three classes are not
stored in `user_invites` and are revoked at the JWT-validation
layer via short TTL + signing-key rotation, not via row updates.

**Residual mitigation, deferred to Work-Unit B (already touching
SSM):** rotate the JWT HS256 signing key under
`/luciel/jwt-signing-key` and bump the `kid` header. This
invalidates all 13 unmatched JTIs (and every other token signed
under the old key) in a single SSM write, without requiring
per-row knowledge of which JTI belongs to which subsystem.

## Audit trail

A single hash-chain-safe entry was written to `admin_audit_logs`
to record the investigation itself (so the audit trail shows we
scanned, not just that we did nothing):

- `action = invite_revoked` (reused; resource sentinel marks it as a scan summary)
- `resource_type = user_invite`
- `resource_natural_id = "leak-scan-2026-05-21"`
- `tenant_id = SYSTEM_ACTOR_TENANT` (cross-tenant summary row)
- `after_json` = the bucket breakdown above + the full JTI list (UUIDs only, no token material)
- `note` contains `arc-3-leak-scan-summary` for filtering

Written by `scripts/arc3_audit_leak_scan_summary.py` running under the
same Pattern-O ECS task-def.

## Drifts captured during this work

| Drift ID | Description | Disposition |
|---|---|---|
| `D-luciel-admin-password-leaked-to-local-scrollback-2026-05-21` | psycopg traceback echoed live RDS password to laptop scrollback during early DSN debugging | Fold into Work-Unit B (already touching SSM): clear scrollback + rotate password |
| `D-arc3-td-rev3-stale-on-disk-2026-05-21` | `td-prod-ops-rev3.json` in repo is stale; registered `luciel-prod-ops:4` is the source of truth (`awslogs-stream-prefix=arc3-prod-ops`, image pinned to `sha256:350cc3b...`) | Arc 7 doc-truthing: regenerate `td-prod-ops-rev4.json` from `describe-task-definition`, commit |
| `D-arc3-em-dash-encoding-in-stdout-2026-05-21` | "DRY RUN — no UPDATE" line shows mojibake (`ù`) in CloudWatch text output; em-dash mangled by Python stdout encoding inside the Fargate container | Trivial: replace em-dashes with ASCII `--` in `arc3_revoke_leaked_invites_run.py` and `.sql`. Fold into Arc 7. |
| `D-arc3-cloudwatch-log-group-prefix-2026-05-21` | Log group is `/ecs/luciel-backend` (not `/aws/ecs/luciel-backend`); confirmed authoritative across `luciel-prod-ops:4` + all backend task-defs | Documentation-only — fix references in `diag_invite_email_revoke.ps1` doc-strings during Arc 7 |

## Six-pillar check

- **Scalability** — One-shot bounded to 19 JTIs; SQL is O(N) on a temp table; no production tenants impacted.
- **Reliability** — Pattern O ECS path proven (the dry-run completed cleanly under `luciel-prod-ops:4`); orchestrator tail-loop hardened against `ResourceNotFoundException` (`b3b8028`).
- **Maintainability** — Single audit-summary row keeps the chain readable; the row-by-row writer (`arc3_audit_leaked_invites_record.py`) is preserved for any future scan that *does* find pending matches.
- **Traceability** — Investigation is recorded on the hash-chain even though no mutation occurred (this is the whole point of writing the summary row instead of skipping audit).
- **Security** — Zero token material in the audit row; only JTI UUIDs and bucket counts. JWT signing-key rotation closes the residual on the 13 unmatched JTIs at the right layer.
- **Simplicity** — Declined the no-op live-revoke task; closed A.2a in one summary row instead of 0 row-level rows + 1 task that flips nothing.
