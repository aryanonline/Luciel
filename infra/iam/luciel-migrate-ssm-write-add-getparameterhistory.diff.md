# P3-G: Add `ssm:GetParameterHistory` to `luciel-migrate-ssm-write`

**Scope:** single-action diff to the existing inline policy
`luciel-migrate-ssm-write` attached to IAM role `luciel-ecs-migrate-role`.

**Why this is a single-action diff:** the prior session asserted the
migrate role was missing both `ssm:GetParameter` and `ssm:PutParameter`.
A real read of the live policy on 2026-05-03 showed both are already
present. The only missing action is `ssm:GetParameterHistory`, which
is what the hardened mint script (`scripts/mint_worker_db_password_ssm.py`,
commit `2b5ff32`) uses to verify a parameter's prior version metadata
without reading the value. The migrate role uses this for migration-
related SSM bookkeeping, NOT for reading the admin DSN (that read is
deliberately moved to the new `luciel-mint-operator-role` per P3-K).

**Why bundled with P3-K:** both are IAM-side changes touching policies
on roles in the same account. Bundling means one IAM-changes commit
instead of two.

---

## Current `luciel-migrate-ssm-write` (live, read 2026-05-03)

```jsonc
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ReadWriteSsmParameters",
      "Effect": "Allow",
      "Action": [
        "ssm:GetParameter",
        "ssm:PutParameter",
        "ssm:DescribeParameters",
        "ssm:AddTagsToResource",
        "ssm:ListTagsForResource"
      ],
      "Resource": [
        "arn:aws:ssm:ca-central-1:729005488042:parameter/luciel/production/*",
        "arn:aws:ssm:ca-central-1:729005488042:parameter/luciel/bootstrap/*"
      ]
    },
    {
      "Sid": "KmsViaSsm",
      "Effect": "Allow",
      "Action": [
        "kms:Encrypt",
        "kms:Decrypt",
        "kms:GenerateDataKey"
      ],
      "Resource": "*",
      "Condition": {
        "StringEquals": {
          "kms:ViaService": "ssm.ca-central-1.amazonaws.com"
        }
      }
    }
  ]
}
```

---

## Diff (one line added)

```diff
   "Action": [
     "ssm:GetParameter",
+    "ssm:GetParameterHistory",
     "ssm:PutParameter",
     "ssm:DescribeParameters",
     "ssm:AddTagsToResource",
     "ssm:ListTagsForResource"
   ],
```

---

## Final `luciel-migrate-ssm-write` (after P3-G)

See `infra/iam/luciel-migrate-ssm-write-after-p3-g.json` (sibling file).

---

## Verification (post-apply, read-only)

```powershell
aws iam get-role-policy `
    --role-name luciel-ecs-migrate-role `
    --policy-name luciel-migrate-ssm-write `
    --query 'PolicyDocument.Statement[?Sid==`ReadWriteSsmParameters`].Action' `
    --output json
```

Expected output: a JSON array containing `ssm:GetParameterHistory`
alongside the other five SSM actions.

---

## Rollback

If anything breaks, revert by reapplying the pre-P3-G version of the
policy via `aws iam put-role-policy`. The pre-P3-G policy is preserved
verbatim in the **Current** section above of this file.
