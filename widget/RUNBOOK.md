# Widget runbook (Step 30b)

Three modes for working with the chat widget locally.

## Mode 1 — Real backend smoke

Use this before the widget hits any customer site. Proves the full
real path: real Postgres, real api_keys row, real
`require_embed_key` gate, real SSE.

```bash
# 1. Build the widget bundle
cd widget
npm ci
npm run build

# 2. Apply the schema migration (commit b)
cd ..
alembic upgrade head    # head is now a7c1f4e92b85

# 3. Boot the backend
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

# 4. In another shell, mint an embed key against the running API.
#    Use whatever admin endpoint / script you already use to mint
#    keys (api_key_service.create_key) and pass:
#       key_kind          = 'embed'
#       permissions       = ['chat']
#       allowed_origins   = ['http://127.0.0.1:8001']  # serve demo from here
#       rate_limit_per_minute = 60
#       widget_config     = {accent_color, greeting_message, display_name}

# 5. Serve widget/ on a different port so it has a real Origin
#    that the embed key's allowlist matches.
cd widget
npx --yes http-server -p 8001 .

# 6. Open http://127.0.0.1:8001/demo.html
#    apiBase: http://127.0.0.1:8000
#    embedKey: paste the key you minted in step 4
#    Click Mount, click the launcher, send a message.
```

Failure modes you should expect to verify:

* Wrong origin → 403 with `code: origin_not_allowed`
* Admin key → 403 with `code: embed_key_required`
* Wrong permissions → 403 with `code: embed_permissions_mismatch`
* Burst past `rate_limit_per_minute` → 429

## Mode 2 — Mock backend smoke

Use this when you don't want to spin up Postgres + Redis. The mock
SKIPS every gate -- it exists only to prove the widget bundle
renders and streams correctly.

```bash
cd widget
npm run build
npm run mock-backend     # listens on 127.0.0.1:8765
```

In another shell:

```bash
cd widget
npx --yes http-server -p 8766 .
# Open http://127.0.0.1:8766/demo.html
# apiBase: http://127.0.0.1:8765
# embedKey: any non-empty string
```

## Mode 3 — End-to-end smoke (automated)

Drives Mode 2 via Playwright + Chromium so the whole path runs
without a human. Skips cleanly if Playwright is not installed
(this is intentional -- the script is on-demand, not a CI gate).

```bash
cd widget
npm install -D playwright       # one-time, only if not already present
npx playwright install chromium # one-time browser fetch
npm run test:e2e
```

The script asserts:

  - bundle imports without error
  - mount creates the Shadow DOM host
  - launcher click opens the panel
  - typing + Send sends a POST to the mock backend
  - streamed reply renders into the Shadow DOM
  - `done` frame clears the streaming class

## Production deployment surface (deferred)

The widget bundle ships to a CDN; the backend service redeploys
behind ALB with the new endpoint. Specifically:

  * S3 bucket + CloudFront distribution for `dist/luciel-chat-widget.js`
    (and its `.map`)
  * Backend service redeploy after `alembic upgrade head`
  * SSM parameter for the per-customer issuance script (the
    actual key-minting workflow that produces the embed key for
    Mode 1 step 4)

These are tracked in `docs/DRIFTS.md` §4 (PROD-PHASE-2B) and live
outside this branch's scope. The work in this branch (commits b, c,
d, e) is everything that ships to git; production rollout is the
next conversation.
