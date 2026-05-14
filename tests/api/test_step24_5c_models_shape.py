"""Step 24.5c model + migration shape contract tests (backend-free).

Sub-branch 1 of the Step 24.5c implementation arc.

Purpose
=======

These are AST + ORM-introspection tests that pin the cross-file
invariants the design-lock pass (commit c98d752 / PR #23) committed to
across three files:

    app/models/conversation.py
    app/models/identity_claim.py
    alembic/versions/3dbbc70d0105_step24_5c_conversations_and_identity_claims.py

plus the surgical edits the sub-branch makes to two existing files:

    app/models/session.py     (adds nullable conversation_id FK)
    app/models/user.py        (adds identity_claims back-population)
    app/models/__init__.py    (registers Conversation + IdentityClaim)

A break in any of these contracts would silently weaken the spec
committed in ARCHITECTURE §3.2.11 -- e.g. a future refactor that
changed conversations.tenant_id to use the tenant_configs.id integer
FK instead of the tenant_configs.tenant_id string, or that made
sessions.conversation_id NOT NULL, or that dropped the load-bearing
uniqueness on (claim_type, claim_value, tenant_id, domain_id), would
not necessarily fail any other unit test but would silently invalidate
the design contract the impl arc rests on.

These tests are backend-free: they import the model classes, inspect
their SQLAlchemy table metadata, and parse the migration file as AST.
No database, no postgres service, no alembic runtime. They run in the
existing "AST + unit tests (backend-free)" CI lane alongside the Step
30d harness contract tests.

Cross-refs
==========

- ARCHITECTURE §3.2.11 (canonical spec).
- CANONICAL_RECAP §11 Q8 + §12 Step 24.5c row.
- DRIFTS.md D-step-24-5c-impl-backlog-2026-05-11.
- tests/api/test_widget_e2e_harness_shape.py (Step 30d precedent for
  the AST+introspection contract pattern).
"""
from __future__ import annotations

import ast
import pathlib
from typing import Any

import pytest


# ----------------------------------------------------------------------
# Lazy imports -- we import the models inside test bodies so that test
# collection itself does not require sqlalchemy. The CI lane installs
# the project in editable mode, so the import will succeed at run time;
# but defensive lazy-import keeps the failure mode obvious if anyone
# tries to run these tests against a stripped environment.
# ----------------------------------------------------------------------


def _import_models() -> dict[str, Any]:
    from app.models import (
        ClaimType,
        Conversation,
        IdentityClaim,
        SessionModel,
        User,
    )

    return {
        "ClaimType": ClaimType,
        "Conversation": Conversation,
        "IdentityClaim": IdentityClaim,
        "SessionModel": SessionModel,
        "User": User,
    }


# ----------------------------------------------------------------------
# File-path constants. Absolute paths relative to the repo root so the
# tests work from any CWD.
# ----------------------------------------------------------------------

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
MIGRATION_PATH = (
    REPO_ROOT
    / "alembic"
    / "versions"
    / "3dbbc70d0105_step24_5c_conversations_and_identity_claims.py"
)
CONVERSATION_MODEL_PATH = REPO_ROOT / "app" / "models" / "conversation.py"
IDENTITY_CLAIM_MODEL_PATH = REPO_ROOT / "app" / "models" / "identity_claim.py"
SESSION_MODEL_PATH = REPO_ROOT / "app" / "models" / "session.py"
MODELS_INIT_PATH = REPO_ROOT / "app" / "models" / "__init__.py"


# ======================================================================
# Conversation model contract
# ======================================================================


