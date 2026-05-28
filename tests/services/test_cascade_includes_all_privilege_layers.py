"""Executable mirror of the AdminService.deactivate_tenant_with_cascade
docstring's canonical layer enumeration.

The docstring at app/services/admin_service.py::deactivate_tenant_with_cascade
declares the canonical cascade order. Per the four-surface-symmetry
doctrine cited in that same docstring, any cascade-layer extension
MUST update four surfaces in the same diff:

  (a) the docstring enumeration,
  (b) the in-body ``# --- N. <table>`` comment,
  (c) CANONICAL_RECAP §14 cascade-layer matrix,
  (d) THIS FILE (the executable mirror).

Surface (d) was missing as of Arc 10 -- the docstring claimed this
file existed but no such file shipped. Arc 10 Gap 7 closure adds it.
See D-arc10-cascade-mirror-test-missing-2026-05-27.

The tests below assert structural pins. They are not full-DB
integration tests (those live in the in-cluster E2E harness). The
goal is: any future edit to the cascade body that removes a layer,
re-numbers, or drops the in-body marker comment without updating
the docstring + this mirror will fail this test.
"""
from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
ADMIN_SERVICE_PATH = REPO_ROOT / "app" / "services" / "admin_service.py"


# Canonical cascade order per Vision/Architecture §3.6 + the in-source
# docstring at AdminService.deactivate_tenant_with_cascade. Pinning as a
# tuple here so this file is the authoritative executable mirror.
#
# Arc 10 Gap 7 prune: layers 6 (`agents`) and 7 (`agent_configs`) were
# REMOVED because their underlying tables were dropped at
# arc5_c_admin_instance_subtractive (Arc 5 Path A). They never executed
# successfully in production; the route that called them crashed at
# import time with ModuleNotFoundError on the deleted AgentRepository.
CANONICAL_CASCADE_LAYERS: tuple[tuple[int, str], ...] = (
    (1, "conversations"),
    (2, "identity_claims"),
    (3, "memory_items"),
    (4, "api_keys"),
    (5, "instances"),
    (6, "scope_assignments"),
    (7, "user_invites"),
    (8, "sessions"),
    (9, "synthetic_orphan_users"),
    (10, "tenant_config"),
)


def _service_src() -> str:
    return ADMIN_SERVICE_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------
# Surface (a) -- docstring enumeration.
# ---------------------------------------------------------------------

def test_docstring_enumerates_all_canonical_layers_in_order():
    """The docstring must list every canonical layer in order."""
    src = _service_src()
    # The docstring uses a numbered list "  1.  conversations..." etc.
    # We find each layer name and assert it appears AFTER all lower-
    # numbered layers (positional order check).
    positions: list[int] = []
    for n, name in CANONICAL_CASCADE_LAYERS:
        # Match "  N.  <name>" or "  N. <name>" patterns. Anchor with
        # the number to avoid catching prose mentions.
        pattern = re.compile(rf"^\s+{n}\.\s+{re.escape(name)}\b", re.MULTILINE)
        match = pattern.search(src)
        assert match is not None, (
            f"docstring must list layer {n}. {name} -- "
            f"see CANONICAL_CASCADE_LAYERS in this file."
        )
        positions.append(match.start())

    for i in range(1, len(positions)):
        assert positions[i] > positions[i - 1], (
            f"docstring layer order is wrong: layer "
            f"{CANONICAL_CASCADE_LAYERS[i][0]} ({CANONICAL_CASCADE_LAYERS[i][1]}) "
            f"appears before layer "
            f"{CANONICAL_CASCADE_LAYERS[i-1][0]} ({CANONICAL_CASCADE_LAYERS[i-1][1]})."
        )


# ---------------------------------------------------------------------
# Surface (b) -- in-body marker comments.
# ---------------------------------------------------------------------

