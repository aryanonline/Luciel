import json
import logging
from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings

logger = logging.getLogger(__name__)


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

# Arc 9.2 PR #96 -- dual-write tenant_id -> admin_id on every INSERT until the
# backend (PR #98) writes admin_id natively.  The hook is idempotent and is
# removed wholesale in PR #101 when tenant_id is finally dropped.
from app.db.admin_id_dual_write import install_admin_id_dual_write  # noqa: E402

install_admin_id_dual_write()


# Arc 9 C2 — In-app RLS connection-pool wrapper (Layer 3 of Wall 1).
#
# Every request-scoped DB session, on BEGIN, sets the PostgreSQL GUC
# ``app.admin_id`` from the in-process ContextVar populated by the
# FastAPI dependency ``get_tenant_scoped_db`` (or by background-task
# wiring in worker tasks). The matching RLS policies (Arc 9 C3) read
# this GUC via ``current_setting('app.admin_id', true)`` and reject
# rows whose tenant_id does not match.
#
# We use SQLAlchemy's ``after_begin`` event rather than a raw DBAPI
# ``checkout`` listener because:
#   - ``SET LOCAL`` is transaction-scoped; it requires an active
#     transaction to bind to. ``checkout`` fires before BEGIN, so a
#     ``SET LOCAL`` issued there would silently no-op (or worse, leak
#     across requests if the driver auto-promotes to SET).
#   - ``after_begin`` fires exactly once per transaction, after BEGIN
#     but before the first query of the unit-of-work, which is the
#     structurally correct moment.
#   - On rollback/commit, ``SET LOCAL`` automatically clears -- no
#     ``RESET`` call is required at session close.
#
# Behaviour matrix:
#   flag=False (v1 default):
#       Listener installed but exits immediately. Zero PostgreSQL
#       traffic added. No behaviour change vs pre-C2.
#   flag=True, admin_id set in ContextVar:
#       Issues ``SELECT set_config('app.admin_id', '<uuid>', true)``
#       on every BEGIN. (Equivalent to SET LOCAL but parameterisable.)
#   flag=True, admin_id NOT set in ContextVar (background job /
#   health check / unauthenticated path):
#       Issues ``SELECT set_config('app.admin_id', '', true)``. RLS
#       policies treat empty string as "no tenant context" and apply
#       their default-deny rule. This means background tasks MUST
#       explicitly set the admin_id before touching customer-data
#       tables -- which is the correct security posture.
from app.core.config import settings  # noqa: E402
from sqlalchemy import event  # noqa: E402
from app.db.tenant_context import get_current_admin_id  # noqa: E402
from app.db.instance_context import get_current_instance_id  # noqa: E402


@event.listens_for(SessionLocal, "after_begin")
def _set_tenant_context_on_begin(session, transaction, connection):
    """Push the in-process admin_id + instance_id into PostgreSQL on every BEGIN.

    Arc 9 C2 introduced the admin_id (Wall 1) push.
    Arc 9 C4.1 added the instance_id (Wall 3) push alongside it. Both
    GUCs are set on the same after_begin event so they bind to the
    same transaction lifecycle and clear together at COMMIT/ROLLBACK
    when SET LOCAL goes out of scope.

    No-op when the master feature flag is False. Idempotent within a
    transaction (SQLAlchemy guarantees one after_begin per BEGIN).

    Both GUCs use the same is_local=true scoping. Both treat empty
    string as "no context" and the matching RLS policies treat that
    state as either deny (Wall 1 strict tables) or NULL-permissive
    read (Wall 3 + asymmetric Wall 1 tables -- knowledge_embeddings,
    retention_policies, deletion_logs, api_keys).
    """
    if not settings.rls_tenant_context_enabled:
        return

    # --- Wall 1 (admin_id) --------------------------------------
    # set_config(name, value, is_local) is the parameterisable
    # equivalent of ``SET LOCAL``. is_local=true scopes the change to
    # the current transaction so the connection returns to the pool
    # clean. Passing empty string for no-context is intentional --
    # RLS policies SHOULD compare to current_setting('app.admin_id',
    # true) and treat empty/missing as deny.
    admin_id = get_current_admin_id()
    admin_value = str(admin_id) if admin_id is not None else ""
    connection.exec_driver_sql(
        "SELECT set_config('app.admin_id', %s, true)",
        (admin_value,),
    )

    # --- Wall 3 (instance_id) -- Arc 9 C4.1 ---------------------
    # instances.id is Integer, not a slug. We serialise to decimal
    # text here and the C4.3 RLS policies cast the column to text
    # before comparing (``luciel_instance_id::text =
    # current_setting(...)``). Empty string means "no instance
    # context" and matches NULL-permissive reads on every Wall 3
    # table.
    instance_id = get_current_instance_id()
    instance_value = str(instance_id) if instance_id is not None else ""
    connection.exec_driver_sql(
        "SELECT set_config('app.instance_id', %s, true)",
        (instance_value,),
    )


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# Arc 9 C6.3 -- BYPASSRLS ops session (Wall 1 escape hatch).
#
# The luciel_ops Postgres role (Arc 9 C6.1) carries BYPASSRLS and a
# narrow grant matrix:
#   - SELECT-only on admin_audit_logs (forward-only immutability;
#     UPDATE/DELETE blocked by the C6.2 RESTRICTIVE policies anyway)
#   - SELECT + DELETE on the eight retention tables listed in
#     admin_service.delete_admin_cascade (sessions, conversations,
#     identity_claims, memory_items, api_keys, luciel_instances,
#     agents, agent_configs)
#   - No INSERT, no UPDATE, no sequence USAGE, no grants on the
#     auth perimeter (admins, tenant_configs, users, user_invites,
#     user_consents)
#
# This session exists so forensic queries and the admin-delete
# cascade can cross the tenant fence WITHOUT temporarily disabling
# RLS, running as superuser, or relying on a connection that
# carries application-managed tenant GUCs.
#
# DESIGN: ops_engine + OpsSessionLocal are constructed as a
# SEPARATE SQLAlchemy sessionmaker from SessionLocal. The Arc 9 C2
# tenant-context listener (_set_tenant_context_on_begin above) is
# attached to SessionLocal specifically, so OpsSessionLocal is
# naturally GUC-free. This is the structural guarantee that an ops
# session can NEVER emit app.admin_id or app.instance_id even if a
# caller forgets to clear the ContextVar before opening it.
#
# We still install_audit_chain_event() above (idempotent across
# every sessionmaker bound to any engine), so if an ops session
# ever did emit an audit row (it cannot today -- no INSERT grant)
# the chain handler would still run.
#
# Fail-closed: ops_engine is constructed lazily only when
# ``settings.luciel_ops_db_url`` is set. ``get_ops_db_session()``
# raises RuntimeError if the URL is unset, so local dev / CI never
# accidentally acquire a BYPASSRLS connection.
ops_engine: Engine | None = None
OpsSessionLocal: sessionmaker[Session] | None = None

