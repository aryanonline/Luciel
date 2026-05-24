"""
Application configuration.

All settings are loaded from environment variables or .env file.
This is the single source of truth for configuration across the app.
Add new provider keys or feature flags here as the product grows.
"""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # --- App ---
    app_name: str = "Luciel Backend"
    api_v1_prefix: str = "/api/v1"

    # --- Database ---
    database_url: str

    # --- LLM Providers ---
    # Luciel can route to any of these providers.
    # Add new provider keys here as you integrate more models.
    openai_api_key: str = ""
    anthropic_api_key: str = ""

    # --- Model Defaults ---
    # The default provider and model Luciel uses when the caller
    # does not specify a preference. Change these as you evaluate
    # which model works best for Luciel's persona and domain tasks.
    default_llm_provider: str = "anthropic"
    default_openai_model: str = "gpt-4o"
    default_anthropic_model: str = "claude-sonnet-4-20250514"

    # --- Async Worker (Step 27b) ---
    # Feature flag: when True, ChatService enqueues memory extraction to the
    # luciel-worker Celery service instead of running it inline. Read at
    # call-time (via settings.memory_extraction_async) so env flips take
    # effect without process restart during worker rollout/rollback.
    memory_extraction_async: bool = False

    # Redis broker URL for Celery. Prod ECS task-def injects from SSM
    # /luciel/production/REDIS_URL. Leave default for local dev.
    redis_url: str = "redis://localhost:6379/0"

    # AWS region for SQS queue-depth admin endpoint and any future
    # worker-side AWS calls. PIPEDA data residency: stays ca-central-1.
    aws_region: str = "ca-central-1"

    # --- Content-safety moderation gate (Step 30d Deliverable B) ---
    # Provider-agnostic moderation runs on every widget chat turn
    # BEFORE the LLM call. See app/policy/moderation.py for the
    # provider abstraction and ARCHITECTURE.md §3.3 step 6.5 for the
    # design statement.
    #
    # moderation_provider:
    #   'openai'  -- production default. Wrapped in FailClosed.
    #   'null'    -- development only; never blocks. Logs WARNING on
    #                every call so it cannot silently ship.
    #   'keyword' -- deterministic substring match against
    #                moderation_keyword_block_terms. Consumed by the
    #                widget-surface E2E CI gate (Step 30d Deliverable
    #                C) and by dev when an OpenAI key is unavailable.
    #                Not wrapped in FailClosed (no transport). Logs
    #                WARNING at construction when block-term list is
    #                empty so it cannot silently ship.
    # moderation_timeout_seconds: hard timeout on the provider call.
    #   3.0s is conservative for a single short text moderation;
    #   anything longer trips the fail-closed path.
    # moderation_fail_closed: when True (the production default), an
    #   unavailable provider is treated as a block. Set False only in
    #   dev to debug the gate; never in production.
    # moderation_keyword_block_terms: list of substrings that the
    #   'keyword' provider blocks on. Case-insensitive. Only consulted
    #   when moderation_provider='keyword'. Empty default so a deploy
    #   that flips to 'keyword' without also configuring terms emits
    #   the construction-time WARNING.
    moderation_provider: str = "openai"
    moderation_timeout_seconds: float = 3.0
    moderation_fail_closed: bool = True
    moderation_keyword_block_terms: list[str] = Field(default_factory=list)

    # --- Action classification gate (Step 30c) ---
    # Provider-agnostic action classifier runs on every tool
    # invocation BEFORE the tool's execute() method is called. See
    # app/policy/action_classification.py for the provider
    # abstraction and ARCHITECTURE.md §3.3 step 8 for the design
    # statement.
    #
    # action_classifier:
    #   'static' -- production default. Reads `declared_tier` from
    #               each tool class; wrapped in
    #               FailClosedActionClassifier so an undeclared
    #               tier routes to APPROVAL_REQUIRED rather than
    #               silently executing.
    #   'null'   -- development only; treats every invocation as
    #               ROUTINE. Logs WARNING on every call so it
    #               cannot silently ship.
    # action_classifier_fail_closed: when True (the production
    #   default), a classifier exception or an undeclared tier is
    #   treated as APPROVAL_REQUIRED. Set False only in dev to
    #   debug the gate; never in production.
    action_classifier: str = "static"
    action_classifier_fail_closed: bool = True

    # --- E2E-only stub LLM provider (Step 30d Deliverable C harness) ---
    # When True, ModelRouter registers a deterministic StubLLMClient
    # alongside any real provider. The stub yields fixed tokens and
    # makes no network call, which is exactly what the widget-e2e
    # workflow needs to assert the happy-path SSE three-frame contract
    # without a billable OpenAI/Anthropic call on every dispatch.
    #
    # MUST be False in production. StubLLMClient.__init__ emits a
    # WARNING on construction so a misconfigured deploy is observable
    # in the application log stream the first time the module is
    # imported. Parallel discipline to NullModerationProvider /
    # empty-list KeywordModerationProvider in app/policy/moderation.py.
    enable_stub_llm_provider: bool = False

    # --- Retention purge batching (Step 28 Phase 2 Commit 8) ---
    # Retention purges run as a sequence of bounded DELETE/UPDATE
    # statements rather than one unbounded statement. Without
    # batching, a year-old tenant with millions of `messages` rows
    # would issue a single DELETE that holds row locks across the
    # whole tenant's history, fills the WAL, blocks autovacuum, and
    # lags read replicas — a real RDS outage class.
    #
    # batch_size: rows per chunk. 1000 is conservative; with the
    # supporting btree(id) on every retention table this finishes a
    # batch in well under 100ms on a warm cache.
    # sleep_seconds: pause between batches. Even 50ms is enough to
    # let autovacuum and replication catch up under sustained load.
    # FOR UPDATE SKIP LOCKED on the inner SELECT keeps the purge
    # safe to run concurrently with chat traffic — locked rows are
    # picked up on the next batch instead of blocking the writer.
    retention_batch_size: int = 1000
    retention_batch_sleep_seconds: float = 0.05
    # Hard cap on total batches per _enforce_single call. Defense in
    # depth: if a category somehow grows faster than we can drain
    # (shouldn't happen post-anonymize-then-delete TTLs, but if it
    # ever does we want a bounded run rather than infinite loop).
    # 10_000 batches × 1000 rows = 10M rows per call — well above
    # any realistic tenant's daily expiry.
    retention_max_batches_per_run: int = 10_000

    # --- Stripe billing (Arc 6 V2 SKU surface) ---
    #
    # Self-serve subscription billing for the Pro tier and ops-driven
    # billing for the Enterprise tier. The Luciel backend is the single
    # source of truth for admin entitlement; Stripe is purely the payment
    # + recurring-billing surface. All money flows through Stripe-hosted
    # Checkout (lowest PCI scope, SAQ-A) and the Stripe Customer Portal
    # (cancel + payment-method update). The backend never touches a PAN.
    #
    # Tier topology (CANONICAL §11.7 / §14, revised Arc 7 Commit 1 —
    # Enterprise is now FLAT-recurring symmetric with Pro; hybrid /
    # metered-overage shape RETIRED by doctrine change 2026-05-24,
    # closes D-enterprise-metering-not-implemented-2026-05-22 as
    # retired-not-shipped; rate-limit ceilings (api_rate_limit_rpm,
    # Arc 7 Commit 4 tier-aware middleware) are the entitlement gates
    # that separate the tiers, per partner direction: "Since we have
    # abuse limits for each tier I don't think we need to include the
    # metering option for enterprise." The earlier ``leads_per_month_cap``
    # field was retired entirely at Arc 7 Commit 5 (2026-05-24) for the
    # same reason -- rate is the abuse boundary, and a monthly count
    # cap on a flat-recurring customer punishes success without
    # protecting any surface rate-limiting does not already cover.):
    #   Free        — $0 CAD, CAPTCHA-gated signup, no Stripe row at all.
    #   Pro         — flat-rate self-serve. $349 CAD/mo or $2,990 CAD/yr
    #                 (~28% annual discount). Stripe Checkout via
    #                 ``stripe_price_pro_monthly`` / ``stripe_price_pro_annual``.
    #                 First-time buyers additionally get the intro fee
    #                 (``stripe_price_intro_fee``) appended as a second
    #                 line item — same one-time pattern as V1.
    #   Enterprise  — flat-rate self-serve, monthly + annual cadences
    #                 symmetric with Pro. $2,800 CAD/mo via
    #                 ``stripe_price_enterprise_monthly`` or $24,000 CAD/yr
    #                 via ``stripe_price_enterprise_annual`` (28.6% annual
    #                 discount, matches Pro's ratio). The legacy
    #                 ``stripe_price_enterprise_floor_annual`` setting is
    #                 RETIRED at Arc 7 Commit 1 — the "floor" framing
    #                 belonged to the retired hybrid-billing shape; under
    #                 flat-recurring there is no floor distinction, just
    #                 the annual cadence Price.
    #
    # stripe_secret_key:      live or test secret key (sk_...).
    # stripe_publishable_key: pk_... -- not used server-side except as a
    #                         feature flag mirror; the marketing site reads
    #                         its own VITE_STRIPE_PUBLISHABLE_KEY.
    # stripe_webhook_secret:  whsec_... -- MUST be set for webhook signature
    #                         verification. Without it the /billing/webhook
    #                         route fails closed (501) at request time.
    # billing_success_url:    where Stripe redirects post-checkout. Carries
    #                         {CHECKOUT_SESSION_ID} for the onboarding
    #                         claim flow.
    # billing_cancel_url:     where Stripe redirects on "back" from checkout.
    # billing_trial_days:     trial length in days for new Pro subscriptions.
    #                         0 disables the trial.
    #
    # All Stripe fields default to empty strings so the backend boots in
    # environments without billing configured (dev, CI, Enterprise-only
    # deployments that don't touch self-serve). The billing routes raise
    # 501 Not Implemented when the corresponding field is empty, never 500.
    # This is the boot-safe pattern (§3.2.9): BillingService.resolve_price_id
    # (tier, cadence) -> price_id raises 501 if the requested pair's config
    # slot is empty rather than crashing the worker.
    #
    # V1 deprecation note (Arc 6 Commit 2, 2026-05-23): the six V1 Price
    # IDs (individual/team/company × monthly/annual) were archived in
    # Stripe Live on 2026-05-23 and their config slots removed in Arc 6
    # Commit 4 (this rewrite). V1 vocabulary (individual/team/company,
    # Tenant) is fully retired across the Stripe-touching surfaces by
    # Arc 6 Commit 6. See CANONICAL §17 Arc 6 entries.
    stripe_secret_key: str = ""
    stripe_publishable_key: str = ""
    stripe_webhook_secret: str = ""
    # --- Pro tier recurring Prices (flat-rate, self-serve via Checkout). ---
    stripe_price_pro_monthly: str = ""
    stripe_price_pro_annual: str = ""
    # --- Enterprise tier recurring Prices (flat-rate, self-serve via
    # Checkout — symmetric with Pro since Arc 7 Commit 1 retired the
    # hybrid/metered shape). $2,800 CAD/mo or $24,000 CAD/yr (28.6%
    # annual discount mirroring Pro's $349/$2,990 ratio). Empty defaults
    # keep boot safe: a missing slot causes the (enterprise, *) row in
    # ``PRICE_ID_KEY`` to resolve to BillingNotConfiguredError → 501. ---
    stripe_price_enterprise_monthly: str = ""
    stripe_price_enterprise_annual: str = ""
    # --- One-time $100 CAD intro fee Price (retained from V1, control). ---
    # Used by BillingService when the buyer's email is first-time-ever
    # (see ``BillingService.is_first_time_customer``). Decoupled from the
    # recurring Price IDs because it is a Stripe Price with
    # ``type=one_time`` rather than ``recurring`` — the same Price ID is
    # appended as a SECOND line_item alongside whichever (tier, cadence)
    # recurring Price the buyer is signing up for. Empty default keeps
    # boot safe: a missing slot causes /billing/checkout to 501 for
    # first-time buyers only; repeat customers (who skip the intro fee)
    # continue to work even if this slot is unconfigured. Unchanged across
    # the V1 -> V2 transition; the Stripe Price ID itself is retained.
    stripe_price_intro_fee: str = ""
    # Arc 2 (2026-05-20) -- D-marketing-site-url-luciel-ai-stale-default-2026-05-14
    # belt-and-suspenders sub-finding: production overrides these via the
    # `BILLING_SUCCESS_URL` / `BILLING_CANCEL_URL` env entries injected by
    # task-def rev49 (verified 2026-05-20 against the live :76 backend),
    # but the source-level defaults were still pointing at the dead apex
    # `luciel.ai` brand. If a future env-injection regression ever drops
    # the override, the fallback now resolves to the live marketing host
    # served via Amplify (`www.vantagemind.ai`, d1xf2f9605mosw) so a paid
    # buyer would land on a real page rather than a dead domain.
    billing_success_url: str = "https://www.vantagemind.ai/account/billing?status=success"
    billing_cancel_url: str = "https://www.vantagemind.ai/pricing?status=cancelled"
    # billing_trial_days: legacy Step 30a single-value default. Step 30a.2
    # superseded the free-trial model entirely with a uniform paid intro
    # (INTRO_TRIAL_DAYS=90 + $100 intro fee, see app/services/billing_service.py).
    # ``resolve_trial_days`` is now a no-op shim that always returns 0.
    # This setting is preserved for back-compat with any external scripts
    # that read it, but the application code path no longer consults it.
    billing_trial_days: int = 0

    # --- Magic-link email auth (Step 30a) ---
    #
    # Post-checkout the buyer needs to land in their Account/billing area.
    # We don't ship a password store at v1 (Step 32 owns that); instead we
    # mint a signed, single-use JWT that the marketing site exchanges for
    # a 30-day cookie session.
    #
    # magic_link_secret:      HS256 signing secret. MUST be set in prod;
    #                         empty value makes the /billing/login route
    #                         fail closed (501) at request time.
    # magic_link_ttl_hours:   how long the one-shot magic link is valid.
    #                         24h is the SaaS norm; long enough to survive
    #                         a slow email delivery, short enough that a
    #                         leaked link from a stale inbox is bounded.
    # session_cookie_ttl_days: lifespan of the cookie session after the
    #                         magic link is exchanged. 30d matches Stripe
    #                         Customer Portal session norms.
    # session_cookie_name:    name of the cookie; consistent value across
    #                         deploys keeps the marketing-site fetch path
    #                         stable.
    # session_cookie_secure:  True in prod (HTTPS-only). False allowed in
    #                         dev so the cookie survives localhost.
    # session_cookie_domain:  set explicitly so the cookie is visible to
    #                         both vantagemind.ai (marketing) and the api
    #                         subdomain. Empty means "host-only" which is
    #                         the dev default.
    magic_link_secret: str = ""
    magic_link_ttl_hours: int = 24
    session_cookie_ttl_days: int = 30
    session_cookie_name: str = "luciel_session"
    session_cookie_secure: bool = True
    session_cookie_domain: str = ""

    # --- Arc 3 Work-Unit B.2 -- JWT signing-key `kid` rolling window ---
    #
    # The single-secret `magic_link_secret` field above does NOT support
    # in-place rotation: overwriting it atomically invalidates every
    # signed-in customer's 30-day session cookie plus every in-flight
    # 24h set-password / reset-password / magic-link token. We added a
    # `kid`-header-based two-key rolling-window scheme so the operator
    # can rotate the signing key without forcing every customer to
    # re-login. Design memo: arc3-out/B2-kid-rolling-design.md.
    #
    # Contract:
    #
    # jwt_signing_keys_json: JSON map of {kid: secret}. In prod, populated
    #                       from SSM under /luciel/jwt-signing-keys (a
    #                       SecureString JSON blob). Exactly two entries
    #                       during a rotation; one entry steady-state.
    #                       Example: {"v2026-05-21": "<primary>",
    #                                 "v2025-08-12": "<grace>"}
    #                       When empty AND magic_link_secret is set, the
    #                       service falls back to a single "legacy"-kid
    #                       entry derived from magic_link_secret. This is
    #                       the boot-time shim that lets the code change
    #                       ship BEFORE the SSM blob lands -- zero deploy-
    #                       ordering hazards.
    # jwt_active_kid:       the kid that the minter should use for newly-
    #                       issued tokens. Must be a key in
    #                       jwt_signing_keys_json. Empty falls through to
    #                       "legacy".
    # jwt_grace_kid:        advisory only -- the decoder accepts any kid
    #                       present in jwt_signing_keys_json. Recorded
    #                       here so the rotation runbook can assert "we
    #                       are mid-rotation" vs. "we are steady-state"
    #                       at a glance.
    #
    # The `magic_link_secret` field stays through Step 32a as the boot-
    # time shim; removed when Step 32a self-serve identity ships its own
    # auth module rewrite.
    jwt_signing_keys_json: str = ""
    jwt_active_kid: str = ""
    jwt_grace_kid: str = ""

    # --- Outbound transactional email (Step 30a / 30a.2) ---
    #
    # As of Step 30a.2 Phase D, the magic-link email is delivered through
    # Amazon SES v2 in the ca-central-1 region. The task's IAM role
    # (luciel-ecs-web-role) carries the LucielSESSendEmail inline policy
    # scoped to the verified vantagemind.ai SES identity, so no
    # credentials are read from this config. The transport is selected
    # at runtime by app/services/email_service.py via the
    # LUCIEL_EMAIL_TRANSPORT env var ("ses" in prod, "log" in local dev).
    #
    # from_email:           the From address on magic-link emails. Must
    #                       be an address on the verified SES identity
    #                       (vantagemind.ai) in production.
    # marketing_site_url:   used to build login-link URLs that go through
    #                       the marketing site (not the backend), so the
    #                       click lands on the React app where the cookie
    #                       can be set on the apex domain.
    from_email: str = "noreply@vantagemind.ai"
    marketing_site_url: str = "https://www.vantagemind.ai"

    # --- Arc 8 Work-Unit 6 -- SES feedback / suppression / deliverability ---
    #
    # Closes D-ses-feedback-loop-not-wired-2026-05-22 and
    # D-ses-reply-to-monitored-inbox-not-confirmed-2026-05-22.
    #
    # ses_configuration_set_name:
    #     The name of the SES configuration set that the backend attaches
    #     to every outbound send_email call. The configuration set has an
    #     event destination that routes Bounce / Complaint / Reject /
    #     RenderingFailure notifications to the SNS topic
    #     ``luciel-ses-events`` in ca-central-1, which HTTPS-subscribes
    #     to ``POST /api/v1/ses-events`` on our backend.
    #
    #     Without the ConfigurationSetName parameter on send_email, SES
    #     does NOT emit feedback events to the configuration set's
    #     destination -- the configuration set exists but is dormant.
    #     Wiring this slot is the load-bearing knob that activates the
    #     feedback loop.
    #
    #     Default ``luciel-default`` matches the name the WU-6 Phase B
    #     prod-touch ceremony creates in the SES console. If the
    #     configuration set does not yet exist in the SES account, the
    #     send_email call returns a ConfigurationSetDoesNotExistException
    #     and the existing MagicLinkError / WelcomeEmailError /
    #     RefundEmailError handlers surface the failure -- so a missing
    #     configuration set is loud, not silent. Operators landing
    #     WU-6 must create the configuration set BEFORE rolling out the
    #     code change.
    #
    # ses_reply_to_address:
    #     The address SES populates into the Reply-To header. Today's
    #     transactional sends use From = ``noreply@vantagemind.ai`` which
    #     drops replies on the floor (the noreply mailbox is not
    #     monitored). Reply-To = ``support@vantagemind.ai`` routes any
    #     buyer reply ("I never set my password, can you help?",
    #     "please refund my charge") into the monitored support inbox
    #     where a human can act on it. This is a deliverability and
    #     trust signal AWS evaluates during the sandbox-exit review.
    #
    #     Closes D-ses-reply-to-monitored-inbox-not-confirmed-2026-05-22.
    #     The literal address must also be a verified SES identity (or
    #     part of the verified domain) -- ``vantagemind.ai`` is verified
    #     at the domain level, so any mailbox on that domain is
    #     send-as-eligible.
    ses_configuration_set_name: str = "luciel-default"
    ses_reply_to_address: str = "support@vantagemind.ai"

    # Arc 8 WU-6 Phase C -- SES feedback / suppression sink trust gate.
    #
    # ses_sns_topic_arn:
    #   The full ARN of the SNS topic that the SES configuration set
    #   ``luciel-default`` publishes feedback events to. The route
    #   ``POST /api/v1/ses-events`` (app/api/v1/ses_events.py) verifies
    #   incoming SNS messages carry this exact TopicArn as one half of
    #   the two-check trust gate (the other half is the SigningCertURL
    #   host check against *.amazonaws.com).
    #
    #   Empty-string default means "do not enforce TopicArn match" --
    #   used in tests and during the deploy bring-up window before the
    #   SNS topic is subscribed to the route. Production sets this to
    #   the real ARN via SSM-injected env var.
    #
    #   Today's production value (Phase B-landed AWS infrastructure):
    #       arn:aws:sns:ca-central-1:729005488042:luciel-ses-events
    ses_sns_topic_arn: str = ""

    # --- CORS (Step 30a.2-pilot Commit 3d) ---
    #
    # D-cors-middleware-missing-on-checkout-preflight-2026-05-15:
    #   The first cross-origin POST in the app (POST /api/v1/billing/checkout
    #   with Content-Type: application/json from https://www.vantagemind.ai)
    #   triggered a CORS preflight that returned 405 Method Not Allowed because
    #   no CORSMiddleware was installed in app/main.py. Every prior cross-origin
    #   path was either simple-request shaped (no preflight) or same-origin, so
    #   this defect was latent until the Step 30a.2-pilot live smoke. Commit 3d
    #   mounts fastapi.middleware.cors.CORSMiddleware in app/main.py with this
    #   allowlist as its origin set.
    #
    # cors_allowed_origins:
    #   The exact list of Origin: header values that the backend honors
    #   pre-flight checks for. Both apex and www are included because the
    #   apex domain still serves an Amplify redirect-only host that may
    #   forward to www but a directly-issued fetch from a script running on
    #   the apex would otherwise fail the preflight. Add staging / preview
    #   origins here when those environments come online.
    #
    #   Defaults are hardcoded inside the codebase (not in SSM) so a deploy
    #   that loses its env-var injection cannot regress the production CORS
    #   allowlist. Override via env var CORS_ALLOWED_ORIGINS or future SSM
    #   when we need to widen the list without a code change.
    cors_allowed_origins: list[str] = Field(
        default_factory=lambda: [
            "https://www.vantagemind.ai",
            "https://vantagemind.ai",
        ]
    )

    # --- Arc 8 Work-Unit 5 -- hCaptcha for Free-tier self-serve signup ---
    #
    # D-free-tier-captcha-missing-2026-05-22 resolution. The Free tier
    # (Arc 4 Deliverable #4 -- Free/Pro/Enterprise shape) ships a
    # public unauthenticated signup endpoint at
    # ``POST /api/v1/billing/signup-free``. Without a bot gate that
    # surface is a free SES-quota drain and a free database-row drain,
    # so we require an hCaptcha token on every Free-tier signup.
    #
    # Provider choice: hCaptcha (privacy-preserving, GDPR-friendly,
    # free up to 1M requests/month). Verify is a single HTTP POST to
    # https://api.hcaptcha.com/siteverify with form-encoded
    # ``{secret, response, [remoteip]}``; no SDK required (httpx is
    # already in the dependency set).
    #
    # hcaptcha_secret_key: server-side secret read from SSM under
    #                      /luciel/production/HCAPTCHA_SECRET_KEY at
    #                      task launch (env-var injection). Empty
    #                      default keeps boot safe in dev / CI; the
    #                      ``/billing/signup-free`` route fails 501
    #                      (not 500) when the slot is empty, mirroring
    #                      the Stripe-not-configured boot-safe pattern
    #                      (§3.2.9).
    # hcaptcha_verify_url: separated from the secret so a future
    #                      pivot to Cloudflare Turnstile or a
    #                      hCaptcha enterprise endpoint does not
    #                      require touching the service code. Default
    #                      is the public hCaptcha verify URL.
    # hcaptcha_site_key:   the front-end widget key (pk_-equivalent).
    #                      Not consumed server-side; reserved here so
    #                      the marketing site can read it through the
    #                      same /api/v1/billing/public-config surface
    #                      that exposes the Stripe publishable key.
    #                      Optional, empty default.
    hcaptcha_secret_key: str = ""
    hcaptcha_verify_url: str = "https://api.hcaptcha.com/siteverify"
    hcaptcha_site_key: str = ""

    # --- Arc 8 Commit 2: post-checkout email deliverability gate ---
    # When True, ``TierProvisioningService.premint_for_tier`` runs the
    # buyer's email through ``email_validator.validate_email`` with
    # ``check_deliverability=True`` (DNS MX lookup). On failure the
    # gate does NOT abort pre-mint -- Stripe has already collected
    # payment and the admin row is committed; instead it records a
    # structured warning that the welcome-email send path consults
    # to decide whether to skip the send (and surface a Support
    # touchpoint in CloudWatch).
    #
    # Set to False to disable the lookup entirely (e.g. when running
    # in a sandbox / CI environment with no outbound DNS, or when a
    # third-party MX-check provider is being rolled in via a
    # different code path). Default True in prod; the test suite
    # patches this False at the unit level and exercises a true-path
    # injection test against a controlled synthetic typo.
    #
    # Synthetic ``*.luciel.local`` emails (identity-resolver mints,
    # Option-B onboarding) are ALWAYS bypassed regardless of this
    # flag -- they are real internal identifiers, not external
    # deliverable addresses. See
    # ``app.services.tier_provisioning_service._SYNTHETIC_EMAIL_DOMAIN_SUFFIX``.
    #
    # Closes drift D-stripe-checkout-no-email-validation-2026-05-18.
    email_deliverability_check_enabled: bool = True
    # Soft network timeout for the MX lookup. The email-validator
    # library uses the system resolver under the hood; this cap
    # exists so a slow/hostile DNS resolver cannot stall a webhook
    # handler past Stripe's 30s ACK budget. Two seconds is enough
    # for a healthy resolver and tight enough to fail-fast against
    # a degraded one (the gate then no-ops with a logged warning).
    email_deliverability_check_timeout_seconds: float = 2.0

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()