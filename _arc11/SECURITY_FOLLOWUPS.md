# Arc 11 — Security Follow-ups

Two security items surfaced during Arc 11 that are **NOT** Arc 11's
job to fix but **must** be tracked through to closure. Founder
owns the schedule.

This file is checked in to the repo. It deliberately does NOT
contain the actual key value — only the key ID. Rotating the key
requires direct AWS console / `aws iam` CLI access; the key value
never enters the repo.

---

## SF1 — Rotate long-lived agent IAM key

**Finding:** The "Sandbox Agent Key Credentials" file in shared
space artifact storage holds a long-lived AWS access key
`AKIA2TPA466VF3RNG47L` with `Action: "*"` / `Resource: "*"`.

**Risk:** Highest-blast-radius IAM shape possible. The key sits in
shared artifact storage (not version control, but still externally
reachable) and grants unconstrained access to every AWS service in
the account.

**Remediation plan:**

1. **Mint a scoped replacement.** Create a new IAM principal
   (`luciel-platform-operator` or per-service users — see scope
   options below) with least-privilege policies:
   - `s3:*` scoped to `arn:aws:s3:::luciel-knowledge-prod-*`,
     `arn:aws:s3:::luciel-data-exports-prod-*`,
     `arn:aws:s3:::luciel-widget-cdn-*`.
   - `ecr:*` scoped to the `luciel-backend` repository only.
   - `ecs:UpdateService`, `ecs:RegisterTaskDefinition`,
     `ecs:DescribeServices`, `ecs:DescribeTaskDefinition` on the
     `luciel-cluster` only.
   - `ssm:GetParameter`, `ssm:PutParameter` on
     `/luciel/production/*` only.
   - `rds:DescribeDBInstances` (read-only) on the `luciel-rds-*`
     resource pattern.
   - Explicit `Deny` for `iam:*`, `organizations:*`,
     `account:*`, `kms:Delete*`, and any
     `*:Delete*` against production-critical resources (see Arc 9
     C8.3 `cfn/luciel-sandbox-agent-policy.yaml` for the locked
     scoped-policy pattern — replicate that shape).
2. **Test the new key against a non-prod target first** (the
   sandbox environment if available; otherwise a `staging` AWS
   account).
3. **Rotate the active key in any system that uses it** —
   update SSM, CI variables, agent config, etc.
4. **Revoke `AKIA2TPA466VF3RNG47L`** via `aws iam
   update-access-key --access-key-id AKIA2TPA466VF3RNG47L
   --status Inactive` first (giving 24h to catch any forgotten
   consumer), then `aws iam delete-access-key` after the soak.
5. **Wipe the artifact-storage copy** of the old key.

**Timeline:** Post-Arc-11 close. Founder-driven. ARC11_PLAN.md
§0.8 + §12 flagged this; not Arc 11's job to fix, but Arc 11's job
to write down.

---

## SF2 — DB password rotation for `luciel_app` role

**Finding:** The Postgres password for the `luciel_app` role
propagates from a `SecureString` SSM parameter into the Fargate
task environment as cleartext. Standard Fargate behavior (the SSM
secret decrypts into the task's env at boot), but during the Arc 11
production verification the URL containing the password was
echoed to the ECS Exec session output stream — visible to anyone
with `ssm:StartSession` on the task.

**Risk:** A compromised or forensic ECS Exec session capture would
include the DB password. Production-relevant; the `luciel_app`
role is NOBYPASSRLS but has CRUD on every customer-data table
under RLS.

**Remediation plan:**

1. Mint a fresh password via `scripts/mint_app_db_password_ssm.py`
   (Arc 9 C10.b shipped this script for exactly this purpose).
   The script rotates a random 32-char password and updates both
   `/luciel/<env>/luciel_app/password` and
   `/luciel/<env>/app_database_url`.
2. Restart the backend + worker services so they pick up the new
   password from SSM at boot.
3. Verify the old password no longer works:
   `PGPASSWORD=<old> psql ...` should fail.
4. Sweep CloudWatch logs / ECS Exec session archives for the
   old password value, redact if found (post-incident hygiene).

**Timeline:** Post-Arc-11 close. Should fire BEFORE the first
customer onboarding (zero prod customers as of Arc 11 close per
§12, so the window is wide).

---

## SF3 — `luciel_app` connection-URL surface in ECS Exec output

**Finding:** Beyond the password rotation in SF2, the **mechanism**
that leaks the URL to ECS Exec is that the URL is set as a
container env var (`DATABASE_URL`), and a `printenv` call inside
the session prints all env vars including the cleartext URL.

**Risk:** Lower than SF2 (the cleartext is only visible to a
caller already authenticated to `ssm:StartSession`), but worth
hardening.

**Remediation plan options (pick one):**

1. **(Cheapest)** Update operator runbook to never `printenv` /
   `env` inside a prod ECS Exec session. Add to the same operator
   playbook that already says "never `cat /etc/luciel/secrets`."
2. **(Medium)** Switch the backend's connection-string assembly
   from a single `DATABASE_URL` env var to per-component
   secrets (`DB_HOST`, `DB_USER`, `DB_PASSWORD_FILE` →
   read from `/run/secrets/...`). Reduces the cleartext surface
   to the password file alone.
3. **(Heavy)** Move to IAM database authentication via
   `rds.AuthToken`. Eliminates the cleartext password entirely;
   the worker mints a short-lived token at connection time.

**Timeline:** Post-Arc-11; lowest priority of the three SF items.
Pair with SF2.

---

## What this file does NOT contain

- The actual value of any secret. Only key IDs and SSM parameter
  paths.
- Any production-environment hostnames beyond what's already in
  the repo's CFN templates.
- Tactical AWS console screenshots or step-by-step recipes — that
  belongs in the operator runbook, not in a code-checked-in file.

If a future security incident review needs the actual value, it
lives in the AWS console / SSM parameter store, not here.
