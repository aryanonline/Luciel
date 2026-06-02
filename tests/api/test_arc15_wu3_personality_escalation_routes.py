"""Arc 15 WU3 — personality + escalation-contact route wiring (AST shape).

Same AST convention as test_arc15_instance_config_routes.py: protects the
WU3 router wiring (four-walls auth, tier gates, audit, no raw-prompt /
no-trigger guards) without a live TestClient/DB. Behaviour is covered in
tests/policy/test_arc15_escalation_config_validation.py and the WU1
validation tests.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PERSONALITY_PATH = REPO_ROOT / "app" / "api" / "v1" / "admin_personality.py"
ESCALATION_PATH = REPO_ROOT / "app" / "api" / "v1" / "admin_escalation.py"
PERSONALITY_SCHEMA = REPO_ROOT / "app" / "schemas" / "personality.py"
ESCALATION_SCHEMA = REPO_ROOT / "app" / "schemas" / "escalation.py"
ROUTER_PATH = REPO_ROOT / "app" / "api" / "router.py"
AUDIT_PATH = REPO_ROOT / "app" / "models" / "admin_audit_log.py"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _parse(p: Path) -> ast.Module:
    return ast.parse(_read(p))


def _function_node(path: Path, name: str) -> ast.FunctionDef:
    for node in ast.walk(_parse(path)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function {name!r} not found in {path.name}")


# ---------------------------------------------------------------------
# Routers are registered.
# ---------------------------------------------------------------------


def test_routers_registered_in_api_router() -> None:
    src = _read(ROUTER_PATH)
    assert "admin_personality" in src
    assert "admin_escalation" in src
    assert "api_router.include_router(admin_personality.router)" in src
    assert "api_router.include_router(admin_escalation.router)" in src


def test_router_prefixes() -> None:
    assert (
        'prefix="/admin/instances/{instance_id}/personality"'
        in _read(PERSONALITY_PATH)
    )
    assert (
        'prefix="/admin/instances/{instance_id}/escalation"'
        in _read(ESCALATION_PATH)
    )


# ---------------------------------------------------------------------
# Four-walls auth on both PUT handlers.
# ---------------------------------------------------------------------


def test_personality_put_enforces_four_walls() -> None:
    src = ast.unparse(_function_node(PERSONALITY_PATH, "put_personality_config"))
    assert "_require_admin_id" in src
    assert "_require_configure_channels" in src
    assert "_load_active_instance" in src
    assert "_resolve_admin_tier" in src


def test_escalation_put_enforces_four_walls() -> None:
    src = ast.unparse(_function_node(ESCALATION_PATH, "put_escalation_config"))
    assert "_require_admin_id" in src
    assert "_require_configure_channels" in src
    assert "_load_active_instance" in src
    assert "_resolve_admin_tier" in src


def test_both_use_configure_channels_permission() -> None:
    for p in (PERSONALITY_PATH, ESCALATION_PATH):
        src = _read(p)
        assert "PERM_CONFIGURE_CHANNELS" in src


# ---------------------------------------------------------------------
# Personality: custom→403 tier gate; business_context→422; audit.
# ---------------------------------------------------------------------


def test_personality_put_custom_preset_403_on_free() -> None:
    src = ast.unparse(_function_node(PERSONALITY_PATH, "put_personality_config"))
    assert "custom_personality_enabled" in src
    assert "status.HTTP_403_FORBIDDEN" in src
    assert "custom_preset_not_available_on_tier" in src


def test_personality_put_business_context_422() -> None:
    src = ast.unparse(_function_node(PERSONALITY_PATH, "put_personality_config"))
    assert "validate_pillars_for_tier" in src
    assert "HTTP_422_UNPROCESSABLE_ENTITY" in src
    assert "personality_config_invalid_for_tier" in src


def test_personality_put_writes_audit() -> None:
    src = ast.unparse(_function_node(PERSONALITY_PATH, "put_personality_config"))
    assert "AdminAuditRepository" in src
    assert "ACTION_PERSONALITY_UPDATED" in src
    assert "RESOURCE_INSTANCE_PERSONALITY" in src


def test_personality_put_persists_axes_only_for_custom() -> None:
    src = ast.unparse(_function_node(PERSONALITY_PATH, "put_personality_config"))
    # Named presets must NOT persist axes (axes resolved from code).
    assert "if body.personality_preset == 'custom' else None" in src


def test_personality_audit_records_length_not_body() -> None:
    # The free-text business_context body must never enter the audit chain.
    src = ast.unparse(_function_node(PERSONALITY_PATH, "put_personality_config"))
    assert "business_context_len" in src


# ---------------------------------------------------------------------
# Escalation: trigger-rejection + tier validation + audit.
# ---------------------------------------------------------------------


def test_escalation_put_validates_and_rejects_triggers() -> None:
    src = ast.unparse(_function_node(ESCALATION_PATH, "put_escalation_config"))
    assert "validate_escalation_config_for_tier" in src
    assert "HTTP_422_UNPROCESSABLE_ENTITY" in src
    assert "escalation_config_invalid" in src


def test_escalation_put_writes_audit() -> None:
    src = ast.unparse(_function_node(ESCALATION_PATH, "put_escalation_config"))
    assert "AdminAuditRepository" in src
    assert "ACTION_ESCALATION_CONFIG_UPDATED" in src
    assert "RESOURCE_INSTANCE_ESCALATION" in src


# ---------------------------------------------------------------------
# No raw-prompt-authoring hook on the personality schema.
# ---------------------------------------------------------------------


def test_personality_schema_has_no_raw_prompt_field() -> None:
    # Inspect the actual field set of PersonalityConfigUpdate via AST — a
    # docstring may legitimately *mention* the absent field; what matters
    # is that no such field is DECLARED.
    tree = _parse(PERSONALITY_SCHEMA)
    fields: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "PersonalityConfigUpdate":
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(
                    stmt.target, ast.Name
                ):
                    fields.add(stmt.target.id)
    assert fields == {
        "personality_preset",
        "personality_axes",
        "business_context",
    }, fields
    for forbidden in ("system_prompt_additions", "raw_prompt", "raw_stanza"):
        assert forbidden not in fields


def test_personality_update_forbids_extra_fields() -> None:
    src = _read(PERSONALITY_SCHEMA)
    assert 'extra="forbid"' in src


def test_escalation_update_forbids_extra_fields() -> None:
    # extra=forbid is the schema-layer backstop; the policy guard is the
    # real defence, but forbidding extras keeps stray trigger fields out.
    src = _read(ESCALATION_SCHEMA)
    assert 'extra="forbid"' in src


# ---------------------------------------------------------------------
# Audit constants are whitelisted (record() would reject otherwise).
# ---------------------------------------------------------------------


def test_audit_constants_whitelisted() -> None:
    src = _read(AUDIT_PATH)
    for const in (
        "ACTION_PERSONALITY_UPDATED",
        "ACTION_ESCALATION_CONFIG_UPDATED",
        "RESOURCE_INSTANCE_PERSONALITY",
        "RESOURCE_INSTANCE_ESCALATION",
    ):
        # Defined AND appears at least twice (definition + tuple member).
        assert src.count(const) >= 2, const
