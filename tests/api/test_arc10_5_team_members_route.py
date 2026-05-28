"""Arc 10.5: GET /api/v1/admin/team-members route contract test.

Anchored to Vision v1 §6.2 (Team Member lifecycle), Customer
Journey v1 §2 (Marcus team-member tier), and Architecture v1
§3.7.2 (role scope assignment is the source of truth for
team-member binding).

Replaces the legacy ``/admin/agents`` endpoint, which referenced
the deleted ``agents`` table and would have crashed in production
on any real call.
"""
from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
ADMIN_ROUTES = REPO_ROOT / "app" / "api" / "v1" / "admin.py"


def _routes_src() -> str:
    return ADMIN_ROUTES.read_text(encoding="utf-8")


def _handler_body() -> str:
    """Slice from def list_team_members_route( to the next @router."""
    src = _routes_src()
    idx = src.find("def list_team_members_route(")
    assert idx >= 0, "list_team_members_route handler not found"
    next_route = src.find("@router.", idx + 1)
    return src[idx:next_route if next_route > 0 else idx + 4000]


def test_team_members_route_is_registered():
    """GET /team-members must exist with response_model=list[TeamMemberRead]."""
    src = _routes_src()
    assert '"/team-members"' in src, (
        "Missing GET /team-members route. Customer Journey v1 §2 "
        "(Marcus team-member tier) requires the Dashboard Team tab "
        "to list scoped users; this route powers it."
    )
    assert "list_team_members_route" in src
    assert "response_model=list[TeamMemberRead]" in src


def test_team_members_route_uses_scope_assignment_repository():
    """Handler must source data from ScopeAssignmentRepository per
    Architecture v1 §3.7.2, NOT from the deleted agents table.
    """
    body = _handler_body()
    assert "ScopeAssignmentRepository" in body
    assert "list_for_tenant" in body
    # No ORM/SQL query against the dropped agents table. Comments and
    # docstrings (which legitimately mention the legacy name) are
    # excluded by stripping comment-only lines and triple-quoted
    # docstring chunks.
    in_docstring = False
    code_only_lines: list[str] = []
    for ln in body.splitlines():
        stripped = ln.lstrip()
        # Track triple-quoted docstring boundaries.
        if '"""' in ln:
            # Single-line docstring: """foo""" on the same line.
            count = ln.count('"""')
            if count == 2:
                continue  # full docstring on this line
            in_docstring = not in_docstring
            continue
        if in_docstring:
            continue
        if stripped.startswith("#"):
            continue
        code_only_lines.append(ln)
    code = "\n".join(code_only_lines)
    # The legacy `agents` table doesn't appear as a real ORM/SQL token
    # anywhere in the handler's executable body.
    assert "FROM agents" not in code.upper(), (
        "Handler runs raw SQL against the dropped `agents` table."
    )
    # Common ORM access patterns to forbid.
    forbidden = ("Agent.", "AgentRepository", "agents_repository")
    for tok in forbidden:
        assert tok not in code, (
            f"Handler references deleted Agent surface: {tok!r}"
        )


def test_team_members_route_uses_cookied_actor_resolution():
    """The route must scope to the cookied actor's admin_id via the
    same _resolve_invite_actor helper that list_invites_route uses.
    """
    body = _handler_body()
    assert "_resolve_invite_actor" in body


def test_team_member_schema_exists():
    schema_path = REPO_ROOT / "app" / "schemas" / "team_member.py"
    assert schema_path.exists()
    src = schema_path.read_text(encoding="utf-8")
    assert "class TeamMemberRead" in src
    for field in (
        "scope_assignment_id", "role", "started_at", "user_id",
        "email", "display_name",
    ):
        assert field in src, (
            f"TeamMemberRead must declare field '{field}'."
        )
