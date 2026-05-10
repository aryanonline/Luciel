# Step 30b — Luciel Staging End-to-End Test Runbook

**Purpose:** Prove the full embed-key-issued / widget-loaded / message-exchanged loop on a Luciel-controlled origin before the first paying-customer drop (REMAX Crossroads). 24–48 hours observed clean closes the staging gate; only then does the Step 30b row in `CANONICAL_RECAP.md` flip from 🔧 to ✅ for first-customer scope.

**Owner:** Aryan Singh
**Backend:** Luciel production API at `https://api.vantagemind.ai` (intentional — see DRIFTS §5 closure of `D-prod-widget-bundle-cdn-unprovisioned-2026-05-09` for why we test against prod, not a parallel staging stack). Note: the production hostname uses the `vantagemind.ai` umbrella brand, not `luciel.<tld>`. This is a pre-existing operational choice; brand-surface alignment for the customer-facing widget (CloudFront raw host + API host) is a separate decision worth making before the REMAX Crossroads handoff (see Phase 6).
**CDN:** `https://d1t84i96t71fsi.cloudfront.net/widget.js` (production, already verified reachable on merge `5ffd42d`).
**Tenant:** dedicated `luciel-staging-widget-test` — never reused for any other purpose, never recycled for a paying customer.
**Embed origin:** Cloudflare Tunnel from local laptop (HTTPS, Cloudflare-issued cert) — `https://<tunnel-host>.trycloudflare.com` or a named tunnel under a domain you own.

---

## Why production backend, not a parallel staging backend

The whole point of this gate is to prove the path REMAX Crossroads will actually use. A parallel staging backend tests an infrastructure shape the real customer never touches; a green signal there does not transfer. The blast radius of testing against production is bounded by four mechanisms we already shipped:

1. **Dedicated tenant** — `luciel-staging-widget-test` cannot read or write any other tenant's data (scope-policy enforcement, Pattern D).
2. **Embed-key permission** — `chat`-only. No admin verbs, no tenant create, no key mint, no SSM reach.
3. **Origin enforcement** — `allowed_origins` is exact scheme+host+optional-port match. Any other origin presenting the key is rejected at the runtime gate.
4. **Rate-limit cap** — set to 30/min on this key. Enough for thorough manual testing; not enough to be useful if leaked. Operator can deactivate the key under Pattern E if anything looks odd.

If any of these four mechanisms is the thing that fails during the test, that is precisely the signal we need — better caught here than on REMAX Crossroads.

---

## Phase 0 — One-time prep (operator local laptop)

```powershell
# 0.1 Confirm production API base URL is reachable from your laptop.
curl https://api.vantagemind.ai/health

# Expected: HTTP/2 200, JSON body {"status":"ok","service":"Luciel Backend"}
# (The route is GET-only; `curl -I` returns 405 with `Allow: GET` — not an error.)

# 0.2 Install cloudflared if not already installed.
winget install --id Cloudflare.cloudflared

# 0.3 Pick a tunnel mode:
#       Option A (zero config, ephemeral host): cloudflared tunnel --url http://localhost:8000
#       Option B (named, stable host under a domain you own): cloudflared tunnel create luciel-staging
#     For a 24–48h observation window, Option B is more honest — the host stays stable.
```

---

## Phase 1 — Mint the staging tenant

Run from a host with platform_admin credentials to the production API. If you have a `scripts/create_tenant.py` CLI use that; otherwise the HTTP path:

```powershell
# 1.1 Create the staging tenant. The cleanup script convention reads the
#     admin key from $env:LUCIEL_PROD_ADMIN_KEY; re-use that pattern here so
#     the raw key never lands in shell history.
$AdminKey = $env:LUCIEL_PROD_ADMIN_KEY
if (-not $AdminKey) { Write-Error "LUCIEL_PROD_ADMIN_KEY not set" }

curl -X POST https://api.vantagemind.ai/v1/admin/tenants `
  -H "Authorization: Bearer $AdminKey" `
  -H "Content-Type: application/json" `
  -d '{
    "tenant_id": "luciel-staging-widget-test",
    "display_name": "Luciel Staging Widget Test (2026-05-10)",
    "tier": "standard"
  }'

