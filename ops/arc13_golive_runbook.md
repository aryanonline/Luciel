# Arc 13 — Go-Live Runbook (Twilio SMS + luciel-mail.com inbound)

**Status: GATED.** Everything in this runbook is held behind explicit
founder go. Nothing here is provisioned automatically. The platform is
build-to-deployable-state and dev-validated; this document is the
step-by-step for the three prerequisites that a human operator executes
**after** the founder says go.

Region: `ca-central-1`  Account: `729005488042`

The IaC this runbook applies:
- `cfn/luciel-ses-inbound.yaml` — SES inbound receipt rule set + S3 + SNS.
- `cfn/luciel-twilio-webhook-routing.yaml` — ALB listener rule making the
  Twilio inbound webhook path reachable.
- `td-backend-rev78.json` — backend task-def carrying the new SSM
  `secrets:` references and the inbound mail env config.

The runtime number-acquisition flow (Admin toggle → purchase a real
Twilio number on demand) is built by the backend slice. This runbook
establishes the **platform prerequisites** that flow depends on; once
they exist, the per-instance flow purchases against them with no further
manual steps.

---

## The three gated prerequisites

| # | Prerequisite | Why it is founder-gated |
|---|--------------|--------------------------|
| 1 | Flip the platform live switch | Turns dev-validated build into a live, billable, customer-facing surface. |
| 2 | Fund + acquire real Twilio numbers/brand | Spends real money (account funding + per-number monthly + per-message). |
| 3 | 10DLC A2P brand + campaign registration | Carrier-mandated for US A2P SMS; failing it gets traffic filtered/blocked and can incur carrier fines. |

Execute them **in order** (3 → 2 are inter-dependent; 1 is flipped last,
only after verification passes). The recommended sequence is:
SES inbound infra → Twilio brand/campaign (3) → Twilio funding+numbers
(2) → final verification → flip live (1).

---

## Step 0 — Populate SSM parameters (no real provisioning yet)

All Twilio/SES-inbound secrets are read by the backend task-def
(`td-backend-rev78.json`) as `secrets:` from SSM, and the non-secret
config from `environment:`. Create each parameter as a **SecureString**
(secrets) or **String** (non-secret) in `ca-central-1`.

### SSM parameters this arc introduces

| SSM parameter | Type | Maps to env var → config field | Notes |
|---------------|------|-------------------------------|-------|
| `/luciel/production/twilio_account_sid` | SecureString | `TWILIO_ACCOUNT_SID` → `twilio_account_sid` | Twilio account SID (`AC...`). |
| `/luciel/production/twilio_auth_token` | SecureString | `TWILIO_AUTH_TOKEN` → `twilio_auth_token` | Auth token; also validates `X-Twilio-Signature` on inbound. |
| `/luciel/production/twilio_messaging_service_sid` | SecureString | `TWILIO_MESSAGING_SERVICE_SID` → `twilio_messaging_service_sid` | Messaging Service (`MG...`) owning the A2P-registered pool. |
| `/luciel/production/twilio_api_key_sid` | SecureString | `TWILIO_API_KEY_SID` → `twilio_api_key_sid` | Optional rotatable API Key (`SK...`). |
| `/luciel/production/twilio_api_key_secret` | SecureString | `TWILIO_API_KEY_SECRET` → `twilio_api_key_secret` | Secret half of the API Key. |
| `/luciel/production/ses_inbound_topic_arn` | SecureString | `SES_INBOUND_TOPIC_ARN` → `ses_inbound_topic_arn` | Inbound SNS topic ARN; trust gate on the inbound-email route. |

Non-secret config injected via `environment:` (already set in the
task-def, listed here for completeness — populate as String params only
if you later choose to externalize them):

| Env var → config field | Value | Notes |
|------------------------|-------|-------|
| `MAIL_INBOUND_DOMAIN` → `mail_inbound_domain` | `luciel-mail.com` | Inbound mail domain. |
| `SES_INBOUND_BUCKET` → `ses_inbound_bucket` | `luciel-mail-inbound-prod` | Inbound MIME bucket (output of the inbound stack). |

> Boot safety: every Twilio field and `ses_inbound_topic_arn` default to
> empty string in `app/core/config.py`. The backend boots with them
> unset; the Twilio/inbound routes fail **closed (501)** rather than
> 500 until the slots are populated. So you can deploy the task-def
> **before** the real values exist and flip them in afterward.

Example (placeholder values — DO NOT commit real secrets):