def test_in_body_marker_comments_exist_for_every_layer():
    """Each cascade layer must carry a `# --- N. <name>` comment.

    The marker comments make the cascade body navigable (jump to the
    layer that touched a given table during incident response) and
    make the layer-number-to-table mapping locally visible.
    """
    src = _service_src()
    for n, name in CANONICAL_CASCADE_LAYERS:
        # Allow alphanumeric/underscore name fragment in the marker so
        # "luciel_instances cascade (all scope levels)" matches.
        pattern = re.compile(rf"#\s*---\s*{n}\.\s+{re.escape(name)}\b")
        assert pattern.search(src) is not None, (
            f"missing in-body marker comment `# --- {n}. {name}` -- "
            f"every cascade layer must carry its layer-number marker."
        )


def test_no_dead_layer_marker_comments_remain():
    """The pre-Arc-10 cascade had `# --- 6. agents` and `# --- 7.
    agent_configs` marker comments. Those layers were removed in the
    Gap 7 prune. The marker comments must be removed from the
    in-body cascade too (NOT just the docstring) -- a dangling marker
    would mislead an incident responder.
    """
    src = _service_src()
    # The Gap 7 prune replaces the layer-6/7 markers with a "# --- N.
    # <table> cascade -- REMOVED" doc-comment that is allowed (it
    # records the historical fact). What is NOT allowed is the OLD
    # marker comment shape that looked like the layer was still live:
    forbidden_shapes = (
        r"#\s*---\s*6\.\s+agents\s*\(new-table\)\s+cascade\s*-+\s*$",
        r"#\s*---\s*7\.\s+agent_configs\s*\(legacy\)\s+cascade\s*\(inline\)\s*-+\s*$",
    )
    for pat in forbidden_shapes:
        m = re.search(pat, src, re.MULTILINE)
        assert m is None, (
            f"dead pre-Arc-10 cascade marker still present: {m.group(0) if m else pat}. "
            f"The Gap 7 prune removed those layers; their marker comments "
            f"must not remain in a live-looking shape."
        )


# ---------------------------------------------------------------------
# Surface (c) is the architecture / Vision text -- intentionally NOT
# pinned here (the documents live in the Space, not this repo).
# Surface (d) is THIS file. Self-evident -- no test needed.
# ---------------------------------------------------------------------


# ---------------------------------------------------------------------
# Architecture-alignment guardrails.
# ---------------------------------------------------------------------

def test_cascade_signature_keeps_agent_repo_optional_for_backcompat():
    """The deactivate_tenant_with_cascade method signature must keep
    agent_repo as an optional kwarg (default None).

    Reason: removing the kwarg outright would break any caller that
    still passes it positionally or as a kwarg pending its own
    update. Default-None keeps the surface backward-compatible while
    the body ignores the value (the agents layers are gone).
    """
    src = _service_src()
    sig_pattern = re.compile(
        r"def deactivate_tenant_with_cascade\("
        r"[\s\S]+?"
        r"agent_repo\s*=\s*None\s*,",
    )
    assert sig_pattern.search(src) is not None, (
        "deactivate_tenant_with_cascade must accept agent_repo=None for "
        "backward compatibility with existing call sites. See "
        "D-arc10-close-path-imports-deleted-agent-repository-2026-05-27."
    )


def test_cascade_does_not_call_deleted_agent_repository():
    """The cascade body must not call agent_repo.<anything>(...).

    AgentRepository was deleted at Arc 5 Path A; any call site is a
    structural production bug (it would crash at request time with
    ModuleNotFoundError or AttributeError).
    """
    src = _service_src()
    # Heuristic: look for `agent_repo.` outside a comment. We allow
    # `agent_repo=None` (kwarg defaults) and `# agent_repo` (comments).
    for line in src.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        # Match agent_repo.<method>( -- a real method call
        if re.search(r"\bagent_repo\.\w+\s*\(", line):
            raise AssertionError(
                f"cascade body must not call agent_repo.<method>(...): "
                f"AgentRepository was deleted at Arc 5 Path A. "
                f"Offending line: {line.strip()!r}"
            )