# Expected: HTTP 201, response body echoes tenant_id, created_at timestamp.
# If 409 Conflict: tenant already exists from a prior run — skip to phase 2.

# 1.2 Verify tenant landed.
curl -X GET https://api.vantagemind.ai/v1/admin/tenants/luciel-staging-widget-test `
  -H "Authorization: Bearer $AdminKey"
```

A LucielInstance under this tenant must exist before the chat path will accept messages. Create one (default persona, no special config) per the existing admin flow before phase 2.

---

## Phase 2 — Mint the staging embed key

```powershell
# 2.1 Start the named Cloudflare tunnel and capture the host it gives you.
#     Run this in a dedicated terminal that stays open for the 24–48h window.
cloudflared tunnel --url http://localhost:8000
# Note the *.trycloudflare.com hostname it prints. Example: foo-bar-baz.trycloudflare.com

# 2.2 Set the host as a variable for the next command.
$TunnelHost = "https://<host-from-cloudflared-output>"

# 2.3 Mint the embed key. Run from the Luciel repo root with the venv active.
python -m scripts.mint_embed_key `
  --tenant-id luciel-staging-widget-test `
  --display-name "Luciel staging widget test (2026-05-10)" `
  --origins $TunnelHost `
  --rate-limit-per-minute 30 `
  --widget-display-name "Luciel Staging" `
  --greeting-message "This is the staging test widget. Real conversations end up in the staging tenant audit log." `
  --created-by "aryan@step30b-staging-e2e"

# Expected stdout: metadata block + raw key on one line, prefixed `luciel_embed_`.
# CRITICAL: copy the raw key once. It is shown only here. If lost, deactivate
# the row under Pattern E and re-mint.
```

---

## Phase 3 — Build the test page

