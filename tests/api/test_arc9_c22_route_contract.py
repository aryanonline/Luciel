"""Arc 9 C22 -- Route contract + IdentitySnapshot semantics tests.

Pins the V2-only API surface and the IdentityBootstrap zero-state
contract introduced by C22. These are the doctrine invariants the
deployed frontend bundle (bce4360) depends on.

What this file pins
===================

1. Route table contains the V2 ``/admin/instances`` family with
   exactly the documented methods (POST, GET listing, GET/{pk},
   PATCH/{pk}, DELETE/{pk}).

2. Route table does NOT contain any ``/luciel-instances`` path. The
   C19 POST alias and the four C21 GET/PATCH/DELETE aliases were
   removed at C22 once the frontend was rebuilt against V2.

3. ``InstanceCreate`` Pydantic schema accepts the V2 body shape
   directly and REJECTS the legacy C19 shape (no silent coercion
   shim). The deployed frontend posts V2 -- legacy callers must
   error loudly, not be silently translated.

4. ``IdentitySnapshot`` empty/zero-state semantics: a user with no
   active scope yields ``canonical_tenant_id="" / canonical_tier=""
   / active_scopes=[] / has_scope==False``. Callers MUST treat this
   as "no entitlement" (typically 402), never silently proceed.

These tests run without a database. They import the FastAPI app and
inspect ``app.routes`` directly + validate the Pydantic schema +
construct snapshots in-memory.

D-DOC-1: route contract lives in code; this is the pinning file. The
human-readable description anchors to Architecture v1 §3.2 (identity
bootstrap surface on the Instance subsystem).
"""
from __future__ import annotations

import os
import uuid

# Match the env-stub pattern from tests/api/test_signup_free_shape.py
# (must precede any ``from app...`` import).
os.environ.setdefault("MODERATION_PROVIDER", "null")
os.environ.setdefault(
    "DATABASE_URL", "postgresql+psycopg://stub:stub@localhost:5432/stub"
)

import pytest
from pydantic import ValidationError


# =====================================================================
# 1. Route-table contract
# =====================================================================


class TestInstanceRouteContract:
    """The deployed frontend (bce4360) hits these routes exclusively.

    A regression that removes a V2 route or re-introduces a legacy
    alias breaks the production contract.
    """

    @pytest.fixture(scope="class")
    def app(self):
        from app.main import app as fastapi_app
        return fastapi_app

    @pytest.fixture(scope="class")
    def admin_routes(self, app):
        """Routes under /api/v1/admin/, indexed by (path, method)."""
        seen: set[tuple[str, str]] = set()
        for r in app.routes:
            path = getattr(r, "path", "")
            methods = getattr(r, "methods", set()) or set()
            if "/api/v1/admin/" in path:
                for m in methods:
                    if m != "HEAD":  # FastAPI auto-adds HEAD; ignore
                        seen.add((path, m))
        return seen

    def test_v2_post_instances_registered(self, admin_routes):
        assert ("/api/v1/admin/instances", "POST") in admin_routes

    def test_v2_get_instances_listing_registered(self, admin_routes):
        assert ("/api/v1/admin/instances", "GET") in admin_routes

    def test_v2_get_instance_by_pk_registered(self, admin_routes):
        assert ("/api/v1/admin/instances/{pk}", "GET") in admin_routes

    def test_v2_patch_instance_registered(self, admin_routes):
        assert ("/api/v1/admin/instances/{pk}", "PATCH") in admin_routes

    def test_v2_delete_instance_registered(self, admin_routes):
        assert ("/api/v1/admin/instances/{pk}", "DELETE") in admin_routes

    def test_no_legacy_luciel_instances_routes(self, admin_routes):
        """C19/C21 legacy aliases must be fully removed.

        The deployed frontend bundle bce4360 hits V2 exclusively;
        keeping the aliases just preserves a parallel contract no
        client uses and lets future callers re-introduce the legacy
        body shape by mistake.
        """
        legacy = [
            (path, method)
            for (path, method) in admin_routes
            if "/luciel-instances" in path
        ]
        assert legacy == [], (
            f"Legacy /luciel-instances routes must not be registered "
            f"after Arc 9 C22, but found: {legacy}"
        )


