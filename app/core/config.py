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

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()