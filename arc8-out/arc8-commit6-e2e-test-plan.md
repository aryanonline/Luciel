# Arc 8 — Commit 6 E2E Test Plan Runbook (WU-6)

**Purpose:** Procedural, partner-triggerable execution runbook for live E2E testing. This is NOT a re-write of `docs/E2E_USER_STORIES.md` (the per-tier behavior canon, 613 lines) — it is the **execution sequence** the partner runs in order, with concrete inputs, observable expected outputs, and pass/fail criteria per step.

**Pre-flight:** All Arc 8 hardening complete (C1-C5 shipped). Prod state: `luciel-backend:93` / `luciel-worker:46` on `arc8-c3-5f83d9d`. `/health` 200, `/ready` 200 `{db:ok,redis:ok}`, `/api/v1/version` 200.

**Doctrine:** Pause only on hard gate failures. Each step has a clear pass criterion — if it passes, advance. If it fails, capture evidence and pause for partner.

---

## 0. Pre-flight (run once before E2E)

### 0.1 Confirm prod is live

```bash
curl -sS https://api.vantagemind.ai/health | jq .
# Expect: {"status":"ok","service":"Luciel Backend"}

curl -sS https://api.vantagemind.ai/ready | jq .
# Expect: {"status":"ready","checks":{"db":"ok","redis":"ok"}}

curl -sS https://api.vantagemind.ai/api/v1/version | jq .
# Expect: {"app":"Luciel Backend","version":"0.1.0","git_sha":"<sha>","status":"ok"}
```

### 0.2 Confirm marketing site live

```bash
curl -sSI https://www.vantagemind.ai | grep -i 'HTTP\|content-type'
# Expect: HTTP/2 200, content-type: text/html
```

### 0.3 Identify test inboxes

You need **two real inboxes you control**:
- `BUYER_EMAIL` — for signup + Stripe Checkout (must pass MX validation, must be reachable)
- `TEAMMATE_EMAIL` — for the team invite path (S5 / J4)

If SES sandbox exit (drift §B in C5 packet) has NOT been approved yet, both inboxes must be added to SES verified identities — otherwise email delivery silently no-ops to non-allowlisted addresses. Verify at: SES Console → Identities → Create identity → Email address.

### 0.4 Identify test card

Use Stripe test card `4242 4242 4242 4242`, any future expiry, any CVC. **CRITICAL:** confirm prod Stripe is using LIVE keys (`sk_live_...`), in which case the 4242 card will fail. Two options:
- (a) Switch to a real low-value card and refund after E2E
- (b) Use the **Stripe Customer Portal** to issue a 100% promo code for the price IDs `price_1TacunRytQVRVXw71i6eCx1K` (enterprise monthly) — recommended

---

## 1. J1 — Anon → Pricing → Signup → Email Verification (Free tier)

### 1.1 Visit pricing page

Navigate: `https://www.vantagemind.ai/pricing`

**Pass:** Page renders. Three tier cards visible (Free / Pro / Enterprise). "Get started" / "Upgrade" / "Contact sales" CTAs visible.

### 1.2 Click "Get started" on Free tier → signup form

**Pass:** Form has email + password fields. hCaptcha widget present (drift `D-amplify-apex-spa-rewrite-deep-path-404` may force this to load on `www.` apex — if 404, use `www.vantagemind.ai/signup` directly).

### 1.3 Submit signup with `BUYER_EMAIL`

- Email: `BUYER_EMAIL`
- Password: strong test password
- Captcha: complete

**Pass:** 200 response. UI shows "Check your inbox to verify your email."
**Fail (1-per-IP gate):** If you tested earlier today from the same IP, response is 429/409 — Free-tier signup IP gate (Arc 6 C6). Wait 24h or use a different IP/VPN.

### 1.4 Verify email arrival

Check `BUYER_EMAIL` inbox within 60s.

**Pass:** Email from `notifications@vantagemind.ai` arrives. Subject like "Verify your VantageMind email." Click the verify link.
**Fail:** No email → check SES sandbox status (drift §B), check CloudWatch logs for `send_email` errors, check spam folder.

### 1.5 Email verification redirect

