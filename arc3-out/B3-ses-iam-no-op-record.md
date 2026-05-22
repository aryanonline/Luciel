# B.3 — SES IAM Widen — Already Complete (Ledger Correction)

**Date investigated:** 2026-05-22 01:18 EDT
**Operator:** Aryan Singh (paired with Computer)
**Disposition:** **NO-OP — pre-existing, ledger entry corrected**

## Background

Arc 3 work-unit B.3 was scheduled as "IAM widen for SES" on the assumption that the backend task role lacked SES send permissions. Block 7q-r2 scout (this session, 2026-05-22 01:17 EDT) revealed that the role `luciel-ecs-web-role` already carries the inline policy `LucielSESSendEmail` granting the required actions on the correct resource scope.

## Evidence

### Existing Policy: `luciel-ecs-web-role` → `LucielSESSendEmail`

`aws iam get-role-policy --role-name luciel-ecs-web-role --policy-name LucielSESSendEmail` returned:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AllowSESSendFromVantagemindIdentity",
      "Effect": "Allow",
      "Action": [
        "ses:SendEmail",
        "ses:SendRawEmail",
        "ses:SendBulkEmail"
      ],
      "Resource": [
        "arn:aws:ses:ca-central-1:729005488042:identity/vantagemind.ai",
        "arn:aws:ses:ca-central-1:729005488042:identity/aryans.www@gmail.com"
      ]
    }
  ]
}
```

### Code Path That Uses It

`app/services/email_service.py` makes three SESv2 `send_email` calls (lines 270, 442, 605). All three:

- Use `boto3.client("sesv2", region_name=region)`
- Call `client.send_email(FromEmailAddress=settings.from_email, Destination={"ToAddresses": [to_email]}, Content={"Simple": {...}})`
- `settings.from_email` defaults to `noreply@vantagemind.ai` (on the verified `vantagemind.ai` identity)

### IAM-Action Compatibility (SES v1 vs SESv2)

AWS deliberately kept the IAM action namespace shared: SDK calls to the `sesv2` client's `send_email` route through to the same `ses:SendEmail` IAM action that v1 `send_email` uses. The existing grant covers SESv2 calls without modification.

### Documented Intent

`app/core/config.py` line 298 explicitly documents:

> "The task's IAM role (luciel-ecs-web-role) carries the LucielSESSendEmail inline policy scoped to the verified vantagemind.ai SES identity, so no additional credentials live in env vars."

This was shipped in commit `bc9abe1` (arc-2-backend-code-hygiene, 2026-05-20) — which referenced the policy by name in the config docstring; the policy itself predates Arc 2.

## Minor Over-Grants Noted (Non-Blocking)

- `ses:SendRawEmail` — unused (no raw-MIME sends in current code)
- `ses:SendBulkEmail` — unused (no bulk sends in current code)
- Identity `aryans.www@gmail.com` in the resource list — operator-personal verified identity, retained for ad-hoc smoke tests; can be removed when no longer needed

These are tracked as Arc 8 hardening line item `D-ses-iam-overgrant-unused-actions-2026-05-22` (low priority — over-grant on a single SES identity, not a credential-sprawl risk).

## Verification

| Check | Value |
|---|---|
| Role ARN | `arn:aws:iam::729005488042:role/luciel-ecs-web-role` |
| Policy name | `LucielSESSendEmail` (inline) |
| Required Action | `ses:SendEmail` ✓ present |
| Required Resource | `arn:aws:ses:ca-central-1:729005488042:identity/vantagemind.ai` ✓ present |
| SDK in code | `boto3.client("sesv2", ...)` (SESv2) ✓ |
| Action namespace | shared v1/v2 — grant covers both ✓ |

## Conclusion

B.3 requires no IAM change. Closure is a ledger correction — entry rewritten from "to-do" to "verified pre-existing".

## Drift Closures + Updates

- **Closes:** No drift to close (B.3 was a planned task, not a drift)
- **Opens:** `D-ses-iam-overgrant-unused-actions-2026-05-22` (Arc 8 hardening, low)
- **Adopts:** Standard #9 — `git grep` path filters MUST be verified against actual repo layout via `Get-ChildItem -Directory` before drawing scope conclusions