class TestConversationTableShape:
    """conversations table matches the spec in ARCHITECTURE §3.2.11."""

    def test_table_name(self) -> None:
        m = _import_models()
        assert m["Conversation"].__tablename__ == "conversations"

    def test_required_columns_present(self) -> None:
        """The six columns named in §3.2.11 all exist."""
        m = _import_models()
        cols = {c.name for c in m["Conversation"].__table__.columns}
        expected = {
            "id",
            "tenant_id",
            "domain_id",
            "last_activity_at",
            "active",
            "created_at",
        }
        assert expected.issubset(cols), f"missing columns: {expected - cols}"

    def test_no_unexpected_columns(self) -> None:
        """The table does not silently grow columns the spec did not name."""
        m = _import_models()
        cols = {c.name for c in m["Conversation"].__table__.columns}
        expected = {
            "id",
            "tenant_id",
            "domain_id",
            "last_activity_at",
            "active",
            "created_at",
            # Step 30a.2: cascade-deactivation timestamp. ARCHITECTURE
            # §3.2.11 row was updated in the same commit that added the
            # alembic migration dfea1a04e037; this row is intentional.
            "deactivated_at",
        }
        unexpected = cols - expected
        assert not unexpected, (
            f"Conversation grew columns not in §3.2.11 spec: {unexpected}. "
            f"If this is intentional, update §3.2.11 first, then this test."
        )

    def test_id_is_uuid_pk(self) -> None:
        """PK is UUID, matching 24.5b User/ScopeAssignment discipline."""
        from sqlalchemy.dialects.postgresql import UUID as PG_UUID

        m = _import_models()
        id_col = m["Conversation"].__table__.columns["id"]
        assert id_col.primary_key, "Conversation.id must be primary key"
        # PG_UUID is a TypeDecorator-ish wrapper; isinstance check is the
        # reliable shape assertion.
        assert isinstance(id_col.type, PG_UUID), (
            f"Conversation.id type is {type(id_col.type).__name__}, expected PG_UUID"
        )

    def test_tenant_id_has_fk_to_tenant_configs(self) -> None:
        """tenant_id FKs tenant_configs.tenant_id RESTRICT (matches §3.2.11
        and the existing 24.5b scope_assignments convention)."""
        m = _import_models()
        col = m["Conversation"].__table__.columns["tenant_id"]
        fks = list(col.foreign_keys)
        assert len(fks) == 1, f"Conversation.tenant_id should have exactly one FK, has {len(fks)}"
        fk = fks[0]
        # Reference target check: tenant_configs.tenant_id (the NATURAL
        # key string, not the integer surrogate id).
        assert fk.column.table.name == "tenant_configs"
        assert fk.column.name == "tenant_id"
        assert fk.ondelete == "RESTRICT", (
            f"Conversation.tenant_id FK ondelete must be RESTRICT (identity "
            f"history protection), is {fk.ondelete}"
        )

    def test_domain_id_has_no_fk(self) -> None:
        """domain_id intentionally has no FK -- composite natural key in
        domain_configs, validated at service layer. Matches the
        scope_assignments convention."""
        m = _import_models()
        col = m["Conversation"].__table__.columns["domain_id"]
        assert not list(col.foreign_keys), (
            "Conversation.domain_id must have NO foreign key. "
            "domain_configs uses (tenant_id, domain_id) as a composite natural "
            "key; a single-column FK would be a half-truth. See §3.2.11 + "
            "scope_assignments precedent in 24.5b File 1.2."
        )

    def test_active_is_nonnullable_with_default_true(self) -> None:
        m = _import_models()
        col = m["Conversation"].__table__.columns["active"]
        assert col.nullable is False
        # server_default's text() value is sqlalchemy-version-sensitive in
        # exact repr; check for "true" anywhere in the rendered string.
        assert col.server_default is not None
        assert "true" in str(col.server_default.arg).lower()


class TestConversationRelationships:
    """Conversation 1..N SessionModel back-population works under
    configure_mappers (the failure mode 24.5b's File 2.8 docstring warns
    about explicitly)."""

    def test_configure_mappers_passes(self) -> None:
        """Importing all models + configure_mappers() does not raise."""
        from sqlalchemy.orm import configure_mappers

        _import_models()
        # configure_mappers() will raise if any back-population pair is
        # broken; e.g. a typo in foreign_keys="..." string, or a mismatched
        # back_populates name.
        configure_mappers()

    def test_sessions_backpopulation(self) -> None:
        m = _import_models()
        assert hasattr(m["Conversation"], "sessions"), (
            "Conversation must expose a `sessions` relationship to back-"
            "populate from SessionModel.conversation."
        )


# ======================================================================
# IdentityClaim model contract
# ======================================================================