if settings.luciel_ops_db_url is not None:
    ops_engine = create_engine(
        settings.luciel_ops_db_url,
        pool_pre_ping=True,
        json_serializer=lambda obj: json.dumps(obj, default=str),
    )
    OpsSessionLocal = sessionmaker(
        bind=ops_engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
        class_=Session,
    )
    # install_audit_chain_event() is idempotent and already invoked
    # at module import (line 80) -- re-invoking is a no-op but kept
    # here as defense-in-depth so the intent is documented at the
    # ops sessionmaker construction site.
    install_audit_chain_event()

    # Arc 9 C7.1 (D7.1): emit a structured log line on every
    # BYPASSRLS ops connection. CloudWatch metric filter
    # ``luciel-backend-ops-role-connect`` parses this exact string
    # (see cfn/luciel-prod-alarms.yaml) and feeds the
    # ``OpsRoleConnectCount`` metric that powers the Medium-severity
    # ``luciel-ops-role-connect-velocity`` alarm.
    #
    # Flag-gated on ``audit_log_immutability_enabled``: the line is
    # only emitted once C9 has flipped the master switch, so dev /
    # CI / staging environments that legitimately stand up
    # OpsSessionLocal for integration tests don't spam CloudWatch
    # with noise that would skew the velocity baseline.
    #
    # The log line format is contract-locked with
    # tests/db/test_c7_1_ops_connect_log_format.py -- changing
    # either the prefix or the field order requires updating both
    # the CloudWatch FilterPattern and the test.
    @event.listens_for(ops_engine, "connect")
    def _arc9_c7_emit_ops_connect_event(dbapi_connection, connection_record):
        if not settings.audit_log_immutability_enabled:
            return
        # Single-line, key=value, machine-parseable. CloudWatch
        # MetricFilter uses the literal prefix as its FilterPattern
        # so a copy/paste of this string (with any pid) matches.
        logger.info(
            "arc9.c7.ops_role_connect role=luciel_ops event=connect"
        )


@contextmanager
def get_ops_db_session() -> Generator[Session, None, None]:
    """Yield a BYPASSRLS ops session bound to the luciel_ops role.

    Fail-closed: raises RuntimeError when ``settings.luciel_ops_db_url``
    is None (the default outside production). Callers MUST NOT catch
    this and fall back to SessionLocal -- the absence of the URL is
    the signal that ops capability is not available in this
    environment.

    Usage::

        from app.db.session import get_ops_db_session

        with get_ops_db_session() as ops_db:
            rows = ops_db.execute(
                select(AdminAuditLog).where(...)
            ).scalars().all()

    The session uses the luciel_ops Postgres role, which:
      - BYPASSRLS -- sees all rows across all tenants
      - Can SELECT admin_audit_logs but NOT UPDATE/DELETE it
        (enforced by Arc 9 C6.2 RESTRICTIVE policies)
      - Can SELECT + DELETE the eight retention tables
      - Cannot touch the auth perimeter (admins, tenant_configs,
        users, user_invites, user_consents) -- those grants are
        deliberately omitted

    The yielded session does NOT emit ``app.admin_id`` or
    ``app.instance_id`` GUCs (the tenant-context after_begin
    listener is attached to SessionLocal only, not OpsSessionLocal).
    """
    if OpsSessionLocal is None:
        raise RuntimeError(
            "get_ops_db_session() called but settings.luciel_ops_db_url "
            "is not set. The luciel_ops BYPASSRLS connection is "
            "production-only -- set the LUCIEL_OPS_DB_URL env var "
            "(minted by scripts/mint_ops_db_password_ssm.py and "
            "injected via SSM /luciel/production/ops_database_url) "
            "to enable it."
        )
    db = OpsSessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()