**Pass:** Browser lands on app dashboard at `https://www.vantagemind.ai/dashboard` (or equivalent). Session cookie `luciel_session` set.

---

## 2. J7 — Owner creates first Instance (Free tier cap = 1)

### 2.1 Create LucielInstance #1

UI → "Create Instance" → fill Profession (any), submit.

**Pass:** Instance created, listed on dashboard.

### 2.2 Try to create LucielInstance #2 — should hit cap

UI → "Create Instance" again.

**Pass (Free cap enforced):** 403 / friendly error: "Free tier allows 1 instance. Upgrade to Pro for 10." (exact copy may vary; key is the cap is enforced)
**Fail:** Cap not enforced → escalate (Arc 7 C4 doctrine violation).

---

## 3. J7 — Mint embed key, paste widget snippet, chat as EndUser

### 3.1 Mint embed key on Instance #1

UI → Instance #1 → Embed → Generate key.

**Pass:** Key revealed ONCE (one-time reveal pattern, S7.2). Copy it. UI shows the `<script>` snippet for embedding.

### 3.2 Embed widget on a test page

Save the snippet to a local HTML file `widget-test.html`:
```html
<!DOCTYPE html>
<html><body>
<h1>Widget E2E test</h1>
<script src="https://www.vantagemind.ai/widget.js" data-embed-key="<your-key>"></script>
</body></html>
```

Open in a browser.

**Pass:** Widget loads, chat bubble visible.

### 3.3 Send a chat message via widget

Type: "Hello, what can you do?" → send.

**Pass:** Bot responds (typically within 5s) with a response grounded in the Instance's Profession config.

### 3.4 Hit the embed-key rate limit

Quickly send 31+ messages in <60s (the per-embed-key cap for Free is 30 rpm — `api_rate_limit_rpm=30` / `embed_key_count_cap=1` floor-divide = 30 rpm). Easiest: paste a JS snippet in DevTools console:

```js
for (let i = 0; i < 35; i++) {
  fetch('https://api.vantagemind.ai/api/v1/widget/chat', {
    method: 'POST',
    headers: {'Content-Type': 'application/json', 'X-Embed-Key': '<your-key>'},
    body: JSON.stringify({message: 'spam ' + i})
  }).then(r => console.log(i, r.status));
}
```

**Pass (Arc 8 C3 enforcement):** First ~30 return 200; thereafter return **429** with response body containing `{"detail": "...", "bucket_scope": "embed_key"}` — the C3 bucket_scope-aware response.
**Fail:** Cap not enforced → Arc 8 C3 doctrine violation; check `luciel-backend` CloudWatch logs for SlowAPI errors.

---

## 4. J2 — Upgrade Free → Pro via Stripe Checkout

### 4.1 Click "Upgrade to Pro"

UI → Settings → Billing → Upgrade.

**Pass:** Redirected to Stripe Checkout hosted page.

### 4.2 Complete checkout

- Card: test 4242 or live card (per 0.4)
- Email: `BUYER_EMAIL` (should be pre-filled and **MX-validated** — Arc 8 C2 gate)

**Pass:** Stripe processes payment. Redirects to a success page on vantagemind.ai.
**Fail (deliverability gate):** If checkout reports "Email address rejected" pre-payment → that's the Arc 8 C2 `email_validator` MX-check working as designed (test with a typo'd email like `foo@nodomain.invalid` to confirm gate fires).

### 4.3 Provisioning idempotency

Within 10s, check dashboard.

**Pass:** Tier badge shows "Pro." Instance cap is now 10 (try creating a 2nd Instance — should succeed). Embed-key cap is 10. `api_rate_limit_rpm` is 300; per-embed-key floor = 30 rpm (300/10).

### 4.4 Stripe webhook idempotency (manual replay)

Optional but doctrine-aligned: From Stripe Dashboard → Webhooks → find the `customer.subscription.created` event → click "Resend." Replay the same event 3x.

**Pass:** Dashboard tier remains "Pro." No double-provisioning, no duplicate audit-log row, no error. (Arc 7 idempotent webhook contract — `D-tier-provisioning-tenant-id-kwarg-mismatch` C2 fix confirmed.)

---