class TestIdentityClaimTableShape:
    """identity_claims table matches the spec in ARCHITECTURE §3.2.11."""

    def test_table_name(self) -> None:
        m = _import_models()
        assert m["IdentityClaim"].__tablename__ == "identity_claims"

    def test_required_columns_present(self) -> None:
        m = _import_models()
        cols = {c.name for c in m["IdentityClaim"].__table__.columns}
        expected = {
            "id",
            "user_id",
            "claim_type",
            "claim_value",
            "tenant_id",
            "domain_id",
            "issuing_adapter",
            "verified_at",
            "active",
            "created_at",
        }
        assert expected.issubset(cols), f"missing columns: {expected - cols}"

    def test_no_unexpected_columns(self) -> None:
        m = _import_models()
        cols = {c.name for c in m["IdentityClaim"].__table__.columns}
        expected = {
            "id",
            "user_id",
            "claim_type",
            "claim_value",
            "tenant_id",
            "domain_id",
            "issuing_adapter",
            "verified_at",
            "active",
            "created_at",
            # Step 30a.2: cascade-deactivation timestamp. Same source
            # of truth as Conversation.deactivated_at above; both flip
            # in lockstep when AdminService.deactivate_tenant_with_cascade
            # tears a tenant down.
            "deactivated_at",
        }
        unexpected = cols - expected
        assert not unexpected, (
            f"IdentityClaim grew columns not in §3.2.11 spec: {unexpected}. "
            f"If intentional, update §3.2.11 first, then this test."
        )

    def test_claim_type_enum_values(self) -> None:
        """ClaimType is exactly {EMAIL, PHONE, SSO_SUBJECT} per §3.2.11."""
        m = _import_models()
        values = {c.value for c in m["ClaimType"]}
        assert values == {"EMAIL", "PHONE", "SSO_SUBJECT"}, (
            f"ClaimType drift: got {values}. §3.2.11 names exactly "
            f"{{EMAIL, PHONE, SSO_SUBJECT}}. Adding a value is a §3.2.11 "
            f"spec change first, not a code change first."
        )

    def test_user_id_fk_to_users_restrict(self) -> None:
        m = _import_models()
        col = m["IdentityClaim"].__table__.columns["user_id"]
        fks = list(col.foreign_keys)
        assert len(fks) == 1
        fk = fks[0]
        assert fk.column.table.name == "users"
        assert fk.column.name == "id"
        assert fk.ondelete == "RESTRICT"

    def test_tenant_id_fk_to_tenant_configs_natural_key(self) -> None:
        m = _import_models()
        col = m["IdentityClaim"].__table__.columns["tenant_id"]
        fks = list(col.foreign_keys)
        assert len(fks) == 1
        fk = fks[0]
        assert fk.column.table.name == "tenant_configs"
        assert fk.column.name == "tenant_id"  # natural key, not surrogate `id`
        assert fk.ondelete == "RESTRICT"

    def test_domain_id_has_no_fk(self) -> None:
        m = _import_models()
        col = m["IdentityClaim"].__table__.columns["domain_id"]
        assert not list(col.foreign_keys), (
            "IdentityClaim.domain_id must have NO foreign key -- composite "
            "natural key convention. See scope_assignments precedent."
        )

    def test_verified_at_is_nullable(self) -> None:
        """verified_at MUST be nullable -- v1 trust model is adapter-asserted;
        end-user-driven verification lands later (Step 34a + Step 31)."""
        m = _import_models()
        col = m["IdentityClaim"].__table__.columns["verified_at"]
        assert col.nullable is True, (
            "IdentityClaim.verified_at must be nullable. v1 records claims "
            "with verified_at=NULL; verification lands with Step 34a + Step "
            "31 per §3.2.11."
        )


class TestIdentityClaimUniqueness:
    """The load-bearing uniqueness constraint that lets two scopes
    independently assert the same number/email."""

    def test_uniqueness_constraint_name_and_columns(self) -> None:
        from sqlalchemy import UniqueConstraint

        m = _import_models()
        uniques = [
            c
            for c in m["IdentityClaim"].__table__.constraints
            if isinstance(c, UniqueConstraint)
        ]
        # Filter out implicit single-column unique=True declarations
        # (none expected on this table, but be defensive).
        named = [u for u in uniques if u.name == "uq_identity_claims_type_value_scope"]
        assert len(named) == 1, (
            "Missing the load-bearing uniqueness constraint "
            "uq_identity_claims_type_value_scope. This is what lets two "
            "scopes independently assert the same value (e.g. the same "
            "phone number for Brokerage A's prospect and Brokerage B's "
            "prospect). See §3.2.11."
        )
        cols = [c.name for c in named[0].columns]
        # Order doesn't matter for uniqueness, but the *set* is what's
        # load-bearing. We assert the set so a future re-ordering of the
        # constraint definition doesn't trip the test for the wrong reason.
        assert set(cols) == {"claim_type", "claim_value", "tenant_id", "domain_id"}


