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

    # --- Stripe billing (Step 30a) ---
    #
    # Self-serve subscription billing for the Individual tier. The Luciel
    # backend is the single source of truth for tenant entitlement; Stripe
    # is purely the payment + recurring-billing surface. All money flows
    # through Stripe-hosted Checkout (lowest PCI scope, SAQ-A) and the
    # Stripe Customer Portal (cancel + payment-method update). The
    # backend never touches a PAN.
    #
    # stripe_secret_key:      live or test secret key (sk_...).
    # stripe_publishable_key: pk_... -- not used server-side except as a
    #                         feature flag mirror; the marketing site reads
    #                         its own VITE_STRIPE_PUBLISHABLE_KEY.
    # stripe_webhook_secret:  whsec_... -- MUST be set for webhook signature
    #                         verification. Without it the /billing/webhook
    #                         route fails closed (501) at request time.
    # stripe_price_individual: price_... for the Individual SKU. Single
    #                         price at v1. Annual / multi-tier prices land
    #                         in Step 30a.1 / 30a.2 as additional fields.
    # billing_success_url:    where Stripe redirects post-checkout. Carries
    #                         {CHECKOUT_SESSION_ID} for the onboarding
    #                         claim flow.
    # billing_cancel_url:     where Stripe redirects on "back" from checkout.
    # billing_trial_days:     trial length in days for new individual
    #                         subscriptions. 0 disables the trial.
    #
    # All Stripe fields default to empty strings so the backend boots in
    # environments without billing configured (dev, CI, tenants on Team /
    # Company tiers that don't touch self-serve). The billing routes raise
    # 501 Not Implemented when the corresponding field is empty, never 500.
    stripe_secret_key: str = ""
    stripe_publishable_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_price_individual: str = ""
    # --- Step 30a.1: five additional Stripe Price IDs (all optional). ---
    # The BillingService.resolve_price_id (tier, cadence) -> price_id
    # lookup raises 501 if the requested pair's config slot is empty.
    # This is the boot-safe pattern (§3.2.9): a backend without billing
    # configured still boots; the /billing/checkout route returns 501.
    stripe_price_individual_annual: str = ""
    stripe_price_team_monthly: str = ""
    stripe_price_team_annual: str = ""
    stripe_price_company_monthly: str = ""
    stripe_price_company_annual: str = ""
    billing_success_url: str = "https://luciel.ai/onboarding?session_id={CHECKOUT_SESSION_ID}"
    billing_cancel_url: str = "https://luciel.ai/pricing?cancelled=1"
    # billing_trial_days: legacy Step 30a single-value default. Step 30a.1
    # introduces a (tier, cadence) -> trial_days lookup in BillingService
    # (TRIAL_DAYS constant). This setting now only governs the v1
    # Individual-monthly fallback for backward compatibility; the new
    # tier-aware path overrides it when the (tier, cadence) pair is in
    # TRIAL_DAYS.
    billing_trial_days: int = 14

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
    #                         both luciel.ai (marketing) and the api
    #                         subdomain. Empty means "host-only" which is
    #                         the dev default.
    magic_link_secret: str = ""
    magic_link_ttl_hours: int = 24
    session_cookie_ttl_days: int = 30
    session_cookie_name: str = "luciel_session"
    session_cookie_secure: bool = True
    session_cookie_domain: str = ""

    # --- Outbound transactional email (Step 30a) ---
    #
    # At v1 we don't ship a full email service abstraction; the magic-link
    # email is logged + emitted via a single FROM address. A future Step
    # 32 commit can swap the sender for SES/Postmark/etc. behind the same
    # interface (see app/services/email_service.py).
    #
    # from_email:           the From address on magic-link emails.
    # marketing_site_url:   used to build login-link URLs that go through
    #                       the marketing site (not the backend), so the
    #                       click lands on the React app where the cookie
    #                       can be set on the apex domain.
    from_email: str = "no-reply@luciel.ai"
    marketing_site_url: str = "https://luciel.ai"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()