# =====================================================================
# 2. InstanceCreate body-shape contract (C19 shim removed at C22)
# =====================================================================


class TestInstanceCreateShape:
    """The schema must accept V2 directly and reject legacy C19 shape."""

    def test_v2_body_accepted(self):
        from app.schemas.instance import InstanceCreate
        m = InstanceCreate(
            admin_id="free-abc123",
            instance_slug="my-instance",
            display_name="My Instance",
        )
        assert m.admin_id == "free-abc123"
        assert m.instance_slug == "my-instance"

    def test_legacy_c19_body_rejected(self):
        """Legacy callers posting {instance_id, scope_owner_tenant_id,
        scope_level} must now get a 422, not silent coercion.

        The C19 _coerce_legacy_body model_validator was deleted at
        C22 once the frontend (bce4360) was rebuilt against V2.
        """
        from app.schemas.instance import InstanceCreate
        with pytest.raises(ValidationError) as ei:
            InstanceCreate(
                instance_id="my-instance",            # legacy key
                scope_owner_tenant_id="free-abc123",  # legacy key
                scope_level="tenant",                 # legacy discriminator
                display_name="My Instance",
            )
        # The required V2 fields are missing -- confirm Pydantic flags
        # both, rather than silently translating from the legacy body.
        missing = {
            err["loc"][0] for err in ei.value.errors()
            if err["type"] == "missing"
        }
        assert "admin_id" in missing
        assert "instance_slug" in missing


# =====================================================================
# 3. IdentitySnapshot zero-state semantics
# =====================================================================


class TestIdentitySnapshotZeroState:
    """``has_scope`` must be False when the user has no scope, so
    HTTP callers can fail-closed (402) instead of silently proceeding.

    Pre-C22 the resolver returned an empty list silently because
    scope_assignments was RLS-gated on app.admin_id (the value we
    were trying to discover). C22 fixes the read path; this test
    pins the surface invariant downstream callers depend on.
    """

    def test_empty_snapshot_has_no_scope(self):
        from app.identity.bootstrap import IdentitySnapshot
        snap = IdentitySnapshot(
            user_id=uuid.uuid4(),
            canonical_tenant_id="",
            canonical_tier="",
            active_scopes=[],
        )
        assert snap.has_scope is False
        assert snap.canonical_role == ""

    def test_snapshot_with_scope_but_empty_tenant_id_has_no_scope(self):
        """Defensive: if the SECDEF returns rows but the canonical
        tenant string is empty (Admin soft-deleted edge case), the
        snapshot must still report no scope so the entitlement layer
        treats it as no entitlement.
        """
        from app.identity.bootstrap import IdentitySnapshot
        from app.models.scope_assignment import ScopeAssignment
        # ScopeAssignment can be constructed without a session; we
        # only need the role/admin_id attributes for has_scope.
        sa = ScopeAssignment(
            id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            admin_id="free-abc",
            role="owner",
            active=True,
        )
        snap = IdentitySnapshot(
            user_id=uuid.uuid4(),
            canonical_tenant_id="",
            canonical_tier="",
            active_scopes=[sa],
        )
        assert snap.has_scope is False

    def test_snapshot_canonical_role_prefers_owner(self):
        from app.identity.bootstrap import IdentitySnapshot
        from app.models.scope_assignment import ScopeAssignment
        u = uuid.uuid4()
        owner = ScopeAssignment(
            id=uuid.uuid4(), user_id=u, admin_id="t1",
            role="owner", active=True,
        )
        member = ScopeAssignment(
            id=uuid.uuid4(), user_id=u, admin_id="t1",
            role="member", active=True,
        )
        snap = IdentitySnapshot(
            user_id=u,
            canonical_tenant_id="t1",
            canonical_tier="free",
            active_scopes=[member, owner],  # owner second on purpose
        )
        assert snap.has_scope is True
        assert snap.canonical_role == "owner"