# ======================================================================
# SessionModel surgical edit contract
# ======================================================================


class TestSessionConversationIdShape:
    """sessions.conversation_id is nullable FK to conversations.id with
    ON DELETE SET NULL, per §3.2.11 + the spec's session-linking-not-
    session-merging design contract."""

    def test_column_present(self) -> None:
        m = _import_models()
        cols = {c.name for c in m["SessionModel"].__table__.columns}
        assert "conversation_id" in cols, (
            "SessionModel must gain a conversation_id column in 24.5c "
            "sub-branch 1. See §3.2.11."
        )

    def test_column_is_nullable(self) -> None:
        """Nullable is the design contract -- a session that arrives with
        no continuity claim stays as a single-session conversation."""
        m = _import_models()
        col = m["SessionModel"].__table__.columns["conversation_id"]
        assert col.nullable is True, (
            "sessions.conversation_id MUST be nullable. Existing sessions "
            "stay at NULL and the v1 design does not retroactively group "
            "historical traffic -- continuity emerges as new sessions arrive "
            "bound to a User via identity_claims. See §3.2.11."
        )

    def test_column_is_uuid(self) -> None:
        from sqlalchemy.dialects.postgresql import UUID as PG_UUID

        m = _import_models()
        col = m["SessionModel"].__table__.columns["conversation_id"]
        assert isinstance(col.type, PG_UUID)

    def test_fk_to_conversations_set_null(self) -> None:
        m = _import_models()
        col = m["SessionModel"].__table__.columns["conversation_id"]
        fks = list(col.foreign_keys)
        assert len(fks) == 1
        fk = fks[0]
        assert fk.column.table.name == "conversations"
        assert fk.column.name == "id"
        assert fk.ondelete == "SET NULL", (
            "sessions.conversation_id FK ondelete must be SET NULL -- this "
            "preserves the session row's audit integrity if a Conversation "
            "is ever administratively pruned. See §3.2.11."
        )


# ======================================================================
# User surgical edit contract
# ======================================================================


class TestUserIdentityClaimsRelationship:
    """User.identity_claims back-populates IdentityClaim.user, completing
    the bidirectional pair per the 24.5b discipline."""

    def test_relationship_exists(self) -> None:
        m = _import_models()
        assert hasattr(m["User"], "identity_claims"), (
            "User must expose `identity_claims` back-populated from "
            "IdentityClaim.user."
        )


# ======================================================================
# Models __init__ registration contract
# ======================================================================


class TestModelsInitRegistration:
    """Conversation + IdentityClaim + ClaimType are exported from
    app.models. Mapper resolution at runtime depends on the model
    classes being imported eagerly (the project pattern -- see
    app/models/__init__.py)."""

    def test_init_exports(self) -> None:
        import app.models as M

        assert "Conversation" in M.__all__
        assert "IdentityClaim" in M.__all__
        assert "ClaimType" in M.__all__

    def test_init_imports_eagerly(self) -> None:
        """The __init__ file must do `from app.models.conversation import
        Conversation` (and same for identity_claim) so SQLAlchemy's
        registry sees the classes before configure_mappers() runs.
        Inspect the AST rather than the runtime export so we catch the
        case where someone removes the eager-import line but kept the
        __all__ entry."""
        tree = ast.parse(MODELS_INIT_PATH.read_text())
        eager_imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith("app.models."):
                    for alias in node.names:
                        eager_imports.add(alias.name)
        assert "Conversation" in eager_imports, (
            "app.models.__init__ must eagerly import Conversation -- "
            "removing the import line silently breaks mapper resolution."
        )
        assert "IdentityClaim" in eager_imports, (
            "app.models.__init__ must eagerly import IdentityClaim."
        )


# ======================================================================
# Migration AST contract
# ======================================================================
#
# Parse the migration file as AST and assert structural invariants
# without executing any DDL. We can't run alembic in the backend-free
# CI lane (no Postgres), but we CAN assert the shape of the upgrade()
# function and the constants it commits.