## 5. J4 / J5 — Invite a teammate (Pro tier flat-team shape)

### 5.1 Send invite

UI → Team → Invite teammate → enter `TEAMMATE_EMAIL`, role `teammate`.

**Pass:** Invite created. Email arrives in TEAMMATE inbox within 60s with a redeem link.

### 5.2 Redeem invite

In incognito / different browser, click the redeem link → set password → submit.

**Pass:** Teammate lands on dashboard. ScopeAssignment role=`teammate` on (tenant, default-domain `general`).

### 5.3 Teammate creates an Instance

**Pass:** Succeeds — counts against tenant's instance cap (now 10 minus already-created Instances).

---

## 6. J7 — Hit the Pro per-instance rate-limit

Take the Pro tenant from §4-5. Pro `api_rate_limit_rpm=300`, `instance_count_cap=10` → per-Instance floor = 30 rpm.

### 6.1 Burst 35 chat requests against a single Instance (authenticated admin chat, NOT widget)

```bash
TOKEN="<owner's jwt or session cookie>"
INSTANCE_ID="<one of the instances>"
for i in $(seq 1 35); do
  code=$(curl -s -o /dev/null -w '%{http_code}' \
    -H "Authorization: Bearer $TOKEN" \
    -H "X-Instance-Id: $INSTANCE_ID" \
    -X POST https://api.vantagemind.ai/api/v1/chat \
    -d '{"message":"burst '$i'"}')
  echo "req $i -> $code"
done
```

**Pass:** First ~30 return 200; thereafter return 429 with `bucket_scope: "tier_admin_instance"` in the response. (Arc 7 C4 + Arc 8 C3 composition.)

---

## 7. J8 — Downgrade Pro → Free (period-end cancel)

### 7.1 Cancel in Stripe Customer Portal

UI → Settings → Billing → "Manage subscription" → Cancel.

**Pass:** Stripe schedules cancellation at period end. Dashboard shows "Pro until <period-end-date>."

### 7.2 Fast-forward via webhook (optional, requires Stripe CLI)

```bash
stripe events resend <subscription.deleted-event-id>
# OR use a clock-advance fixture
```

**Pass:** Tier badge flips to "Free." Excess Instances (anything above Free cap of 1) get one of:
- Marked `is_active=False` (preferred per cascade doctrine)
- Owner prompted to pick which one to keep

Verify which behavior is live by checking dashboard immediately after webhook fires.

### 7.3 Cascade integrity verification

After downgrade, check:
- Instance over-cap rows have `is_active=False` (DB inspection or admin UI)
- Embed-key over-cap rows similarly
- Outstanding invites: still valid (no cascade — teammates remain functional)
- No 500s on dashboard, no orphan ScopeAssignments

**Pass:** Six-pillar reliability + maintainability holds.

---

## 8. J8.3 / J11 — Full cancellation + tenant deactivation

### 8.1 Owner deactivates tenant

UI → Settings → Danger zone → Deactivate tenant → confirm with typed phrase.

**Pass:** Multi-step confirm UI. On submit:
- Tenant row marked `is_active=False`
- All 13 cascade layers run in one DB transaction (Step 30a.7 cascade reconciler)
- Owner is logged out

### 8.2 Belt-and-suspenders middleware

Owner attempts to log back in with existing cookie.

**Pass (Step 30a.7 middleware):** Every authenticated request to a deactivated tenant returns 403 with `{detail: "tenant deactivated"}`. Session is unusable.

### 8.3 Orphan-zero verification

Run the cascade integrity probe (from Step 30a.7 invariant suite):

```bash
# Connect to RDS prod read-replica (or prod primary if no replica)
# Query each cascade table for orphans:
SELECT 'scope_assignments' as t, COUNT(*) FROM scope_assignments sa
  LEFT JOIN tenants t ON sa.tenant_id = t.id
  WHERE t.id IS NULL OR t.is_active = false AND sa.is_active = true;
# Repeat for: user_invites, sessions, synthetic_users, instances, embed_keys,
# audit_log, billing_subscriptions, etc. (full 13-layer list in Step 30a.7 record)
```

**Pass:** Every query returns 0 rows. Refund-safety invariant holds.

---

