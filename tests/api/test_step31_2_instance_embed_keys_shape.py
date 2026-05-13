"""Backend-free contract tests for Step 31.2 commit B -- lift the v1
luciel_instance_id carve-out on POST /admin/embed-keys.

Step 31.2 commit B lets operators mint embed keys pinned to a specific
LucielInstance. Chat resolution already honours request.state.
luciel_instance_id (Step 24.5 / 30b), so this commit is purely an
issuance-time unlock: the schema accepts the field, the route validates
scope, and ApiKeyService.create_key receives the propagated value.

Coverage (AST + import only -- no Postgres, no FastAPI runtime, no
network):

  * EmbedKeyCreate schema -- accepts optional luciel_instance_id (int,
    gt=0), defaults to None, lives in `extra='forbid'` model.
  * EmbedKeyRead schema -- surfaces luciel_instance_id so the UI can
    show 'this key is pinned to instance X'.
  * Route docstring + comment trail -- the lifted carve-out is
    documented at the point of edit so a future doc-truthing pass can
    cross-reference.
  * Validation branches present in admin.py: 404 when instance pk
    missing, 403 when instance.scope_owner_tenant_id mismatch, 403 when
    domain mismatch, 422 when instance inactive.
  * Service propagation -- the route passes payload.luciel_instance_id
    (not None hard-code) to ApiKeyService.create_key.

End-to-end correctness (mint pinned key -> POST /chat/widget ->
response respects instance system prompt) is covered by the companion
live e2e harness at tests/e2e/step_31_2_live_e2e.py.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


# --------------------------------------------------------------------------- #
# EmbedKeyCreate schema accepts luciel_instance_id
# --------------------------------------------------------------------------- #


def test_embed_key_create_has_luciel_instance_id_field() -> None:
    """The schema accepts luciel_instance_id as an optional int."""
    from app.schemas.api_key import EmbedKeyCreate

    fields = EmbedKeyCreate.model_fields
    assert "luciel_instance_id" in fields, (
        "EmbedKeyCreate must accept luciel_instance_id after Step 31.2 "
        "commit B lifted the v1 carve-out."
    )


def test_embed_key_create_luciel_instance_id_default_none() -> None:
    """Default is None -- existing callers that don't pass the field
    continue to mint tenant- or domain-scoped keys as before."""
    from app.schemas.api_key import EmbedKeyCreate

    f = EmbedKeyCreate.model_fields["luciel_instance_id"]
    assert f.default is None, (
        "luciel_instance_id default must be None so existing callers "
        "(unaware of the field) are unaffected."
    )


def test_embed_key_create_luciel_instance_id_positive_int_only() -> None:
    """gt=0 because LucielInstance.id is autoincrement starting at 1.
    A zero or negative value is malformed; Pydantic rejects pre-route."""
    from app.schemas.api_key import EmbedKeyCreate

    # Pydantic v2: metadata reads as Field(...) constraints. We test
    # behaviour rather than introspecting metadata because that varies
    # across versions.
    minimal = {
        "tenant_id": "t1",
        "display_name": "test",
        "allowed_origins": ["https://example.com"],
        "rate_limit_per_minute": 60,
    }

    # Zero rejected:
    with pytest.raises(Exception):
        EmbedKeyCreate(**minimal, luciel_instance_id=0)

    # Negative rejected:
    with pytest.raises(Exception):
        EmbedKeyCreate(**minimal, luciel_instance_id=-1)

    # Positive accepted:
    parsed = EmbedKeyCreate(**minimal, luciel_instance_id=42)
    assert parsed.luciel_instance_id == 42


def test_embed_key_create_omitting_instance_id_still_works() -> None:
    """Backward compatibility: existing callers that never pass the
    new field continue to mint successfully (subject to other
    validation)."""
    from app.schemas.api_key import EmbedKeyCreate

    parsed = EmbedKeyCreate(
        tenant_id="t1",
        display_name="test",
        allowed_origins=["https://example.com"],
        rate_limit_per_minute=60,
    )
    assert parsed.luciel_instance_id is None


# --------------------------------------------------------------------------- #
# EmbedKeyRead surfaces luciel_instance_id
# --------------------------------------------------------------------------- #


def test_embed_key_read_includes_luciel_instance_id() -> None:
    """The read-side projection surfaces luciel_instance_id so the UI
    (Step 32) can render 'this key is pinned to instance X'."""
    from app.schemas.api_key import EmbedKeyRead

    fields = EmbedKeyRead.model_fields
    assert "luciel_instance_id" in fields


# --------------------------------------------------------------------------- #
# Route lifts the v1 carve-out + validates instance scope
# --------------------------------------------------------------------------- #


ADMIN_PY = (
    Path(__file__).parent.parent.parent / "app" / "api" / "v1" / "admin.py"
)


def test_admin_route_no_longer_rejects_with_step_30c_message() -> None:
    """The v1 carve-out message ('Step 30c+ follow-up') has been
    removed. We assert on absence of the specific phrase so a future
    revert is loud."""
    src = ADMIN_PY.read_text()
    assert "Step 30c+ follow-up" not in src, (
        "The v1 luciel_instance_id carve-out message is back. Step 31.2 "
        "commit B is supposed to have lifted it."
    )
    assert "Agent-scoped keys cannot mint embed keys at v1" not in src, (
        "The 'at v1' qualifier on the agent-scope carve-out message is "
        "back; Step 31.2 dropped the v1-specific wording."
    )


def test_admin_route_documents_the_lift() -> None:
    """The route source carries the Step 31.2 commit B comment so a
    future reader can trace why luciel_instance_id is now accepted."""
    src = ADMIN_PY.read_text()
    assert "Step 31.2 commit B: lift the v1 luciel_instance_id carve-out" in src


def test_admin_route_validates_instance_tenant_match() -> None:
    """The route rejects cross-tenant instance pinning with 403."""
    src = ADMIN_PY.read_text()
    assert "scope_owner_tenant_id != payload.tenant_id" in src
    assert "different tenant" in src.lower()


def test_admin_route_validates_instance_domain_match() -> None:
    """The route rejects cross-domain instance pinning (for domain-
    scoped embed keys) with 403."""
    src = ADMIN_PY.read_text()
    assert "scope_owner_domain_id != payload.domain_id" in src
    assert "different domain" in src.lower()


def test_admin_route_rejects_inactive_instance() -> None:
    """The route rejects pinning to an inactive (soft-deleted) instance
    with 422."""
    src = ADMIN_PY.read_text()
    # Find the inactive-instance branch.
    assert "not instance.active" in src
    assert "HTTP_422_UNPROCESSABLE_ENTITY" in src


def test_admin_route_returns_404_for_missing_instance() -> None:
    """The route returns 404 if luciel_instance_id references a
    non-existent row."""
    src = ADMIN_PY.read_text()
    # Look for the 404 branch tied to instance lookup.
    assert "HTTP_404_NOT_FOUND" in src
    assert "Luciel instance pk=" in src


# --------------------------------------------------------------------------- #
# Service propagation: payload.luciel_instance_id is passed through
# --------------------------------------------------------------------------- #


def _find_create_embed_key_function(tree: ast.AST) -> ast.FunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "create_embed_key":
            return node
    return None


def test_route_passes_payload_instance_id_to_service() -> None:
    """The hard-coded `luciel_instance_id=None` in the
    ApiKeyService.create_key call must now read
    `luciel_instance_id=payload.luciel_instance_id`. We AST-walk to
    avoid false-positives from comments."""
    tree = ast.parse(ADMIN_PY.read_text())
    fn = _find_create_embed_key_function(tree)
    assert fn is not None, "create_embed_key function missing from admin.py"

    found_propagation = False
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.keyword)
            and node.arg == "luciel_instance_id"
            and isinstance(node.value, ast.Attribute)
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "payload"
            and node.value.attr == "luciel_instance_id"
        ):
            found_propagation = True
            break

    assert found_propagation, (
        "create_embed_key must call service.create_key with "
        "luciel_instance_id=payload.luciel_instance_id (not None) so "
        "the pinned instance reaches the api_keys row."
    )


def test_route_no_longer_hardcodes_instance_id_none() -> None:
    """The previous hard-code `luciel_instance_id=None` must be gone.
    AST-walk for the keyword + Constant(None) combination."""
    tree = ast.parse(ADMIN_PY.read_text())
    fn = _find_create_embed_key_function(tree)
    assert fn is not None

    for node in ast.walk(fn):
        if (
            isinstance(node, ast.keyword)
            and node.arg == "luciel_instance_id"
            and isinstance(node.value, ast.Constant)
            and node.value.value is None
        ):
            pytest.fail(
                "create_embed_key still hard-codes luciel_instance_id=None "
                "in the service call -- Step 31.2 commit B should have "
                "replaced this with payload.luciel_instance_id."
            )


# --------------------------------------------------------------------------- #
# Chat resolution already honours luciel_instance_id (regression guard)
# --------------------------------------------------------------------------- #


def test_chat_widget_reads_luciel_instance_id_from_state() -> None:
    """chat_widget.py reads request.state.luciel_instance_id and
    propagates it to chat resolution. This is pre-existing behaviour
    (Step 24.5 / 30b); Step 31.2 commit B relies on it. We assert on
    the read so a future refactor that drops it is caught loudly."""
    chat_py = (
        Path(__file__).parent.parent.parent
        / "app"
        / "api"
        / "v1"
        / "chat_widget.py"
    )
    src = chat_py.read_text()
    assert 'request.state, "luciel_instance_id"' in src, (
        "chat_widget.py no longer reads luciel_instance_id from "
        "request.state -- chat resolution will silently fall back to "
        "tenant/domain defaults even for pinned embed keys."
    )


# --------------------------------------------------------------------------- #
# ApiKeyService.create_key signature accepts luciel_instance_id
# --------------------------------------------------------------------------- #


def test_api_key_service_create_key_signature() -> None:
    """ApiKeyService.create_key has accepted luciel_instance_id since
    Step 24.5; this test pins the signature so a refactor that drops
    it is caught."""
    import inspect

    from app.services.api_key_service import ApiKeyService

    sig = inspect.signature(ApiKeyService.create_key)
    assert "luciel_instance_id" in sig.parameters, (
        "ApiKeyService.create_key must accept luciel_instance_id."
    )
