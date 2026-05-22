# B.4 — SES Sandbox Exit Request (Production Access)

**Date authored:** 2026-05-22 01:22 EDT
**Operator:** Aryan Singh (paired with Computer)
**Submission target:** AWS Support Center → Service Quotas → SES → "Request production access"
**Region:** `ca-central-1`
**AWS Account:** 729005488042
**Status (this doc):** READY TO SUBMIT — operator pastes into AWS Console form
**Refreshed:** 2026-05-22 ~15:41 EDT — Field 3 + Field 5 + Open Follow-Ups updated to reflect Arc 8 WU-6 Phase A (commit `c3d974f`) + Phase B B1/B2/B3 live state

---

## Pre-submission checklist

- [x] Domain `vantagemind.ai` is verified in SES (`sesv2 list-email-identities` confirms)
- [x] DKIM, SPF, and DMARC alignment for `vantagemind.ai` are configured
- [x] App emits structured CloudWatch logs for every send attempt (`[magic-link-email] sent via SES from=… message_id=…` and matching `SES send FAILED` warning lines) — bounce/complaint diagnosis path exists
- [x] Send paths swallow `ClientError`/`BotoCoreError` and audit failures via `admin_audit_log` — no user-visible crash on bad send
- [x] All recipients are explicit-opt-in (signup or admin-issued invite); no purchased lists, no broadcast newsletters
- [ ] **Operator action: submit ticket via AWS Console** ← this step is human-only

---

## AWS Support Form — Field-by-Field

### Field 1 — "What type of mail does your use case send?"

> **Transactional**

### Field 2 — "Website URL"

> https://vantagemind.ai

### Field 3 — "Describe in detail how you will send email using Amazon SES"

> VantageMind is a SaaS product for AI-assisted lead qualification and customer-relationship workflows in real estate brokerages. Our backend (a Python FastAPI service running on Amazon ECS Fargate in ca-central-1) sends three categories of transactional email through Amazon SES v2, all to recipients who have explicitly opted in via signup or admin-issued invite:
>
> 1. **Magic-link sign-in emails** — sent when a user requests passwordless sign-in to the application. The email contains a one-time, 24-hour-TTL signed URL that authenticates the user when clicked. One email per user-initiated sign-in attempt.
>
> 2. **Welcome / set-password emails** — sent immediately after a successful Stripe checkout completes for a new subscription, prompting the new buyer to set their account password. One email per completed checkout.
>
> 3. **Post-refund courtesy emails** — sent when a Stripe pilot refund event fires, notifying the buyer of refund completion and next steps. One email per refund event.
>
> All three flows are 1:1 transactional. There is no marketing list, no newsletter, no bulk send. The application uses the SESv2 `SendEmail` API exclusively (not `SendBulkEmail`). Send volume scales linearly with paying-customer count and is bounded by signup, login, and refund frequency.
>
> Sending is implemented in `app/services/email_service.py`. Every send is tagged with the SES Configuration Set `luciel-default`, which has Reputation Metrics enabled and an event destination publishing `Bounce`, `Complaint`, `Reject`, and `RenderingFailure` events to the SNS topic `arn:aws:sns:ca-central-1:729005488042:luciel-ses-events`. The IAM grant is the inline policy `LucielSESSendEmail` on the ECS task role `luciel-ecs-web-role`, allowing only `ses:SendEmail` and `ses:SendRawEmail` against identities in `arn:aws:ses:ca-central-1:729005488042:identity/*`. Region: `ca-central-1`. From-address: `noreply@vantagemind.ai`. Every outbound message also carries `Reply-To: support@vantagemind.ai` for recipient escalation.

### Field 4 — "How will your recipients opt in to receive email from you?"

> All recipients opt in by an affirmative action they take with VantageMind:
>
> - **Magic-link login emails:** the recipient initiates the email by entering their address into the VantageMind sign-in form and clicking "Send sign-in link". No email is sent without that explicit user action.
> - **Welcome / set-password emails:** the recipient initiates the email by completing a Stripe checkout for a paid VantageMind subscription. Stripe collects the email and our backend sends the welcome email only after Stripe webhook confirms `checkout.session.completed`.
> - **Post-refund courtesy emails:** sent only to a buyer whose Stripe payment we have refunded. The buyer is the same address that opted in at checkout, and the email is a one-time service notice about their own transaction.
>
> No address is added to a list. No address is shared, sold, or reused outside the specific transactional event that triggered the send. There is no broadcast, newsletter, or marketing flow.

### Field 5 — "How will you handle bounces and complaints?"