## 9. Deliverability sweep

### 9.1 Verify all transactional emails arrived

Recall what should have been sent during the run:
- §1.4 — signup verification email
- §5.1 — teammate invite email
- §4.2 — Stripe Checkout email receipt (Stripe-side, not SES)
- §7.1 — Stripe cancellation confirmation (Stripe-side)
- §8.1 — tenant deactivation confirmation (if implemented)

**Pass:** All Luciel-sent emails (§1.4, §5.1, §8.1) arrived. From: `notifications@vantagemind.ai`. Reply-To: `support@vantagemind.ai`.

### 9.2 Reply-to mailbox check (drift §C — open)

Reply to the §1.4 verification email.

**Pass criterion:** If drift §C is RESOLVED, the reply lands at `support@vantagemind.ai` and reaches a human-monitored inbox. If drift §C is still OPEN (no mailbox wired yet), the reply will bounce or land nowhere — **this is the current expected state; do not fail E2E on this**.

---

## 10. Post-E2E cleanup

### 10.1 Refund test payment

If a real card was used in §4.2: Stripe Dashboard → find the charge → Refund.

### 10.2 Delete test tenants

If §8 wasn't run (or partial), manually deactivate the test tenant. SQL fallback for the partner:

```sql
UPDATE tenants SET is_active = false WHERE owner_email = '<BUYER_EMAIL>';
-- Then trigger cascade reconciler (or wait for the next periodic sweep)
```

### 10.3 Capture run record

Save the E2E run output to `arc8-out/arc8-e2e-run-<date>.md` with:
- Per-section pass/fail
- Screenshots of any failure
- CloudWatch correlation IDs for any 5xx
- Stripe event IDs for any anomaly

---

## Pass/Fail Summary Matrix

| § | What it proves | Hard fail = block ship |
|---|---|---|
| 0 | Prod alive | YES |
| 1 | Signup + verify + IP gate + captcha | YES |
| 2 | Free instance cap | YES |
| 3 | Widget chat + Arc 8 C3 embed-key rate limit | YES |
| 4 | Stripe Checkout + Arc 8 C2 email validation + idempotent provisioning | YES |
| 5 | Team invite + redeem | YES |
| 6 | Pro per-instance rate limit (Arc 7 C4 + Arc 8 C3) | YES |
| 7 | Downgrade cascade | YES |
| 8 | Cancellation + cascade + middleware belt | YES |
| 9.1 | Email deliverability for verify/invite/deactivate | YES |
| 9.2 | Reply-to mailbox | NO (drift §C deferred) |

A clean run of §1-§8 + §9.1 = **Arc 8 pre-E2E hardening is operationally verified.** Ship-ready.

---

## Drifts that will be exercised (and tested against expected behavior)

| Drift | E2E section | Expected behavior |
|---|---|---|
| `D-health-endpoint-shallow...` (C1 closed) | §0.1 | `/ready` returns DB+Redis status separately from `/health` |
| `D-stripe-checkout-no-email-validation` (C2 closed) | §4.2 | MX-fail email rejected pre-payment |
| `D-tier-provisioning-tenant-id-kwarg-mismatch` (C2 closed) | §4.3, §4.4 | Provisioning succeeds first-time; idempotent on replay |
| `D-pro-tier-rate-limit-abuse-surface` (C3 closed) | §3.4, §6.1 | 429 with `bucket_scope` |
| `D-no-internal-smoke-path-for-direct-alb` (C4 closed) | (operator-axis, not E2E) | Run `luciel-smoke-probe:1` per C5 §E to verify |
| `D-amplify-apex-spa-rewrite-deep-path-404` (open §A) | §1.2 | If apex 404 on signup, use `www.` directly — expected until §A lands |
| `D-ses-sandbox-exit-pending` (open §B) | §1.4 | Verified-only inboxes work; non-allowlisted emails silently no-op |
| `D-ses-reply-to-monitored-inbox-not-confirmed` (open §C) | §9.2 | Reply may bounce — expected until §C lands |

---

**This document is the E2E execution plan. After a clean run, append the run record to `arc8-out/arc8-e2e-run-<date>.md` and ping agent for the envelope close (C7).**