def test_cascade_does_not_query_dropped_agent_configs_table():
    """The cascade must not query the `agent_configs` table.

    `agent_configs` was DROPPED at arc5_c_admin_instance_subtractive
    (legacy_tbl list). Any SQL or ORM query against it is dead code
    that would error out at runtime if reached.

    This test is scoped to the cascade method only -- other methods
    in admin_service.py (create_agent_config, get_agent_config,
    list_agent_configs) remain as orphaned CRUD until their own
    cleanup pass.
    """
    src = _service_src()
    # Find the cascade method body bounds.
    start = src.find("def deactivate_tenant_with_cascade(")
    assert start >= 0, "cascade method must exist"
    # Find the next top-level def after it. Use a coarse heuristic:
    # next `    def ` (4-space indent for a class method).
    end_match = re.search(r"\n    def \w+", src[start + 1 :])
    end = (start + 1 + end_match.start()) if end_match else len(src)
    body = src[start:end]
    # AgentConfig.<col> or "agent_configs" in a query context.
    forbidden = (
        r"\bAgentConfig\.\w+",
        r'"agent_configs"',
        r"'agent_configs'",
    )
    for pat in forbidden:
        m = re.search(pat, body)
        if m:
            # Allow if inside a comment line.
            line_start = body.rfind("\n", 0, m.start()) + 1
            line_end = body.find("\n", m.end())
            line = body[line_start:line_end if line_end > 0 else len(body)]
            if line.lstrip().startswith("#"):
                continue
            raise AssertionError(
                f"cascade body references dropped agent_configs surface: "
                f"{m.group(0)!r} on line {line.strip()!r}"
            )


# ---------------------------------------------------------------------
# Arc 10 Gap 7 closure: the instances cascade layer must call through
# InstanceService.cascade_on_admin_deactivate, NOT bypass to the
# repository's old (renamed-away) deactivate_all_for_tenant method.
#
# Anchored to Architecture v1 \u00a73.6.2 step 3 ("All instances deactivated
# cascade per 3.6.1") -- the cascade invocation is what the doctrine
# names; mismatched method names are a production bug, not a stylistic
# choice. Surfaced live when ClosureService.initiate_closure crashed
# with AttributeError on the first close attempt against the local
# Postgres baseline.
# ---------------------------------------------------------------------

def test_cascade_calls_instance_service_cascade_hook():
    """Layer 5 must call InstanceService.cascade_on_admin_deactivate.

    Two forbidden shapes:
      1. ``.repo.deactivate_all_for_tenant(...)`` (renamed away in
         Arc 9.2 PR #101 \u2014 tenant_id collapsed to admin_id).
      2. ``.repo.deactivate_all_for_admin(...)`` directly (bypasses
         the service-layer audit emission policy).

    The only correct shape is
    ``luciel_instance_service.cascade_on_admin_deactivate(...)``.
    """
    src = _service_src()
    # Must call the cascade hook.
    assert (
        "luciel_instance_service.cascade_on_admin_deactivate(" in src
    ), (
        "Layer 5 must invoke "
        "luciel_instance_service.cascade_on_admin_deactivate(...) per "
        "Architecture v1 \u00a73.6.2 step 3."
    )
    # Must NOT call the renamed-away repo method on the instance
    # repository specifically. We scope the check to the cascade body
    # so other methods on the module that legitimately retain a
    # _for_tenant name (e.g. bulk_soft_deactivate_memory_items_for_
    # tenant) don't trip the assertion.
    start = src.find("def deactivate_tenant_with_cascade(")
    assert start >= 0
    next_def = src.find("\n    def ", start + 1)
    cascade_body = src[start:next_def if next_def > 0 else len(src)]
    assert ".repo.deactivate_all_for_tenant" not in cascade_body, (
        "Cascade body must not reference repo.deactivate_all_for_tenant -- "
        "that method was renamed to deactivate_all_for_admin in Arc 9.2 "
        "PR #101 (tenant_id -> admin_id collapse)."
    )
    # Must NOT bypass the service to the repo for this layer.
    assert "luciel_instance_service.repo.deactivate_all_for_admin" not in src, (
        "Cascade must not bypass InstanceService to call the repo "
        "directly; the service hook emits the per-instance audit rows."
    )