> 1. **CloudWatch logs** — every send attempt and outcome is recorded in the ECS task's CloudWatch log group `/ecs/luciel-backend` with a `message_id` field, allowing forward correlation with SES feedback events.
>
> 2. **Audit table** — every send attempt writes a row to `admin_audit_log` with action `EMAIL_SEND_*` and outcome (`SENT` / `FAILED`); failure rows include the SES error code and message. This gives us a queryable history of every email outcome per user.
>
> 3. **SES feedback loop (already live in the account at submission time):**
>    - Configuration Set `luciel-default` (Reputation Metrics enabled) is attached to every outbound send
>    - Event destination publishes `Bounce`, `Complaint`, `Reject`, and `RenderingFailure` events to SNS topic `arn:aws:sns:ca-central-1:729005488042:luciel-ses-events`
>    - Application-layer suppression schema (`email_send_event` durable record + `email_suppression` table with case-insensitive UNIQUE index) and the `EmailSuppressionService` are code-complete on `main` (commit `c3d974f`). Every send path now runs a pre-flight suppression check before calling SES; addresses flagged as bounced/complained are not re-attempted. The HTTPS subscriber that subscribes the backend to the SNS topic ships with the next backend deploy within days of this submission.
>
> Until the SNS HTTPS subscriber is in production, the SES Reputation dashboard and the CloudWatch log group `/ecs/luciel-backend` are our live monitors; any bounce or complaint is followed by a manual `EmailSuppressionService.record_suppression()` call within 24 hours so the address is permanently suppressed at the application layer.
>
> All transactional emails contain a plain-language identification of VantageMind, the From address `noreply@vantagemind.ai`, and a contact path (reply-to a monitored support address) for any recipient to flag a problem.

### Field 6 — "Expected daily send volume"

> **Initial:** 50 emails/day (currently in pilot with single-digit paying customers; magic-link and welcome flows trigger ≤5 sends/day per active user).
>
> **3-month projection:** 500 emails/day.
>
> **12-month projection:** 5,000 emails/day.
>
> Bursts are bounded by signup velocity. The current SES sending rate cap of 1 message/second is sufficient for our flows; we are not requesting an increase to the rate cap, only an exit from the sandbox so we can deliver to non-pre-verified addresses.

### Field 7 — "List acquisition" (if asked)

> No list acquisition. All sends are 1:1 transactional, triggered by an authenticated user action (sign-in request, paid checkout, or refund) tied to the recipient's own account.

### Field 8 — "Opt-out / unsubscribe handling" (if asked)

> All emails are transactional service notifications tied to specific account events. Recipients who no longer want emails can:
> 1. Stop using VantageMind (no further magic-link or welcome emails will fire)
> 2. Cancel their subscription (no further post-refund-courtesy or welcome emails will fire)
> 3. Reply to any of the emails to reach our monitored support inbox to be removed from all future sends
>
> Per CAN-SPAM / CASL guidance for transactional messages, emails identify VantageMind as the sender, give a contact path, and pertain only to the recipient's own service relationship.

---

## Open Follow-Ups (Drift Items Opened by This Ticket)

- `D-ses-feedback-loop-not-wired-2026-05-22` — Configuration Set + SNS bounce/complaint webhook → **SNS topic + Configuration Set landed at Arc 8 WU-6 Phase B (this submission window); HTTPS subscriber ships at Phase C with next backend deploy**
- `D-ses-suppression-app-layer-not-implemented-2026-05-22` — application-layer suppression on bounce/complaint → **code-complete on `main` at commit `c3d974f` (Arc 8 WU-6 Phase A); deploys at Phase C**
- `D-ses-reply-to-monitored-inbox-not-confirmed-2026-05-22` — `Reply-To: support@vantagemind.ai` is now set explicitly on every send (Arc 8 WU-6 Phase A code); monitored-mailbox confirmation remains operator-side

---

## Operator Submission Procedure

1. Sign in to AWS Console as IAM principal with permissions to file Service Quotas requests
2. Region selector → **ca-central-1**
3. Navigate: **Amazon SES → Account dashboard → "Request production access"** (or **Service Quotas → AWS services → Amazon Simple Email Service → "Request production access"**)
4. Paste the field text from this document into the matching form fields
5. Submit
6. Save the resulting Support Center case ID into this file under "Submission Record" below
7. AWS typically responds within 24-72 hours; if the response is a request for more detail, treat it as a normal support thread reply
8. On approval: re-run `aws sesv2 get-account --region ca-central-1 --query "ProductionAccess"` — must return `true`. Commit a record to `arc3-out/B4-ses-production-access-granted.md` and close `D-ses-sandbox-exit-pending-2026-05-22`

## Submission Record

- **Submission timestamp:** 2026-05-22 ~16:37 EDT
- **Support Center case ID:** 177948223100786
- **Account ID:** 729005488042
- **Subject (AWS-side):** SES: Production Access
- **Severity (AWS-side):** low
- **Submission form variant:** New streamlined form (Mail type + Website URL + Acknowledgement only; legacy long-form fields removed by AWS). Account-state introspection (`luciel-default` configuration set with `BOUNCE`/`COMPLAINT`/`REJECT`/`RENDERING_FAILURE` event destination to `arn:aws:sns:ca-central-1:729005488042:luciel-ses-events`; rightshaped `LucielSESSendEmail` inline policy on `luciel-ecs-web-role`; verified `vantagemind.ai` identity with DKIM/SPF/DMARC; Phase A code at commit `c3d974f`) is the substantive evidence AWS will review.
- **AWS first response timestamp:** (pending)
- **Outcome:** (pending — typical SLA 24–72h)
- **Closure verification command on approval:** `aws sesv2 get-account --region ca-central-1 --query "ProductionAccessEnabled"` must return `true`

## Drift Tracker

- **Opens (pending AWS):** `D-ses-sandbox-exit-pending-2026-05-22` (Arc 3 closure-deferred; awaiting AWS async approval)
- **Opens (Arc 8):** `D-ses-feedback-loop-not-wired-2026-05-22`, `D-ses-suppression-app-layer-not-implemented-2026-05-22`, `D-ses-reply-to-monitored-inbox-not-confirmed-2026-05-22`
