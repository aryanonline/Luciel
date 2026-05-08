# Step 28 — D1 Closure: Local Platform-Admin Key Rotation

**Date:** 2026-04-27
**Operator:** aryan
**Branch:** `step-28-hardening`
**Audit row id:** 1997
**Drift item closed:** D1 (from Step 24.5b canonical recap §3)

---

## Context

On 2026-04-26 during the Step 27c-final / Step 24.5b operational arc, the
local dev platform-admin key id=158 (prefix `luc_sk_HY_RK`, full string
`luc_sk_HY_RK_mywB7x-3WM9Oclb4hQeE6RrVTK5qcDOm2Q7RGQ`) was pasted into chat
during a verification session. The exposure was logged as drift item D1 in
the Step 24.5b canonical recap with the note "HARD GATE before any
external demo / GTA brokerage outreach."

This runbook documents the rotation event that closes D1.

## Threat model

- **Scope:** Local dev environment only. The leaked key was never granted
  scope on prod (`tenant_id=NULL` is local-only; prod platform-admin key
  is id=3, prefix `luc_sk_kHqA2`, lives in password manager only and has
  zero exposure events).
- **Blast radius:** Anyone with the leaked string and access to a local
  Luciel dev environment could authenticate as platform-admin against
  that local DB. Local Postgres is not exposed beyond `127.0.0.1`, so
  the practical attack surface required local machine access.
- **Why rotate anyway:** (1) Discipline — security posture must match
  what we will tell prospects during tech due diligence. (2) Chat
  history is durable; the leak is not theoretical. (3) Rotation is
  cheap (~30 min) and the cost of pitching with a known-open admin key
  is real.

## Pre-rotation state
id=8 prefix=luc_sk_rWQ0a active=True tenant_id=None
id=15 prefix=luc_sk_GsDhB active=True tenant_id=None
id=16 prefix=luc_sk_VzJmX active=True tenant_id=None
id=158 prefix=luc_sk_HY_RK active=True tenant_id=None ← rotation target

## Procedure executed

1. **Mint replacement key** via `scripts.mint_platform_admin_ssm` —
   raw key written to SSM SecureString at
   `/luciel/bootstrap/admin_key_539`, never to stdout/CloudWatch.
   New key: id=539, prefix `luc_sk_lsxv7`, `tenant_id=NULL`,
   permissions `[chat, sessions, admin, platform_admin]`.
2. **Retrieve raw key** from SSM into PowerShell session env var
   `$env:LUCIEL_PLATFORM_ADMIN_KEY`. Length 50 chars (Step 27a-pattern).
3. **Verify 14/14 with new key** via `python -m app.verification` —
   all pillars green including Pillars 12/13/14 (Q6 cascade).
4. **Save raw key to password manager** under entry "Luciel Local
   Platform-Admin Dev Key (Step 28 D1 rotation, 2026-04-27)". Clipboard
   cleared post-paste.
5. **Delete SSM bootstrap path** via
   `aws ssm delete-parameter --name /luciel/bootstrap/admin_key_539`.
   Raw key now exists only in password manager + this PowerShell session.
6. **Deactivate id=158** via direct DB update inside single transaction
   with audit row written by `AdminAuditRepository.record(...)` using
   `AuditContext.system(label="step28-d1-rotation")`. Audit row id 1997.
7. **Confirm 401** — old key string against
   `GET /api/v1/sessions` returned `401` as expected.

## Post-rotation state
id=8 prefix=luc_sk_rWQ0a active=True tenant_id=None
id=15 prefix=luc_sk_GsDhB active=True tenant_id=None
id=16 prefix=luc_sk_VzJmX active=True tenant_id=None
id=158 prefix=luc_sk_HY_RK active=False tenant_id=None ← deactivated
id=539 prefix=luc_sk_lsxv7 active=True tenant_id=None ← replacement

## Forensic anchor

`admin_audit_logs.id = 1997`
- `action = deactivate`
- `resource_type = api_key`
- `resource_pk = 158`
- `actor_label = step28-d1-rotation`
- `actor_permissions = system`
- `created_at = 2026-04-27 17:57:15 UTC`
- `note = D1 closure: rotated due to LEAKED_2026_04_26 chat exposure.
  Replaced by id=539 (luc_sk_lsxv7). Step 28 hardening sprint,
  first commit.`

## Durable rules established

1. **Platform-admin keys (local and prod) live in password manager
   only.** Never persist to `.env` or any tracked file. Set via
   session env var `$env:LUCIEL_PLATFORM_ADMIN_KEY = '<raw>'` per shell.
2. **Raw keys never appear in stdout, logs, or chat.** SSM-direct mint
   pattern (`scripts.mint_platform_admin_ssm` with `ssm_write=True`)
   is the only sanctioned mint path.
3. **Every rotation writes an audit row** in the same transaction as
   the `active=False` flip. The audit row is the forensic record of
   the rotation event, not the commit message or this runbook.
4. **If a key is exposed in chat, treat it as fully compromised** even
   if scope is local-only. Rotate within 24 hours of detection.

## Step 28 sprint context

This is the first commit of the Step 28 hardening sprint, branch
`step-28-hardening`. D1 was the gate before any external demo or GTA
brokerage outreach could begin. With D1 closed, outreach is unblocked.

Remaining Step 28 Phase 1 work (per Step 24.5b canonical recap §4):
- Consent route double-prefix bug (`/api/v1/api/v1/consent/*`)
- D11 `memory_items.actor_user_id` NOT NULL flip after orphan sweep
- Separate `luciel_worker` Postgres role (least-privilege at DB layer)
- Dedicated `luciel-worker-sg` security group

Phase 1 closes when those four ship and prod redeploy completes.
