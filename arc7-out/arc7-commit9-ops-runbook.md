# Arc 7 — Commit 9 Operator Runbook (WU-4)

Operator (partner) actions required to close three deferred-but-doc-truthed
infra surfaces. Each action is console-only — no code changes — so they can
land at the partner's next operator session without touching the deploy pipe.

**Sandbox agent IAM does NOT permit any of these actions.** Each must be
executed by the partner's IAM identity (root or an admin role).

---

## A. Amplify apex SPA-rewrite deep-path fix

**Drift:** `D-amplify-apex-spa-rewrite-deep-path-404-2026-05-20`
**Surface:** `vantagemind.ai/<deep-path>` returns 404; `www.vantagemind.ai/<deep-path>` returns 200.
**App:** `d1xf2f9605mosw` in Amplify console.

### Verify the drift is still real (before fixing)

```bash
curl -sSI https://vantagemind.ai/pricing
curl -sSI https://www.vantagemind.ai/pricing
```

Expected pre-fix: apex returns `HTTP/2 404`, www returns `HTTP/2 200`.
If apex already returns `301` with `location: https://www.vantagemind.ai/pricing`
the fix has already landed; skip to the verification step.

### Fix (Amplify Console click-path)

1. AWS Console → Amplify → Apps → **`d1xf2f9605mosw`** (`VantageMind` site)
2. Left sidebar → **Hosting → Rewrites and redirects**
3. Click **Add rule**. Use these values:
   - **Source address:** `https://vantagemind.ai/<*>`
   - **Target address:** `https://www.vantagemind.ai/<*>`
   - **Type:** `301 (Permanent redirect)`
   - **Country code:** leave blank
4. Click **Save**. Amplify applies new rewrite rules immediately
   (no rebuild required — they live at the edge, not in the bundle).
5. Confirm rule ordering: this 301 must sit AFTER any explicit asset-path
   rules (so `/assets/*.js` doesn't get redirected) and BEFORE the catch-all
   SPA-fallback (`/<*>` → `/index.html` 200). The Amplify console renders
   rules in evaluation order top-to-bottom.

### Verify the fix

```bash
curl -sSI https://vantagemind.ai/pricing
# Expect: HTTP/2 301, location: https://www.vantagemind.ai/pricing

curl -sSL https://vantagemind.ai/pricing -o /dev/null -w "final-status=%{http_code} final-url=%{url_effective}\n"
# Expect: final-status=200 final-url=https://www.vantagemind.ai/pricing
```

After verification, partner pings agent to apply DRIFTS.md §3 strikethrough +
§5 closure stanza in the next agent session.

---

## B. SES sandbox exit — case `177948223100786`

**Drift:** `D-ses-sandbox-exit-pending-2026-05-22`
**Status:** SUBMITTED to AWS Support 2026-05-22 ~16:37 EDT
**Account ID:** `729005488042` (`ca-central-1`)

### Check approval status

1. AWS Console → Support → **Your support cases**
2. Find case **`177948223100786`** ("SES: Production Access")
3. If status is **Resolved** with an approval message from AWS, proceed to
   the verification probe below. If still **In progress** or **Pending
   customer action**, no action — wait for AWS.

### Verification probe (after approval)

```bash
# Partner runs this with partner's own credentials (sandbox agent lacks
# ses:GetAccount IAM):
aws sesv2 get-account --region ca-central-1 --query "ProductionAccessEnabled"
# Expect: true
```

### Post-approval smoke

```bash
aws ses send-email \
  --region ca-central-1 \
  --from notifications@vantagemind.ai \
  --to <a-non-allowlisted-test-address> \
  --subject "Arc 7 C9 SES production access smoke" \
  --text "If you receive this, SES sandbox exit is live." \
  --configuration-set-name luciel-default
# Expect: MessageId returned, no AccessDeniedException
```

After smoke passes, partner pings agent to write the closure record at
`arc3-out/B4-ses-production-access-granted.md` and strikethrough the drift
in DRIFTS.md §3 / §5.

---

## C. SES reply-to inbox confirmation

**Drift:** `D-ses-reply-to-monitored-inbox-not-confirmed-2026-05-22`
**Status:** Code leg LIVE in prod (`ReplyToAddresses=["support@vantagemind.ai"]`
on every `send_email` call); operator-side confirmation that the mailbox
actually resolves to a human-monitored inbox is what remains.

### Three mailbox paths (partner picks one)

| Path | Cost | Effort | Risk | Notes |
|---|---|---|---|---|
| **(a) DNS migration GoDaddy → Cloudflare + Email Routing** | Free | High (touches `api.vantagemind.ai`, `www.vantagemind.ai`, SES DKIM CNAMEs, Amplify CNAME) | High during cutover | Doctrine-strongest long-term; correct for the production posture. |
| **(b) GoDaddy-native email forwarding** | ~$0 | ~5 min | Reliability known to be flaky | Fastest but the inbox-reliability dependency lives with GoDaddy. |
| **(c) Improvmx / Forwardemail.net (MX-record forwarder)** | Free tier | ~15 min (MX edit on GoDaddy) | Adds a vendor dependency | Cleanest near-term: only one DNS row changes, no migration. |

Partner's 2026-05-22 ~21:40 EDT decision was to defer this entire decision to a
dedicated post-Arc-5/6 mini-arc rather than stacking DNS-layer risk against
in-flight product surfaces. **Arc 7 does NOT close this drift either** — Arc 7's
hardening cohort is request-path + signup-path + tier-shape. DNS path A/B/C
remains the partner's call at the natural mailbox-arc moment.

### Verification once mailbox is wired

```bash
# Partner sends a test transactional email through prod, replies to it from
# a different mailbox, and confirms the reply arrives at support@vantagemind.ai.
# The closure evidence is a screenshot or message-id chain.
```

---

## D. Carry-forward — orphan SSM `floor_annual` deletion

**Drift:** `D-arc7-ssm-orphan-floor-annual-pending-console-delete-2026-05-24`

Sandbox agent lacks `ssm:DeleteParameter`. Partner deletes via console:

1. AWS Console → Systems Manager → Parameter Store
2. Filter on name = `/luciel/production/stripe_price_enterprise_floor_annual`
3. Select the parameter row → **Delete** → confirm

After deletion, partner pings agent to close the drift in DRIFTS.md.

---

## Summary

Three deferred-but-doc-truthed surfaces and one orphan SSM param. None
require code changes. All execute from the AWS Console with partner's
own IAM identity (sandbox-agent IAM cannot perform any of these).

Doctrine: Arc 7 closes the **request-path** abuse boundary (C4 tier-aware
RPM) and the **signup-path** fraud surface (C6 1-per-IP soft gate). The
deliverability boundary (SES sandbox exit + reply-to + suppression) and
the marketing-DNS boundary (Amplify apex 301) are operator-axis work that
properly sits outside the Arc 7 ship envelope — but their doc-truth needed
to be locked, which this Commit does.
