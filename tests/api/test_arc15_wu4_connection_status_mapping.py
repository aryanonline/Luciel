"""Arc 15 WU4 — connection_status chip mapping (behavioural).

Exercises the pure mapping ``_connection_status_for`` and the
``_serialize_tool_view`` threading directly (no DB / TestClient). Pins
the three-state surface from spec §92-96:

  no row / unconfigured → "action_needed"
  connected             → "connected"
  error / expired       → "reconnect_needed"
  requires_connection==None → connection_status is None (no chip)
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("OPENAI_API_KEY", "dummy")
os.environ.setdefault("ANTHROPIC_API_KEY", "stub")

import pytest

from app.api.v1.admin_tools import _connection_status_for, _serialize_tool_view


@pytest.mark.parametrize(
    "requires_connection,live_status,expected",
    [
        ("calendar", None, "action_needed"),
        ("calendar", "unconfigured", "action_needed"),
        ("record_source", "connected", "connected"),
        ("record_source", "error", "reconnect_needed"),
        ("record_source", "expired", "reconnect_needed"),
        (None, None, None),
        (None, "connected", None),  # None requirement → never a chip.
    ],
)
def test_connection_status_mapping(requires_connection, live_status, expected):
    assert (
        _connection_status_for(
            requires_connection=requires_connection, live_status=live_status
        )
        == expected
    )


def _fake_tool(*, tool_id, requires_connection):
    class _T:
        requires_tier = ("free", "pro", "enterprise")
        requires_channels = frozenset()
        execution_mode = "in_process"
        display_name = "T"
        description = "d"

    t = _T()
    t.tool_id = tool_id
    t.requires_connection = requires_connection
    return t


def test_serialize_threads_status_from_map():
    tool = _fake_tool(tool_id="lookup_record", requires_connection="record_source")
    view = _serialize_tool_view(
        tool=tool,
        authorization=None,
        admin_tier="pro",
        instance_channels=frozenset(),
        live_status_by_type={"record_source": "connected"},
    )
    assert view.connection_type == "record_source"
    assert view.connection_status == "connected"


def test_serialize_no_connection_tool_has_null_chip():
    tool = _fake_tool(tool_id="schedule_callback", requires_connection=None)
    view = _serialize_tool_view(
        tool=tool,
        authorization=None,
        admin_tier="pro",
        instance_channels=frozenset(),
        live_status_by_type={"record_source": "connected"},
    )
    assert view.connection_type is None
    assert view.connection_status is None


def test_serialize_missing_row_is_action_needed():
    tool = _fake_tool(tool_id="book_appointment", requires_connection="calendar")
    view = _serialize_tool_view(
        tool=tool,
        authorization=None,
        admin_tier="pro",
        instance_channels=frozenset(),
        live_status_by_type={},
    )
    assert view.connection_type == "calendar"
    assert view.connection_status == "action_needed"