```bash
aws ssm put-parameter --region ca-central-1 --type SecureString \
  --name /luciel/production/twilio_account_sid     --value "ACxxxxPLACEHOLDER"
aws ssm put-parameter --region ca-central-1 --type SecureString \
  --name /luciel/production/twilio_auth_token      --value "PLACEHOLDER"
aws ssm put-parameter --region ca-central-1 --type SecureString \
  --name /luciel/production/twilio_messaging_service_sid --value "MGxxxxPLACEHOLDER"
aws ssm put-parameter --region ca-central-1 --type SecureString \
  --name /luciel/production/twilio_api_key_sid     --value "SKxxxxPLACEHOLDER"
aws ssm put-parameter --region ca-central-1 --type SecureString \
  --name /luciel/production/twilio_api_key_secret  --value "PLACEHOLDER"
aws ssm put-parameter --region ca-central-1 --type SecureString \
  --name /luciel/production/ses_inbound_topic_arn  --value "PLACEHOLDER"
```

> The backend execution role (`luciel-ecs-execution-role`) must already
> have `ssm:GetParameters` + `kms:Decrypt` (via the SSM service
> principal) on `/luciel/production/*`; it does, since the existing
> Stripe/JWT secrets read the same path. No IAM change is required here.

---

## SES inbound infra — deploy `cfn/luciel-ses-inbound.yaml`

This is not one of the three founder-gated business prerequisites; it is
the platform plumbing those prerequisites' verification depends on, so
deploy it first.

> **Region caveat:** SES *inbound* receipt rules are only available in a
> subset of regions (historically `us-east-1`, `us-west-2`, `eu-west-1`).
> If `ca-central-1` does not offer SES inbound at deploy time, deploy
> **only this stack** to a supported inbound region; the S3 bucket,
> backend, and all other infra stay in `ca-central-1`. The template's
> `Region`-independent ARNs and the cross-region S3 read are documented
> in the template header. Confirm current region support before applying.

### A. Verify the mail domain in SES

1. In SES (deploy region), add domain identity `luciel-mail.com`.
2. Publish the SES-provided DKIM CNAME records and the domain
   verification TXT record in the `luciel-mail.com` DNS zone.
3. Wait for SES to mark the identity **Verified** (DKIM **Success**).

### B. Add the inbound MX record

Point `luciel-mail.com` mail at the SES inbound endpoint for the deploy
region:

```
luciel-mail.com.   MX   10   inbound-smtp.<deploy-region>.amazonaws.com.
```

(e.g. `inbound-smtp.us-east-1.amazonaws.com` if the inbound stack is in
`us-east-1`.) Only **one** MX is needed for SES inbound.

### C. Deploy the stack

```bash
aws cloudformation deploy \
  --template-file cfn/luciel-ses-inbound.yaml \
  --stack-name luciel-ses-inbound \
  --capabilities CAPABILITY_NAMED_IAM \
  --region <ses-inbound-supported-region>
```

Creates: `luciel-mail-inbound-prod` bucket (+ SES-write bucket policy),
`luciel-mail-inbound` SNS topic (+ SES-publish topic policy), and the
`luciel-inbound-ruleset` receipt rule set with one store-and-notify rule.

### D. Stamp the SSM params from the stack outputs

```bash
aws cloudformation describe-stacks --stack-name luciel-ses-inbound \
  --region <deploy-region> \
  --query "Stacks[0].Outputs"
# → InboundBucketName, InboundTopicArn, InboundRuleSetName

aws ssm put-parameter --region ca-central-1 --overwrite --type SecureString \
  --name /luciel/production/ses_inbound_topic_arn \
  --value "<InboundTopicArn output>"
# SES_INBOUND_BUCKET is already the task-def default (luciel-mail-inbound-prod);
# update only if the stack created a differently-named bucket.
```

### E. Activate the receipt rule set

The stack creates the rule set but does **not** activate it (only one
set can be active per account/region; activating is a deliberate step):

```bash
aws ses set-active-receipt-rule-set \
  --rule-set-name luciel-inbound-ruleset \
  --region <deploy-region>
```

### F. Subscribe the backend to the inbound topic

The backend inbound-email route (`POST /api/v1/inbound-email`, backend
slice) HTTPS-subscribes to the topic. Do this **after** the backend
image carrying the route is deployed:

```bash
aws sns subscribe --region <deploy-region> \
  --topic-arn "<InboundTopicArn>" \
  --protocol https \
  --notification-endpoint "https://api.vantagemind.ai/api/v1/inbound-email"
```

Confirm the `SubscriptionConfirmation` (the route auto-confirms by
fetching `SubscribeURL`, mirroring the existing `ses-events` route).

---

## Twilio webhook routing — deploy `cfn/luciel-twilio-webhook-routing.yaml`

Makes `https://api.vantagemind.ai/api/v1/twilio/*` reachable by adding a
listener rule to the existing prod ALB. Gather the four required ARNs/ids
first:

```bash
# HTTPS:443 listener on the prod ALB fronting api.vantagemind.ai
aws elbv2 describe-listeners --region ca-central-1 \
  --load-balancer-arn <prod-alb-arn> \
  --query "Listeners[?Port==\`443\`].ListenerArn"

# Backend target group ARN (reuse — the webhook goes to the same tasks)
aws elbv2 describe-target-groups --region ca-central-1 \
  --query "TargetGroups[?contains(TargetGroupName,'backend')].TargetGroupArn"

# ALB SG and backend-task SG ids
aws elbv2 describe-load-balancers --region ca-central-1 --load-balancer-arns <prod-alb-arn> \
  --query "LoadBalancers[0].SecurityGroups"
aws ecs describe-services --region ca-central-1 --cluster <cluster> \
  --services luciel-backend-service \
  --query "services[0].networkConfiguration.awsvpcConfiguration.securityGroups"
```

> Before deploying, check existing listener-rule priorities so the
> chosen priority (default `40`) does not collide, and check whether an
> ALB→backend SG ingress on port 8000 already exists (it does if
> `/api/v1/*` is already routed to these tasks — if so, the
> `BackendIngressFromAlb` resource is redundant; either remove it from
> the template before deploy or expect/ignore a duplicate-rule error):

```bash
aws elbv2 describe-rules --region ca-central-1 --listener-arn <listener-arn> \
  --query "Rules[].Priority"
```

Deploy:

```bash
aws cloudformation deploy \
  --template-file cfn/luciel-twilio-webhook-routing.yaml \
  --stack-name luciel-twilio-webhook-routing \
  --parameter-overrides \
     HttpsListenerArn=<listener-arn> \
     BackendTargetGroupArn=<backend-tg-arn> \
     AlbSecurityGroupId=<alb-sg-id> \
     BackendSecurityGroupId=<backend-sg-id> \
  --region ca-central-1
```

Stack output `TwilioWebhookUrl` = the URL Twilio must POST to
(`https://api.vantagemind.ai/api/v1/twilio/*`).

---

## Prerequisite 3 — 10DLC A2P brand + campaign registration

US carriers require A2P 10DLC registration before SMS sends reliably.
Do this in the Twilio Console (Messaging → Regulatory Compliance →
A2P 10DLC) or via the Twilio API. **This is founder-gated** — it submits
the business's legal identity to The Campaign Registry (TCR) and incurs
registration fees.

1. **Create the Brand.** Submit business legal name, EIN/tax id,
   business type, address, and contact. This is the registered sender
   identity for `luciel-mail.com`'s parent business (VantageMind).
2. **Wait for brand vetting.** TCR returns a brand status + trust score.
   Standard vetting can take minutes to days.
3. **Create the Campaign.** Choose the use-case (e.g. *Mixed* /
   *Customer Care* / *2FA* as appropriate for Luciel's conversational +
   appointment SMS), provide sample messages, opt-in/opt-out language,
   and the call-to-action describing how end-users consent.
4. **Attach the Campaign to a Messaging Service.** Create (or reuse) the
   Messaging Service whose SID goes into
   `/luciel/production/twilio_messaging_service_sid`. Numbers purchased
   per-instance at runtime are added to **this** Messaging Service so
   they inherit the registered campaign.
5. **Record the Messaging Service SID** into SSM (Step 0).

> Until the campaign is **Approved**, do not flip the platform live
> switch for SMS — sends will be filtered or blocked by carriers.

---

## Prerequisite 2 — Fund + acquire real Twilio numbers/brand

1. **Fund the Twilio account** to cover number purchases + projected
   message volume (and any TCR/vetting fees from Prerequisite 3).
2. **Record account credentials** into SSM (Step 0):
   `twilio_account_sid`, `twilio_auth_token`, and (recommended) an
   **API Key** pair (`twilio_api_key_sid` / `twilio_api_key_secret`) so
   the credential used by the running service is rotatable without
   touching the root auth token.
3. **Per-instance numbers are NOT bulk-purchased here.** Acquisition is
   **purchase-on-demand**: when an Admin toggles SMS on for their
   instance, the backend slice's runtime flow buys a number through the
   Twilio API and attaches it to the registered Messaging Service. This
   prerequisite only ensures the **account is funded and the Messaging
   Service/campaign exist** so that on-demand purchase succeeds against a
   real, A2P-approved sender pool.
