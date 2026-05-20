import json
from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings


# Step 30a.7 gap-fix
# (D-jsonb-uuid-serializer-engine-default-2026-05-20):
#
# SQLAlchemy's default JSONB column serializer is `json.dumps` with no
# `default=` hook, which raises `TypeError: Object of type UUID is not
# JSON serializable` the moment any caller stuffs a `uuid.UUID` instance
# into a JSONB column value. The Step 30a.7 cascade in admin_service +
# the cascade-orphan backfill script both build `after_json` payloads
# containing `affected_pks=[uuid.UUID(...), ...]` (scope_assignments and
# user_invites both have UUID primary keys). Without an engine-level
# coercion hook, every audit emission for those two layers blows up at
# INSERT time and rolls back the whole per-tenant transaction.
#
# We install `default=str` as the engine-wide JSON serializer fallback.
# This is safe because:
#   - `json.dumps` only invokes `default` for types it cannot serialize
#     natively (so existing dict/list/int/bool/None payloads are
#     unaffected, byte-for-byte);
#   - `str(uuid.UUID(...))` is the canonical RFC-4122 hex form already
#     used by every other audit caller that explicitly coerces UUIDs
#     (e.g. sessions Layer 11 at backfill_cascade_orphans.py:368);
#   - `str(datetime)` produces ISO-8601-ish output that audit-chain
#     hashing already normalises before hashing (see
#     audit_chain.canonical_row_hash at line 131-134).
#
# This is the structural choke point: every code path that uses the ORM
# binds against this engine, so installing the serializer here closes
# the gap for ALL current and future audit callers, not just the two
# fixed at their call sites in the same commit.
engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    json_serializer=lambda obj: json.dumps(obj, default=str),
)

SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
    class_=Session,
)

# Step 29.y gap-fix C25
# (D-audit-chain-listener-only-in-app-main-2026-05-08):
#
# The before_flush listener that populates row_hash / prev_row_hash on
# every AdminAuditLog row used to be installed only by app/main.py and
# app/worker/celery_app.py at process boot. Any code path that imports
# SessionLocal directly without also importing app.main (notably: ad-hoc
# operator heredocs run inside the prod-ops container, one-off scripts,
# REPL sessions) constructs sessions whose flushes do NOT trigger the
# chain handler -- silently writing audit rows with NULL row_hash /
# prev_row_hash and creating a forensic gap.
#
# Postmortem 2026-05-08-platform-admin-consolidation documents one such
# incident: row 3445 was written with NULL hashes during the Step 29.y
# platform-admin key consolidation, then backfilled by hand. To prevent
# recurrence we install the event listener here, at session.py module-
# import time. Every code path that uses the ORM must import SessionLocal
# (or get_db), so installing here is the structural choke point that
# makes "forgot to install the listener" impossible.
#
# install_audit_chain_event() is idempotent (it checks event.contains(...)
# before listening), so the redundant calls in app/main.py and
# app/worker/celery_app.py remain harmless. We deliberately keep those
# call sites as defense-in-depth in case a future refactor moves
# SessionLocal construction lazily.
from app.repositories.audit_chain import install_audit_chain_event  # noqa: E402

install_audit_chain_event()


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()