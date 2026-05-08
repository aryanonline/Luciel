from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
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