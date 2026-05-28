"""Arc 11 Closeout PR-A — widget gating against inactive instances.

Customer Journey §4.5 Phase 8 mandates that a Paused instance's widget
"renders an empty <div>" (no error, no broken UI). The implementation
surface is the ``widget_chat_stream`` route in
``app/api/v1/chat_widget.py``: when the resolved Instance is not in the
``active`` state, the route returns HTTP 204 with the
``X-Luciel-Instance-Status`` header carrying the canonical state value
so the embed JS knows what happened.

These are AST + text assertions on the shipped source — same
convention as test_instance_lifecycle_arc11_closeout.py. Live SSE
integration tests live in tests/db/ (live-DB gated).
"""
from __future__ import annotations

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
CHAT_WIDGET_PATH = REPO_ROOT / "app" / "api" / "v1" / "chat_widget.py"


def _read() -> str:
    return CHAT_WIDGET_PATH.read_text(encoding="utf-8")


def _parse() -> ast.Module:
    return ast.parse(_read())


def _widget_chat_stream_node() -> ast.FunctionDef:
    tree = _parse()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "widget_chat_stream":
            return node
    raise AssertionError("widget_chat_stream not found in chat_widget.py")


# ---------------------------------------------------------------------
# Status-gating import + reference.
# ---------------------------------------------------------------------


def test_widget_imports_instance_status_enum():
    """The route must import InstanceStatus to compare against the
    canonical 'active' member."""
    src = _read()
    assert (
        "from app.models.instance_status import InstanceStatus" in src
    ), "chat_widget.py must import InstanceStatus."


def test_widget_imports_instance_model():
    src = _read()
    assert "from app.models.instance import Instance" in src, (
        "chat_widget.py must import the Instance ORM model to resolve "
        "the row from the embed key's luciel_instance_id."
    )


# ---------------------------------------------------------------------
# Route returns 204 with X-Luciel-Instance-Status when not active.
# ---------------------------------------------------------------------


def test_widget_returns_204_when_instance_not_active():
    src = ast.unparse(_widget_chat_stream_node())
    assert "status_code=204" in src, (
        "widget_chat_stream must return HTTP 204 No Content when the "
        "instance is not active, per Customer Journey §4.5 Phase 8 "
        "('renders an empty <div>')."
    )


def test_widget_sets_instance_status_header_on_inactive():
    src = ast.unparse(_widget_chat_stream_node())
    assert "X-Luciel-Instance-Status" in src, (
        "widget_chat_stream must emit the X-Luciel-Instance-Status "
        "header when gating an inactive instance so a developer "
        "inspecting Network tab can see why the widget went silent."
    )


def test_widget_compares_against_instance_status_active():
    """The gate predicate must be ``instance_status != ACTIVE``, not
    the legacy ``not active`` boolean. The boolean is the deprecated
    mirror; instance_status is the source of truth post Arc 11 Closeout."""
    src = ast.unparse(_widget_chat_stream_node())
    assert "InstanceStatus.ACTIVE" in src, (
        "widget gate must compare against InstanceStatus.ACTIVE."
    )


def test_widget_gates_before_session_resolution():
    """The status gate MUST run before lazy session creation so a
    paused or deleted instance never mints a fresh session row.
    Anchoring the gate's relative position keeps this contract honest
    against future edits to the route body.

    We compare line numbers from the SOURCE file (not ast.unparse, which
    strips comments and so makes positional checks unreliable when
    docstring text incidentally matches the marker)."""
    src = _read()
    # Gate marker: the dict key emission inside the Response() call.
    gate_idx = src.index('"X-Luciel-Instance-Status":')
    # Session marker: the actual call site (= session = session_service.create_session(
    # at the lazy-anonymous branch), found with the leading whitespace+`session = `
    # so docstring mentions don't match.
    session_idx = src.index("        session = session_service.create_session(")
    assert gate_idx < session_idx, (
        "widget_chat_stream must gate on instance_status BEFORE lazy "
        "session creation; otherwise a paused instance still mints "
        "session rows for every widget hit."
    )


def test_widget_gate_handles_missing_instance_row():
    """Defensive: if the embed key resolves to an instance_id that has
    no surviving row (post-retention purge), the gate must still
    return 204 rather than fall through to a 500. The retention
    worker removes the row only after the 30-day grace -- but during
    the brief window between row delete and embed-key revocation, a
    cached widget bundle could still try to hit the endpoint."""
    src = ast.unparse(_widget_chat_stream_node())
    # Either an explicit `is None` check or an `if ... or ...` chain.
    assert "is None" in src or "instance_row is None" in src, (
        "widget gate must defensively handle a missing instance row."
    )