4. **Register the inbound webhook on the Messaging Service.** Set the
   Messaging Service's inbound "A message comes in" webhook to the routed
   URL from the Twilio routing stack:
   ```
   https://api.vantagemind.ai/api/v1/twilio/inbound-sms
   ```
   (HTTP POST.) Numbers added to the service inherit this webhook, so
   on-demand-purchased numbers route inbound SMS to the backend with no
   per-number configuration. Optionally set the delivery-status callback
   under the same `/api/v1/twilio/*` path.

---

## Prerequisite 1 — Flip the platform live switch

Flip **last**, only after the verification checklist below passes end to
end. The live switch is the platform-level enablement consumed by the
runtime SMS flow (owned by the backend slice). Flipping it turns the
Admin SMS toggle into a real, billable number purchase instead of a
dev/no-op path.

- Populate the real (non-placeholder) values for every Twilio SSM
  parameter and `ses_inbound_topic_arn`.
- Roll the backend ECS service to `td-backend-rev78` (or later) so the
  task picks up the new `secrets:`/`environment:` entries.
- Enable the platform live flag per the backend slice's live-switch
  contract (config flag / SSM param it defines). **Do not invent a flag
  here** — confirm the exact name with the backend slice before flipping;
  this infra slice deliberately does not own that switch.

---

## Verification checklist — prove each leg is wired before going live

Run top to bottom. Every box must be checked before Prerequisite 1.

### SES inbound
- [ ] `luciel-mail.com` shows **Verified** + DKIM **Success** in SES.
- [ ] `dig MX luciel-mail.com` returns the `inbound-smtp.<region>.amazonaws.com` record.
- [ ] `aws ses describe-active-receipt-rule-set --region <region>` returns `luciel-inbound-ruleset`.
- [ ] Bucket `luciel-mail-inbound-prod` exists; its policy allows `ses.amazonaws.com` PutObject under `inbound/` scoped to account `729005488042`; TLS-only deny present; all public access blocked.
- [ ] SNS topic `luciel-mail-inbound` exists; topic policy allows `ses.amazonaws.com` Publish scoped to the account.
- [ ] Backend HTTPS subscription to the topic is **Confirmed** (`aws sns list-subscriptions-by-topic`).
- [ ] `/luciel/production/ses_inbound_topic_arn` SSM value equals the live topic ARN; backend `SES_INBOUND_TOPIC_ARN` env resolves to it.
- [ ] **End-to-end:** send a test email to a `@luciel-mail.com` address → confirm an object lands under `s3://luciel-mail-inbound-prod/inbound/` AND the backend logs an inbound-email notification.

### Twilio routing
- [ ] `aws elbv2 describe-rules --listener-arn <arn>` shows the `path-pattern /api/v1/twilio/*` rule forwarding to the backend target group at the expected priority.
- [ ] ALB→backend SG ingress on port 8000 exists (from this stack or pre-existing).
- [ ] `curl -i https://api.vantagemind.ai/api/v1/twilio/inbound-sms` reaches the backend (expect the handler's auth/validation response, e.g. 403 on missing `X-Twilio-Signature` — NOT an ALB 404/502).

### Twilio account / A2P
- [ ] Twilio account funded; balance covers number + message + vetting costs.
- [ ] A2P **Brand** registered and vetted (status returned by TCR).
- [ ] A2P **Campaign** status = **Approved**.
- [ ] Campaign attached to the Messaging Service whose SID is in `/luciel/production/twilio_messaging_service_sid`.
- [ ] Messaging Service inbound webhook = `https://api.vantagemind.ai/api/v1/twilio/inbound-sms` (POST).
- [ ] All Twilio SSM params hold real (non-placeholder) values; backend rolled to a task-def revision that reads them; Twilio routes no longer 501.

### Go-live
- [ ] Backend service running `td-backend-rev78` (or later) and healthy.
- [ ] Platform live switch flipped per the backend slice's contract.
- [ ] **End-to-end:** an Admin SMS toggle in a test instance triggers a real on-demand number purchase, the number joins the Messaging Service, an inbound test SMS reaches `/api/v1/twilio/inbound-sms`, and an outbound reply is delivered.

---

## Rollback notes

- **SES inbound:** `aws ses set-active-receipt-rule-set` with no
  `--rule-set-name` deactivates inbound processing without deleting the
  stack. The S3 bucket + SNS topic are `Retain`-policy'd, so a stack
  delete does not destroy in-flight mail.
- **Twilio routing:** delete the `luciel-twilio-webhook-routing` stack to
  remove the listener rule; the backend keeps serving `/api/v1/*` via its
  existing default rule. Removing the rule makes `/api/v1/twilio/*`
  unreachable from the internet (inbound SMS stops) but does not affect
  any other path.
- **Live switch:** flip the platform live flag back off to return the
  Admin SMS toggle to its dev/no-op behavior; Twilio credentials can stay
  in SSM (routes simply go quiet).
