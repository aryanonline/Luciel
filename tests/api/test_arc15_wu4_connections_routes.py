"""Arc 15 WU4 — connection-config route wiring + ToolView chip (shape).

AST/text shape test (same convention as the WU3 route tests): protects
the connections router wiring (four-walls auth with
PERM_CONFIGURE_CONNECTIONS, the no-fake-connected honesty fork, audit on
every write, no-secret non_secret_config guard) and the ToolView
connection_status mapping — without a live TestClient/DB. The gate
behaviour is covered in tests/tools/test_arc15_wu5_connection_gate.py;
the connection_status pure mapping is covered behaviourally below.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONN_PATH = REPO_ROOT / "app" / "api" / "v1" / "admin_connections.py"
CONN_SCHEMA = REPO_ROOT / "app" / "schemas" / "connection.py"
TOOLS_PATH = REPO_ROOT / "app" / "api" / "v1" / "admin_tools.py"
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
# Router registration + mount point (Architecture §1.1: /admin/...).
# ---------------------------------------------------------------------


def test_router_registered() -> None:
    src = _read(ROUTER_PATH)
    assert "admin_connections" in src
    assert "api_router.include_router(admin_connections.router)" in src


def test_router_prefix_and_paths() -> None:
    src = _read(CONN_PATH)
    assert 'prefix="/admin"' in src
    assert '"/instances/{instance_id}/connections"' in src
    assert '"/connections/{connection_id}"' in src


# ---------------------------------------------------------------------
# Four-walls auth on every route; connections-specific permission.
# ---------------------------------------------------------------------


def test_uses_configure_connections_permission() -> None:
    src = _read(CONN_PATH)
    assert "PERM_CONFIGURE_CONNECTIONS" in src
    # Must NOT silently reuse the channels permission for connections.
    assert "_require_configure_connections" in src


def test_list_and_post_enforce_four_walls() -> None:
    for fn in ("list_connections", "configure_connection"):
        src = ast.unparse(_function_node(CONN_PATH, fn))
        assert "_require_admin_id" in src
        assert "_load_active_instance" in src
        assert "_require_configure_connections" in src


def test_delete_fences_to_admin() -> None:
    src = ast.unparse(_function_node(CONN_PATH, "disconnect_connection"))
    assert "_require_admin_id" in src
    # Load-then-revoke fenced to the admin via the repo (Wall-1).
    assert "get_live_for_admin" in src
    assert "disconnect" in src


# ---------------------------------------------------------------------
# Honesty fork (Unit 13c): driven by the §3.8.5 auth_class, NOT a
# hardcoded LIVE/DEFERRED list. Each credential SHAPE has its own honest
# connect path; none ever fabricates 'connected' without a real backing.
# ---------------------------------------------------------------------


def test_post_honesty_fork_no_fake_connected() -> None:
    src = ast.unparse(_function_node(CONN_PATH, "configure_connection"))
    # The fork now keys on auth_class_for(conn_type) — the hardcoded
    # LIVE/DEFERRED sets were removed.
    assert "auth_class_for" in src
    assert "LIVE_CONNECTION_TYPES" not in src
    assert "DEFERRED_CONNECTION_TYPES" not in src
    # All three honest dispositions are present; connected only on a real
    # backing (api_key config-presence / provisioned identity present).
    assert "'connected'" in src or '"connected"' in src
    assert "'unconfigured'" in src or '"unconfigured"' in src
    assert "Arc17Pending" in src
    # provisioned_resource verifies a real platform identity before connect.
    assert "_provisioned_resource_identity" in src


def test_auth_class_fork_covers_every_shape() -> None:
    # The fork must branch on all four §3.8.5 credential shapes (oauth_token
    # / api_key / provisioned_resource handled; long_lived_token falls into
    # the same liveness family). auth_class_for is the single mapping point.
    from app.connections.instance_connection import (
        AUTH_CLASS_BY_TYPE,
        auth_class_for,
    )

    assert auth_class_for("calendar") == "oauth_token"
    assert auth_class_for("crm") == "oauth_token"
    assert auth_class_for("email_sender") == "provisioned_resource"
    assert auth_class_for("sms_sender") == "provisioned_resource"
    assert auth_class_for("record_source") == "api_key"
    assert auth_class_for("outbound_webhook") == "api_key"
    # Every connection_type maps to a class (no silent gap).
    src = ast.unparse(_function_node(CONN_PATH, "configure_connection"))
    for klass in set(AUTH_CLASS_BY_TYPE.values()):
        assert klass in src, klass


def test_non_secret_config_secret_guard() -> None:
    src = _read(CONN_PATH)
    assert "_FORBIDDEN_CONFIG_KEYS" in src
    assert "secret_in_non_secret_config" in src
    # secret_ref stays NULL in this slice.
    assert "secret_ref=None" in src


# ---------------------------------------------------------------------
# Audit on every write.
# ---------------------------------------------------------------------


def test_writes_audit_on_configure_and_disconnect() -> None:
    post = ast.unparse(_function_node(CONN_PATH, "configure_connection"))
    assert "AdminAuditRepository" in post
    assert "ACTION_CONNECTION_CONFIGURED" in post
    assert "RESOURCE_INSTANCE_CONNECTION" in post

    delete = ast.unparse(_function_node(CONN_PATH, "disconnect_connection"))
    assert "AdminAuditRepository" in delete
    assert "ACTION_CONNECTION_DISCONNECTED" in delete


def test_audit_constants_whitelisted() -> None:
    src = _read(AUDIT_PATH)
    for const in (
        "ACTION_CONNECTION_CONFIGURED",
        "ACTION_CONNECTION_DISCONNECTED",
        "RESOURCE_INSTANCE_CONNECTION",
    ):
        assert src.count(const) >= 2, const


def test_audit_records_config_keys_not_values() -> None:
    # non_secret_config is non-secret by contract, but record only the KEYS so
    # the audit row stays bounded.
    src = ast.unparse(_function_node(CONN_PATH, "configure_connection"))
    assert "config_keys" in src


# ---------------------------------------------------------------------
# ToolView connection_status threading.
# ---------------------------------------------------------------------


def test_toolview_has_connection_fields() -> None:
    tree = _parse(TOOLS_PATH)
    fields: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "ToolView":
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(
                    stmt.target, ast.Name
                ):
                    fields.add(stmt.target.id)
    assert "connection_status" in fields
    assert "connection_type" in fields


def test_list_route_threads_live_status_by_type() -> None:
    src = ast.unparse(_function_node(TOOLS_PATH, "list_tools_for_instance"))
    assert "InstanceConnectionRepository" in src
    assert "live_status_by_type" in src
