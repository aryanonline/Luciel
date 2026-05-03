"""
Application configuration.

All settings are loaded from environment variables or .env file.
This is the single source of truth for configuration across the app.
Add new provider keys or feature flags here as the product grows.
"""

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