Save to `C:\Users\aryan\staging-widget-test\index.html` (or any local path). Replace the three `<<<...>>>` placeholders.

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Luciel Widget Staging Test — 2026-05-10</title>
  <meta name="robots" content="noindex,nofollow">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    body { font-family: system-ui, sans-serif; max-width: 720px; margin: 4rem auto; padding: 0 1rem; color: #222; }
    h1 { font-size: 1.4rem; margin-bottom: 0.4rem; }
    .meta { color: #666; font-size: 0.85rem; margin-bottom: 2rem; }
    pre { background: #f5f5f5; padding: 1rem; border-radius: 6px; font-size: 0.8rem; overflow-x: auto; }
    .note { background: #fff8d6; border-left: 4px solid #e6c200; padding: 0.8rem 1rem; margin: 1.5rem 0; font-size: 0.9rem; }
  </style>
</head>
<body>
  <h1>Luciel Widget — Staging End-to-End Test</h1>
  <p class="meta">Staging tenant: <code>luciel-staging-widget-test</code> &middot; Page first served: 2026-05-10 &middot; Backend: production</p>

  <p>This page exists to prove the embed-key-issued / widget-loaded / message-exchanged loop end-to-end on a Luciel-controlled origin. If you are not Aryan or someone he sent here, please close this tab — conversations on this page land in the staging audit log and are observed during the validation window.</p>

  <div class="note">
    <strong>Validation window:</strong> 24–48 hours observed clean. After that, the Step 30b row in CANONICAL_RECAP §12 stays at 🔧 until REMAX Crossroads (first paying-customer drop) also lands clean.
  </div>

  <pre id="config"></pre>

  <script>
    // Echo the script-tag config to the page so screenshots and console logs
    // can correlate to a specific minted key without exposing the full secret.
    window.addEventListener("DOMContentLoaded", () => {
      const tag = document.querySelector("script[data-luciel-embed-key]");
      if (!tag) return;
      const key = tag.getAttribute("data-luciel-embed-key") || "";
      const apiBase = tag.getAttribute("data-luciel-api-base") || "";
      document.getElementById("config").textContent =
        "data-luciel-api-base: " + apiBase + "\n" +
        "data-luciel-embed-key (last 4): ..." + key.slice(-4) + "\n" +
        "page-loaded-at: " + new Date().toISOString();
    });
  </script>

  <!-- The widget itself. The src below is the production CDN stable alias. -->
  <script src="https://d1t84i96t71fsi.cloudfront.net/widget.js"
          data-luciel-api-base="https://api.vantagemind.ai"
          data-luciel-embed-key="<<<RAW_EMBED_KEY_FROM_PHASE_2>>>"></script>
</body>
</html>
```

Serve it locally:

```powershell
cd C:\Users\aryan\staging-widget-test
python -m http.server 8000
# Cloudflared tunnel from phase 2.1 will already be forwarding to :8000.
```

---

## Phase 4 — The actual test (manual)

Open `https://<tunnel-host>` in a fresh browser profile (no extensions, no other Luciel sessions). DevTools Network panel open from the start.

| Check | What to confirm |
|---|---|
| `widget.js` loads | Network panel: 200 from `d1t84i96t71fsi.cloudfront.net`, content-type `application/javascript`, ≥27 KB. Check the `x-cache` header — `Hit from cloudfront` confirms warm cache, `Miss` the first time. |
| Widget UI renders | Floating chat surface appears in the corner. Greeting message matches what was minted in phase 2.3 ("This is the staging test widget…"). |
| First message round-trips | Send "test message 1." Network panel: a `POST` to `https://api.vantagemind.ai/v1/widget/chat` (confirm the exact path against the widget bundle source if it differs), 200 response, response body contains a model reply. Same exchange visible in the widget UI. |
| Origin enforcement holds | From DevTools console, run `fetch(...)` to the same widget endpoint with the embed key but `Origin: https://attacker.example`. Confirm rejection (403 or appropriate). |
| Rate limit fires | Send 31 messages in under a minute via the script-injected loop. Confirm the 31st gets a 429 with rate-limit headers. Then wait one minute and confirm normal operation resumes. |
| Audit row visible | Operator query against production DB: rows in `audit_log` for tenant `luciel-staging-widget-test` matching this session, with `actor_kind='embed_key'` and the last-4 of the minted key. |

---

## Phase 5 — Observation window (24–48h)

Leave the tunnel running. Periodically (every 4–6h):

- Hit the page from a fresh tab, send one message, confirm 200 + reply.
- Spot-check the production CloudWatch dashboard for the staging tenant: error rate, queue depth, model-call latency. Anything anomalous attributable to this tenant pauses the gate.
- Spot-check the audit-log tail for unexpected actor kinds or cross-tenant references.

A clean 24–48h means: zero unexplained errors attributable to the staging tenant or this embed key, every message exchange logged, no rate-limit anomalies, no origin-enforcement bypasses observed.

---

## Phase 6 — Close staging gate, prep REMAX Crossroads handoff

If clean:

1. **Do not deactivate the staging key yet** — keep it alive as the regression-test surface for any future change to the widget bundle, the chat endpoint, or the embed-key path. Pattern E.
2. **Stop the cloudflared tunnel** — the page goes 503 from the public side; the key still works if a future test re-establishes the same tunnel host. (If you used an ephemeral `*.trycloudflare.com` host, the host changes on next tunnel run and the key needs re-mint with the new origin. Named tunnels under a domain you own avoid this.)
3. **Open the REMAX Crossroads onboarding ticket.** Required inputs from them: their domain (the exact origin they will embed on), the page they intend to drop the script tag on, their preferred branding (accent color, display name, greeting message). Mint a separate embed key under a separate tenant for them. Never reuse the staging tenant or staging key.
4. **Hand off via secure channel** — 1Password share or signed email. Never paste the raw key into Slack, regular email, or any system that retains plaintext.
5. **Watch their first 48h** with the same dashboard rigor as the staging window. If clean, then and only then: flip the Step 30b row marker from 🔧 to ✅ in CANONICAL_RECAP §12.

If staging is not clean: the failure stays in the staging tenant, you debug there, REMAX Crossroads is not exposed to the issue. That is the entire point of sequencing this way.

---

## Pattern E references

- Embed key row: deactivate via `is_active=false`, never delete. Audit chain stays walkable.
- Tenant row for `luciel-staging-widget-test`: never reused for a paying customer. If retired, mark inactive and leave the row.
- CDN bundle: forward-only. The hashed alias `luciel-chat-widget.36a25740a60c.js` stays reachable forever for any consumer that pinned it.
