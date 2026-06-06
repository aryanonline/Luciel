"""Arc 15 WU1 — instance-config-pillar route wiring (AST shape test).

Same AST convention as test_instance_lifecycle_arc11_closeout.py:
protects the create + PATCH route wiring for the tier-conditional pillar
validation without standing up a live TestClient/DB. The behavioural
rules themselves are covered in tests/policy/test_arc15_instance_config_*.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ADMIN_ROUTES_PATH = REPO_ROOT / "app" / "api" / "v1" / "admin" / "__init__.py"
SERVICE_PATH = REPO_ROOT / "app" / "services" / "instance_service.py"
REPO_PATH = REPO_ROOT / "app" / "repositories" / "instance_repository.py"
SCHEMA_PATH = REPO_ROOT / "app" / "schemas" / "instance.py"
MODEL_PATH = REPO_ROOT / "app" / "models" / "instance.py"


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
# create route — tier-conditional pillar validation wired.
# ---------------------------------------------------------------------


def test_create_route_resolves_admin_tier_and_validates_pillars() -> None:
    src = ast.unparse(_function_node(ADMIN_ROUTES_PATH, "create_luciel_instance"))
    assert "_resolve_admin_tier_for_pillars" in src
    assert "validate_pillars_for_tier" in src
    assert "status_code=422" in src
    assert "instance_config_invalid_for_tier" in src


def test_create_route_threads_pillar_fields_to_service() -> None:
    src = ast.unparse(_function_node(ADMIN_ROUTES_PATH, "create_luciel_instance"))
    for field in (
        "website",
        "personality_preset",
        "personality_axes",
        "business_context",
        "lead_routing",
    ):
        assert field in src, field


# ---------------------------------------------------------------------
# PATCH route — merged-row validation + cross-field axes rule.
# ---------------------------------------------------------------------


def test_update_route_validates_merged_pillars() -> None:
    src = ast.unparse(_function_node(ADMIN_ROUTES_PATH, "update_luciel_instance"))
    assert "validate_pillars_for_tier" in src
    assert "status_code=422" in src
    assert "instance_config_invalid_for_tier" in src


def test_update_route_resolves_effective_values_for_partial_patch() -> None:
    src = ast.unparse(_function_node(ADMIN_ROUTES_PATH, "update_luciel_instance"))
    # Effective merge: stored value when the PATCH omits the field.
    assert "eff_preset" in src
    assert "eff_axes" in src
    assert "eff_business_context" in src
    assert "eff_lead_routing" in src


def test_update_route_cross_validates_custom_axes() -> None:
    src = ast.unparse(_function_node(ADMIN_ROUTES_PATH, "update_luciel_instance"))
    assert "validate_custom_axes" in src
    assert "PRESET_CUSTOM" in src


# ---------------------------------------------------------------------
# admin.py imports the shared validators.
# ---------------------------------------------------------------------


def test_admin_imports_pillar_validators() -> None:
    src = _read(ADMIN_ROUTES_PATH)
    assert "from app.policy.instance_config import validate_pillars_for_tier" in src


# ---------------------------------------------------------------------
# Service + repository thread the pillar fields.
# ---------------------------------------------------------------------


def test_service_create_instance_accepts_pillar_kwargs() -> None:
    node = _function_node(SERVICE_PATH, "create_instance")
    arg_names = {a.arg for a in node.args.args} | {
        a.arg for a in node.args.kwonlyargs
    }
    for field in (
        "website",
        "personality_preset",
        "personality_axes",
        "business_context",
        "lead_routing",
    ):
        assert field in arg_names, field


def test_repository_updatable_fields_include_pillars() -> None:
    src = _read(REPO_PATH)
    start = src.index("_UPDATABLE_FIELDS")
    block = src[start : start + 900]
    for field in (
        "website",
        "personality_preset",
        "personality_axes",
        "business_context",
        "lead_routing",
        "escalation_config",
    ):
        assert f'"{field}"' in block, field


# ---------------------------------------------------------------------
# Model carries the pillar columns; system_prompt_additions is removed.
# ---------------------------------------------------------------------


def test_model_carries_pillar_columns() -> None:
    src = _read(MODEL_PATH)
    for col in (
        "website",
        "personality_preset",
        "personality_axes",
        "business_context",
        "lead_routing",
        "escalation_config",
    ):
        assert col in src, col


def test_model_drops_system_prompt_additions() -> None:
    # Arc 15 doctrine cleanup — the free-text raw-prompt column is GONE
    # from the model end-to-end (Vision §3.5 / Architecture §3.5.1
    # "never raw prompt authoring"). The structured pillars replace it.
    src = _read(MODEL_PATH)
    assert "system_prompt_additions" not in src
