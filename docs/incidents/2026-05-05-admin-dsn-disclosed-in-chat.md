# INCIDENT — Admin DSN disclosed in chat transcript

**Date:** 2026-05-05 ~08:52 EDT
**Severity:** HIGH (compromise of bootstrap secret, no evidence of exploitation)
**Status:** OPEN — rotation in progress
**Reporter:** Computer (advisor agent), self-flagged in real time
**Impacted secret:** Postgres `luciel_admin` user password
**Impacted resource:** SSM parameter `/luciel/database-url`

## Timeline

- ~08:50 EDT: During P3-S Half 1, advisor needed to verify worker DB name to construct the `luciel-mint` ECS task definition correctly.
- 08:51 EDT: Advisor asked user to run `aws ssm get-parameter --name /luciel/database-url --with-decryption ...` and paste the output **with the password redacted as `<REDACTED>`**, providing an explicit example of correct redaction format.
- 08:52 EDT: User pasted the full DSN with the password **unredacted in plaintext**.
- 08:52 EDT: Advisor halted P3-S work, opened this incident record, recommended immediate rotation.

## Exposure surface

The admin password traversed:
1. Local PowerShell terminal scrollback (still on the operator's machine)
2. Chat transport between operator and advisor
3. Advisor's context window (this conversation)
4. Any logging or telemetry layer in the chat infrastructure
5. Browser/client-side chat history persistence

## Threat model assessment

**Realistic exploitation likelihood: LOW**
- RDS endpoint is in a private VPC subnet (not internet-reachable)
- No bastion host or VPN configured for external network access to the VPC
- An attacker would need both this password AND AWS-network-adjacent access to exploit

**But integrity of the audit story: BROKEN**
- The secret has touched unauthorized channels
- Cannot be undone
- Any future security review (customer, investor, regulator) will flag this
- Per stated business principle ("we cannot make any compromises in our security and programmatic errors"), this requires rotation, not acceptance

## Remediation plan

Sequential, one step at a time, advisor walks operator through each command:

1. **Rotate `luciel_admin` Postgres password.** Connect to RDS as superuser via existing channels, ALTER ROLE luciel_admin WITH PASSWORD '<new-strong-password>'. Generate password offline (e.g., `python -c "import secrets, string; alphabet=string.ascii_letters+string.digits+'-_'; print(''.join(secrets.choice(alphabet) for _ in range(48)))"`).
2. **Update SSM parameter** `/luciel/database-url` with new DSN. Use `aws ssm put-parameter --overwrite`.
3. **Verify rotation** by reading SSM and connecting via the new DSN through whatever existing channel works (migrate task, manual psql via approved path).
4. **Update this incident record** with rotation timestamp and new SSM parameter version.
5. **Resume P3-S work** only after rotation is verified.

## Lessons / process changes

1. **Never request secrets in chat, even with redaction instructions.** Redaction instructions failed once today — they will fail again. The advisor should have asked the user to verify the DB name *without* exposing the DSN, e.g., via `aws ssm get-parameter ... | <local extraction script>` or simply `aws rds describe-db-instances --query 'DBInstances[0].DBName'`.
2. **Add to operator-patterns.md:** Pattern for "advisor needs to verify a config value that lives inside a secret" — never the full secret. Always extract the specific non-secret field.
3. **Pre-commit incident: this incident itself becomes a process artifact.** Future advisor (or future user) reads this and knows the boundary.

## Drift register entry

`D-admin-dsn-disclosed-in-chat-2026-05-05` — admin DSN disclosed via chat transcript during P3-S Half 1; `luciel_admin` password rotation required before further mint-ceremony work; advisor process patched to never request secrets in chat.

## Sub-incident — wrong-subnet drift during rotation verification (09:08 EDT)

During Step 5a verification, advisor composed the `aws ecs run-task` command using subnet IDs pulled from the RDS describe-db-instances output (`subnet-0b315ad9ad4a8efb6`, `subnet-0cd66d8e9229aa122`). These are RDS's *DB subnets*, which intentionally have no SSM VPC endpoint or NAT egress.

Result: migrate task failed with `ResourceInitializationError: unable to pull secrets ... context deadline exceeded`. Initial reading suggested a production-affecting VPC networking issue.

After checking VPC endpoints, advisor discovered the SSM/ssmmessages/ec2messages endpoints exist in `subnet-0e54df62d1a4463bc` and `subnet-0e95d953fd553cbd1` — the application subnets, not the RDS DB subnets.

Root cause: advisor assumed subnet identity from RDS metadata instead of asking which subnets the application tasks actually use. Plausible-looking mistake; symptom matched a real possible failure mode (broken VPC endpoint).

Process change: before launching ad-hoc Fargate tasks, advisor must verify subnet identity by checking `aws ecs describe-services` for an existing service that runs in production, not by inferring from RDS or other unrelated infrastructure.

No production outage occurred. Real web/worker/migrate runs use the application subnets. Only the verification ceremony was affected.

## Drift register entry (additional)

`D-runbook-rotation-verify-wrong-subnets-2026-05-05` — verification ran in RDS DB subnets instead of application subnets due to advisor assumption; runbook process patched to require explicit subnet verification before ad-hoc task launch.
