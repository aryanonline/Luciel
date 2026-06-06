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

    # --- Per-tier model-class configuration (Architecture §3.4.3) ---
    #
    # The tier matrix (Vision §7) sells "model selection: base/mid/top"
    # by tier. Each tier maps to a LOCKED model class (Anthropic primary
    # + OpenAI fallback). The specific version strings are operational
    # (config-driven so ops can retune without code change); what is
    # LOCKED per Decision #7/#8/#11 is the class mapping itself.
    #
    # Tier -> class -> concrete model:
    #   Free       -> small/fast  -> anthropic_model_free  / openai_model_free
    #   Pro        -> mid         -> anthropic_model_pro   / openai_model_pro
    #   Enterprise -> top         -> anthropic_model_ent   / openai_model_ent
    #
    # Per-tier fast models (intra-tier fast routing, Decision #9):
    #   When ALL of (no tools, <= 4K ctx tokens, low complexity) hold,
    #   the router uses the fast model variant instead of the tier primary.
    #   Fast model defaults to the Free-tier primary (Haiku-class) for all
    #   tiers since that is the intended cheap/fast seat. The fast model
    #   is NOT surfaced to admins (Decision #11).
    #
    # Anthropic primary models by tier:
    anthropic_model_free: str = "claude-haiku-4-20250514"
    anthropic_model_pro: str = "claude-sonnet-4-20250514"
    anthropic_model_ent: str = "claude-sonnet-4-20250514"
    # OpenAI fallback models by tier:
    openai_model_free: str = "gpt-4o-mini"
    openai_model_pro: str = "gpt-4o"
    openai_model_ent: str = "gpt-4o"
    # Intra-tier fast models (used when fast-route conditions hold).
    # One fast model shared across tiers (always Haiku-class); a tier
    # whose primary IS already the fast class simply reuses it.
    anthropic_model_fast: str = "claude-haiku-4-20250514"
    openai_model_fast: str = "gpt-4o-mini"
    # Complexity threshold for intra-tier fast routing (Decision #9).
    # The heuristic score must be BELOW this value to qualify for the
    # fast path. Higher value = more messages qualify. Operational.
    llm_fast_route_complexity_threshold: float = 10.0
    # Context token limit for the fast path (4 K tokens per Decision #9).
    llm_fast_route_context_token_limit: int = 4096

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
    # BEFORE the LLM call. See app/runtime/input_safety.py for the
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
    # empty-list KeywordModerationProvider in app/runtime/input_safety.py.
    enable_stub_llm_provider: bool = False

    # --- Hermetic stub embedding provider (Unit 10) ---
    # When True, app.knowledge.embedder.embed_texts returns
    # deterministic stub vectors (seeded from sha256 of each text)
    # instead of calling OpenAI. Mirrors enable_stub_llm_provider:
    # it exists so the two carried arc11 internal-retrieve live tests
    # can run hermetically with no network call. The embedder emits a
    # WARNING the first time the stub path runs so a production deploy
    # that flips this flag is observable in the log stream.
    #
    # MUST be False in production so real OpenAI embeddings are used.
    enable_stub_embedding_provider: bool = False

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
    #   Pro         — flat-rate self-serve. $149 CAD/mo or $1,432 CAD/yr
    #                 (~20% annual discount: 1,432 vs 149×12=1,788). Stripe Checkout via
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
    # Enterprise tier recurring Price slots removed in Unit 1
    # (Enterprise deferred -- Open Decision #8). The ratified model
    # ships Free + Pro only.
    # --- Arc 18 (§3.4.1b) conversation-overage metered Prices. ---
    # The per-instance overage add-on billed at cycle close. These are
    # METERED (usage-record) Prices, distinct from the flat-rate recurring
    # base Prices above — Arc 18 re-introduces metering as an ADD-ON axis
    # (Arc 7 Commit 1 retired metering only for the flat BASE subscription;
    # see ARC18_BACKEND_REPORT.md "supersedes" note). Founder provisions
    # these Prices in Stripe; the backend NEVER mints them. Resolved per
    # (tier, cadence) by ``entitlements.overage_price_config_key``:
    #   Pro monthly → $35.00 / 100 conversations (3500 cents)
    #   Pro annual  → $30.00 / 100 conversations (3000 cents)
    # Empty defaults keep boot safe: a missing slot makes the
    # usage-record report a no-op (the period still resets) and is
    # surfaced as a documented gap. (Enterprise per-contract overage
    # removed in Unit 1 -- Enterprise tier deferred.)
    stripe_price_overage_pro_monthly: str = ""
    stripe_price_overage_pro_annual: str = ""
    # The Stripe Billing Meter ``event_name`` the overage Prices read from.
    # stripe-python 15.x reports metered usage via the Billing Meter Events
    # API (stripe.billing.MeterEvent) keyed by this event_name + the
    # customer id, NOT the legacy per-subscription-item usage record API
    # (removed in SDK 8+). Founder provisions the Meter; empty default →
    # usage reporting is a no-op (period still resets). One meter serves
    # both Pro cadences; the (tier,cadence) Price gates the rate.
    stripe_meter_event_overage: str = ""
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

    # --- Arc 9 C2: In-app RLS connection-pool wrapper ---
    # Master feature flag for the Layer-3 tenant-context wiring that
    # issues ``SET LOCAL app.admin_id = '<uuid>'`` on every DB
    # connection at request entry. When True, the engine ``after_begin``
    # listener pushes the in-process admin_id (and instance_id) GUC to
    # PostgreSQL so the per-table RLS policies (Arc 9 C3..C9) actually
    # enforce tenant isolation. When False the ContextVar still tracks
    # the current admin_id (cheap, in-process only) but the listener is
    # a no-op and RLS has nothing to compare against.
    #
    # DEFAULT FLIPPED TO TRUE (ARC 15 — Vision §3.3 / §5 hard tenant
    # isolation). The full Arc 9 rollout has landed: every customer-data
    # table carries an ENABLE + FORCE ROW LEVEL SECURITY policy keyed on
    # ``current_setting('app.admin_id', true)`` (arc9_c3_*, arc9_c4_3*,
    # arc9_c5_*, arc9_c10_a_force_rls, arc9_c11_tenant_restrictive,
    # arc11_d1/d2, arc12b_custom_roles_rls), the runtime app role
    # (arc9_c10_b_luciel_app_role) is non-superuser so FORCE RLS applies,
    # and the SECURITY DEFINER bootstrap helpers (arc9_c20..c22) resolve
    # tenancy without bypassing the fence. The live-Postgres integration
    # test (tests/db/test_c9_5_live_rls_integration.py) proves the GUC +
    # policies deny cross-tenant reads, deny unset-GUC reads, and deny
    # mismatched-tenant INSERT/UPDATE with the flag on. Isolation is now
    # ON unless an operator explicitly sets RLS_TENANT_CONTEXT_ENABLED=0
    # (e.g. a forensic break-glass session against a non-RLS replica).
    #
    # Historical rollout order (see ARC9_RUNBOOK):
    #   1. C2 lands flag=False everywhere -- code path exists but
    #      no behaviour change. Tests verify ContextVar plumbing.
    #   2. C3 lands the first per-table RLS policy (admin_audit_logs);
    #      flag flipped per-environment as policies landed per-table.
    #   3. C3 lands remaining tables incrementally (ENABLE RLS each).
    #   4. C9 lands FORCE ROW LEVEL SECURITY + the luciel_app role and
    #      tags arc-9-tenant-isolation-complete.
    #   5. Arc 15 flips this source-level default True so a deploy that
    #      loses its env-var injection still fails closed (isolation on)
    #      rather than open.
    #
    # Closes the structural gap C1 surfaced: customer-data tables filter
    # at L1, but a single forgotten WHERE clause in a future repository
    # method would leak cross-admin rows. L3 + L2 together make that
    # impossible -- and with the default True they are active by default.
    rls_tenant_context_enabled: bool = True

    # Arc 11 Step 8 — knowledge retrieval feature flag.
    #
    # Master kill-switch for the ``LucielOrchestrator.run`` Retrieve
    # step. When ``True``, the orchestrator calls
    # ``KnowledgeRetriever.retrieve_with_sources(...)`` and threads
    # the resulting source PKs through to ``TraceService.record_trace``
    # (which writes them to ``traces.source_ids_used`` — the
    # ``/affected-questions`` endpoint reads from there).
    #
    # Defaults closed: Arc 11 ships the WIRING, not the live retriever.
    # Arc 14 owns the full agentic loop (PLAN/ACT/REFLECT, escalation
    # judgment, tool dispatch) and is the right place to make
    # retrieval the always-on hot path. Until then, retrieval is
    # opt-in.
    #
    # Granularity: a single global bool. Per-``(admin_id, instance_id)``
    # rollout was considered (see ARC11_PLAN.md §4) and deferred —
    # Arc 14 may need it when tenant-by-tenant flips matter, but for
    # v1 the surface is "on" or "off."
    knowledge_retrieval_enabled: bool = False

    # Arc 9 C6.3 -- BYPASSRLS ops connection (Wall 1 escape hatch).
    #
    # The luciel_ops Postgres role created in Arc 9 C6.1 carries
    # BYPASSRLS and a narrow grant matrix (SELECT-only on
    # admin_audit_logs; SELECT + DELETE on the eight retention tables
    # listed in admin_service.delete_admin_cascade). It exists so
    # forensic queries and retention DELETEs can cross the tenant
    # fence cleanly without temporarily disabling RLS or running as
    # superuser. Application code that needs that capability calls
    # ``app.db.session.get_ops_db_session()``, which binds against
    # this URL.
    #
    # When ``luciel_ops_db_url`` is None (the default) the helper
    # raises ``RuntimeError`` -- fail closed. Production sets this
    # via SSM parameter ``/luciel/production/ops_database_url``
    # (minted by ``scripts/mint_ops_db_password_ssm.py``) which the
    # ECS task definition injects as the LUCIEL_OPS_DB_URL env var.
    # Local dev / CI leave it unset so the ops session is unreachable
    # outside production.
    #
    # SECURITY: the ops session MUST NOT emit ``app.admin_id`` /
    # ``app.instance_id`` GUCs -- ops queries have no tenant scope and
    # leaking a stale GUC onto a BYPASSRLS connection would be a
    # cross-tenant footgun. ``app/db/session.py`` enforces this by
    # attaching the after_begin tenant-context listener to
    # SessionLocal only; the separate OpsSessionLocal is naturally
    # GUC-free.
    luciel_ops_db_url: str | None = None

    # Arc 9 C6.3 -- forward-only audit-log immutability flag.
    #
    # The C6.2 migration installs two RESTRICTIVE policies on
    # admin_audit_logs (admin_audit_logs_no_update,
    # admin_audit_logs_no_delete) that allow UPDATE/DELETE only when
    # ``current_user = 'luciel_ops'``. The migration creates the
    # policies unconditionally, but the policies' effect is
    # behaviourally identical to today's posture until
    # ``ENABLE ROW LEVEL SECURITY`` is also applied (the same
    # dark-deploy pattern Arc 9 C2/C3 uses for the tenant fence).
    #
    # This flag is the master switch for application-side guards
    # (assertions in tests, future runtime checks) that should only
    # fire once immutability is fully active in prod. It stays False
    # until C9 envelope close flips it True at the same deploy that
    # tags arc-9-tenant-isolation-complete.
    audit_log_immutability_enabled: bool = False

    # -----------------------------------------------------------------
    # Arc 10 -- Lifecycle subsystem settings.
    # -----------------------------------------------------------------
    # audit_archiver_db_url:
    #   Connection URL for the luciel_audit_archiver Postgres role
    #   created by the Arc 10 migration. This role has SELECT + UPDATE
    #   on admin_audit_log only and BYPASSRLS. Used exclusively by
    #   AuditRetentionService. The audit-retention beat task runs as
    #   a no-op when this is unset, so local dev / CI do not
    #   accidentally archive audit rows.
    audit_archiver_db_url: str | None = None

    # data_export_bucket:
    #   S3 bucket name for pre-closure export bundles (Arc 10
    #   DataExportService). Lifecycle policy on the bucket aborts
    #   incomplete multipart uploads after 24h so interrupted
    #   generations do not leak storage forever.
    data_export_bucket: str = "luciel-data-exports"

    # audit_cold_archive_bucket:
    #   S3 bucket name for tier-conditional audit cold archive.
    #   Per Vision 6.5 / 7: hot rows whose tier window has elapsed
    #   are moved here with the hash chain extended across the
    #   hot/cold boundary.
    audit_cold_archive_bucket: str = "luciel-audit-cold-archive"

    # -----------------------------------------------------------------
    # Arc 13 — channel adapters (email + SMS) provisioning + transport.
    # -----------------------------------------------------------------
    #
    # PLATFORM LIVE-SWITCH. Master gate separating real-Twilio /
    # real-provisioning from the fake/no-op path. When False (the
    # boot-safe default), NO real Twilio API call is ever made: the
    # PhoneNumberProvisioningService selects FakePhoneNumberProvider
    # and the SMS adapter's outbound send becomes a no-op receipt.
    # Production flips this True in lockstep with the SSM-injected
    # Twilio credentials below. Dev / CI / tests leave it False so a
    # mis-wired test can never bill Twilio.
    channels_live_provisioning_enabled: bool = False

    # Twilio credentials + messaging service. Sourced in prod from SSM
    # under /luciel/production/ (cross-checked against arc13-infra). All
    # default empty so the backend boots cleanly without Twilio
    # configured; the provisioning service refuses to make a live call
    # when channels_live_provisioning_enabled is True AND any required
    # credential is empty (fail-loud, never a half-configured live call).
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_messaging_service_sid: str = ""
    twilio_api_key_sid: str = ""
    twilio_api_key_secret: str = ""
    # Public base URL the Twilio inbound webhook is wired to point at.
    # Provisioning sets each purchased number's SMS webhook to
    # f"{twilio_webhook_base_url}/api/v1/twilio/sms". Empty default keeps
    # boot safe; live provisioning requires it to be set.
    twilio_webhook_base_url: str = ""

    # Inbound email (SES → SNS) channel settings. MAIL_INBOUND_DOMAIN is
    # the platform subdomain inbound addresses live under
    # (<instance-slug>@<admin-slug>.MAIL_INBOUND_DOMAIN). SES_INBOUND_BUCKET
    # is the S3 bucket SES delivers raw inbound MIME to. ses_inbound_topic_arn
    # is the SNS topic the inbound notifications publish to — verified by
    # the email adapter's signature gate (reuses the ses_events two-check
    # trust gate: TopicArn allowlist + SigningCertURL host check). Empty
    # defaults keep boot safe; the email adapter degrades to "do not
    # enforce TopicArn" when ses_inbound_topic_arn is empty (dev / CI),
    # exactly as ses_sns_topic_arn does for the feedback route.
    mail_inbound_domain: str = ""
    ses_inbound_bucket: str = ""
    ses_inbound_topic_arn: str = ""

    # -----------------------------------------------------------------
    # Arc 17 — Connections layer secret store + OAuth (DEPLOY-GATED)
    # -----------------------------------------------------------------
    #
    # connections_live_secrets_enabled is the master gate selecting the
    # real AWS Secrets Manager store vs the in-memory LocalFakeSecretStore.
    # When False (the boot-safe default) get_secret_store() returns the
    # fake — NO boto3 client is constructed and NO AWS call is ever made,
    # so dev / CI / tests can exercise the full connection code path
    # without AWS creds. Production flips this True in lockstep with the
    # IAM secretsmanager:* grant. See app/integrations/secrets/.
    connections_live_secrets_enabled: bool = False

    # record_source_live_enabled is the master gate for reading a LIVE
    # record source whose store_ref points at remote object storage
    # (an s3:// URI). When False (the boot-safe default) the resolver
    # NEVER constructs a boto3 client and an s3:// store_ref returns an
    # HONEST deploy-gated failure rather than a fake success — exactly
    # the convention connections_live_secrets_enabled uses for the secret
    # store. A local/file:// store_ref is ALWAYS readable regardless of
    # this flag (no AWS dependency). Production flips this True in lockstep
    # with the IAM s3:GetObject grant on the record-source bucket prefix.
    # See app/integrations/record_source/.
    record_source_live_enabled: bool = False

    # Google OAuth client credentials for the calendar connector (the
    # Arc 17 reference OAuth provider). Sourced in prod from SSM under
    # /luciel/production/. Empty defaults keep boot safe AND keep the
    # connector HONEST: when either is empty the OAuth provider reports
    # itself unconfigured and the connector round-trips as an honest
    # 'unconfigured' + arc17_pending marker — it NEVER fakes 'connected'.
    # The flip to 'connected' is DEPLOY-GATED on these being populated.
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    # Redirect URI the OAuth consent screen returns the auth code to.
    google_oauth_redirect_uri: str = ""

    # Server-side HMAC secret that signs the opaque OAuth ``state`` the
    # initiate endpoint mints and the callback endpoint verifies. The
    # callback is UNAUTHENTICATED in the session-cookie sense (Google
    # redirects the browser to it with no cookie), so it authorizes
    # ENTIRELY off this verified state — a tampered/forged/expired state
    # is rejected before any tenant resolution. Prod injects a strong
    # random value from SSM (/luciel/production/OAUTH_STATE_SIGNING_SECRET);
    # the dev default below is a clearly-marked placeholder that keeps the
    # backend booting locally but is NOT safe for production. The HMAC is
    # over (admin_id, instance_id, connection_type, nonce, issued-at), so
    # rotating this secret simply invalidates any in-flight consent flows.
    oauth_state_signing_secret: str = "dev-insecure-oauth-state-secret-change-me"
    # Seconds an OAuth ``state`` stays valid between initiate and callback.
    # The consent screen round-trip is interactive (human clicks Approve),
    # so a few minutes is ample; a tight TTL bounds replay of a leaked
    # state. 600s = 10 minutes.
    oauth_state_ttl_seconds: int = 600
    # Frontend route the callback redirects the browser to after a
    # successful (or failed) exchange. The connection_type + an outcome
    # flag are appended as query params so the SPA can refetch the
    # connections list and toast the result. Prod overrides via env /
    # SSM; the default points at the live marketing/admin host.
    oauth_callback_success_url: str = (
        "https://www.vantagemind.ai/admin/connections"
    )

    # -----------------------------------------------------------------
    # Arc 17 — deploy-gated LIVE connectors (email_sender / sms_sender /
    # native HubSpot + Salesforce CRM OAuth).
    # -----------------------------------------------------------------
    #
    # MASTER LIVE-SWITCH for the deploy-gated connectors (the send/push
    # code paths are BUILT; this switch gates whether they reach a live
    # provider). Mirrors channels_live_provisioning_enabled exactly: when
    # False (the boot-safe default) NO real provider call is ever made —
    # the email_sender / sms_sender send tools return an HONEST no-op
    # receipt and the CRM push tool round-trips an honest unconfigured, so
    # dev / CI / tests can
    # exercise the full code path without billing or hitting any provider.
    # Production flips this True IN LOCKSTEP with the per-connector
    # credentials below landing in SSM. The OAuth client-creds gate
    # (is_configured) is the SECOND, independent honesty gate: even with
    # this switch on, an absent credential keeps the connector honestly
    # unconfigured and short-circuits BEFORE any network call.
    connectors_live_enabled: bool = False

    # --- email_sender (outbound sender-identity) -------------------------
    # The outbound send rides the existing SES transport
    # (LUCIEL_EMAIL_TRANSPORT=ses + the Arc 13 SES IAM grant); these fields
    # carry the VERIFIED sender identity the live send uses as the From
    # address. Sourced in prod from SSM under
    #   /luciel/<env>/connectors/email_sender/FROM_ADDRESS  -> email_sender_from_address
    #   /luciel/<env>/connectors/email_sender/FROM_NAME     -> email_sender_from_name
    # Empty defaults keep boot safe AND keep the connector honest: when
    # email_sender_from_address is empty the send tool reports itself
    # unconfigured and performs NO send (no SES call), exactly the
    # is_configured() discipline the OAuth providers use.
    email_sender_from_address: str = ""
    email_sender_from_name: str = ""

    # --- HubSpot CRM OAuth -----------------------------------------------
    # Native HubSpot OAuth 2.0 app credentials. Sourced in prod from SSM:
    #   /luciel/<env>/connectors/hubspot/CLIENT_ID     -> hubspot_oauth_client_id
    #   /luciel/<env>/connectors/hubspot/CLIENT_SECRET -> hubspot_oauth_client_secret
    #   /luciel/<env>/connectors/hubspot/REDIRECT_URI  -> hubspot_oauth_redirect_uri
    # Empty defaults keep the connector HONEST: when either client id or
    # secret is empty the HubSpot OAuth provider reports is_configured()
    # False and the crm connector round-trips unconfigured + arc17_pending.
    # The flip to a live token exchange is DEPLOY-GATED on these.
    hubspot_oauth_client_id: str = ""
    hubspot_oauth_client_secret: str = ""
    hubspot_oauth_redirect_uri: str = ""

    # --- Salesforce CRM OAuth --------------------------------------------
    # Native Salesforce OAuth 2.0 (web-server flow) connected-app
    # credentials. Sourced in prod from SSM:
    #   /luciel/<env>/connectors/salesforce/CLIENT_ID     -> salesforce_oauth_client_id
    #   /luciel/<env>/connectors/salesforce/CLIENT_SECRET -> salesforce_oauth_client_secret
    #   /luciel/<env>/connectors/salesforce/REDIRECT_URI  -> salesforce_oauth_redirect_uri
    # salesforce_oauth_login_base is the auth host: the production login
    # domain by default; an org on a sandbox sets it to
    # https://test.salesforce.com (or its My Domain) via
    #   /luciel/<env>/connectors/salesforce/LOGIN_BASE -> salesforce_oauth_login_base
    # Empty client id/secret keep the connector HONEST (is_configured()
    # False → unconfigured + arc17_pending); the live token exchange is
    # DEPLOY-GATED on the client id + secret being populated.
    salesforce_oauth_client_id: str = ""
    salesforce_oauth_client_secret: str = ""
    salesforce_oauth_redirect_uri: str = ""
    salesforce_oauth_login_base: str = "https://login.salesforce.com"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()