class TestMigrationShape:
    """alembic/versions/3dbbc70d0105_*.py commits the design-locked shape."""

    @pytest.fixture(scope="class")
    def migration_text(self) -> str:
        return MIGRATION_PATH.read_text()

    @pytest.fixture(scope="class")
    def migration_ast(self, migration_text: str) -> ast.Module:
        return ast.parse(migration_text)

    def test_file_exists(self) -> None:
        assert MIGRATION_PATH.exists(), (
            f"Migration file not found at {MIGRATION_PATH}. Sub-branch 1 "
            f"of the 24.5c impl arc must commit this file."
        )

    def test_revision_id(self, migration_text: str) -> None:
        # The revision id is referenced from CANONICAL_RECAP and the
        # downstream sub-branches' deploy runbook. Pin it.
        assert 'revision = "3dbbc70d0105"' in migration_text

    def test_down_revision_chains_to_24_5b_widget(self, migration_text: str) -> None:
        # a7c1f4e92b85 = step30b_api_keys_widget_columns, the head at the
        # time the design-lock landed. If a future migration squeezes
        # into the chain ahead of this one, this test breaks and forces
        # us to re-verify chain integrity.
        assert 'down_revision = "a7c1f4e92b85"' in migration_text

    def test_creates_conversations_table(self, migration_text: str) -> None:
        assert 'op.create_table(\n        "conversations"' in migration_text

    def test_creates_identity_claims_table(self, migration_text: str) -> None:
        assert 'op.create_table(\n        "identity_claims"' in migration_text

    def test_adds_sessions_conversation_id_column(self, migration_text: str) -> None:
        # op.add_column with the sessions table and the conversation_id
        # column. The exact whitespace can drift; match on substring.
        assert 'op.add_column(\n        "sessions"' in migration_text
        assert '"conversation_id"' in migration_text

    def test_creates_claim_type_enum(self, migration_text: str) -> None:
        # The CLAIM_TYPE_ENUM_NAME constant in the migration must match
        # the one in the model (both are "identity_claim_type"). If they
        # drift, the model's column type and the migration's CREATE TYPE
        # become two ships in the night.
        assert 'CLAIM_TYPE_ENUM_NAME = "identity_claim_type"' in migration_text

    def test_enum_values_match_model(self, migration_text: str) -> None:
        # Cheap textual check that all three enum values appear in the
        # migration's CLAIM_TYPE_VALUES tuple.
        for v in ("EMAIL", "PHONE", "SSO_SUBJECT"):
            assert f'"{v}"' in migration_text, (
                f"Enum value {v} missing from migration's CLAIM_TYPE_VALUES. "
                f"§3.2.11 names exactly {{EMAIL, PHONE, SSO_SUBJECT}}."
            )

    def test_uniqueness_constraint_in_migration(self, migration_text: str) -> None:
        # The load-bearing constraint must be present in the migration
        # by its named identifier.
        assert "uq_identity_claims_type_value_scope" in migration_text

    def test_downgrade_function_present(self, migration_ast: ast.Module) -> None:
        funcs = [n.name for n in ast.walk(migration_ast) if isinstance(n, ast.FunctionDef)]
        assert "upgrade" in funcs
        assert "downgrade" in funcs, (
            "Migration must have a downgrade(). Invariant 12 (hand-written, "
            "replayable) requires a working reverse path."
        )

    def test_downgrade_drops_in_reverse_phase_order(self, migration_text: str) -> None:
        """Downgrade must reverse C -> B -> A: sessions.conversation_id
        first (depends on conversations.id), then identity_claims
        + enum (depends on users.id + tenant_configs.tenant_id), then
        conversations. If the order inverts, the downgrade hits an FK
        constraint violation."""
        # Find the downgrade() function body by string slicing rather
        # than full AST walk -- robust and readable.
        downgrade_start = migration_text.find("def downgrade(")
        assert downgrade_start > 0
        downgrade_body = migration_text[downgrade_start:]
        # The three table drops must appear in C -> B -> A order
        sess_drop = downgrade_body.find('op.drop_column("sessions", "conversation_id")')
        idclaim_drop = downgrade_body.find('op.drop_table("identity_claims")')
        conv_drop = downgrade_body.find('op.drop_table("conversations")')
        assert sess_drop > 0, "downgrade must drop sessions.conversation_id"
        assert idclaim_drop > 0, "downgrade must drop identity_claims"
        assert conv_drop > 0, "downgrade must drop conversations"
        assert sess_drop < idclaim_drop < conv_drop, (
            "Downgrade order is wrong. Must be C (sessions.conversation_id) "
            "-> B (identity_claims) -> A (conversations). FK constraints "
            "would otherwise reject the reverse-walk."
        )
