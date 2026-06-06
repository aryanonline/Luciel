"""Unit 12 — matching-logic test for the doctrine doc-sync CI gate.

The gate (`.github/scripts/doctrine_sync_gate.py`) enforces §5.9.3: a PR
that changes a doctrine-anchored path must also update the in-repo
doctrine changelog (the proxy for "architecture doc updated"). This test
drives the gate's pure decision function with synthetic changed-file
lists + a synthetic anchor map and asserts pass/fail, with no git or
filesystem dependency.

It also sanity-checks that the real DOCTRINE_ANCHORS.toml at the repo root
parses and that the gate's view of it is non-empty (the moved Unit 12
anchors are present), so the gate can never silently no-op because the map
went unreadable.
"""
from __future__ import annotations

import importlib.util
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE_PATH = REPO_ROOT / ".github" / "scripts" / "doctrine_sync_gate.py"
ANCHORS_PATH = REPO_ROOT / "DOCTRINE_ANCHORS.toml"


def _load_gate_module():
    spec = importlib.util.spec_from_file_location("doctrine_sync_gate", GATE_PATH)
    assert spec and spec.loader, f"cannot load gate at {GATE_PATH}"
    mod = importlib.util.module_from_spec(spec)
    # Register before exec so dataclass type-resolution can find the module.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


gate = _load_gate_module()


# A small synthetic anchor map exercising a directory anchor, a file
# anchor, a multi-path anchor, a NO-MODULE-YET (no paths), and an
# exception status (still live paths).
FAKE_ANCHORS = {
    "anchor": [
        {"id": "lifecycle", "status": "MATCHES-DOC", "paths": ["app/lifecycle/"]},
        {"id": "handoff", "status": "MATCHES-DOC", "paths": ["app/runtime/handoff.py"]},
        {
            "id": "billing_metering",
            "status": "MATCHES-DOC",
            "paths": ["app/billing/metering.py", "app/billing/overage.py"],
        },
        {"id": "analytics", "status": "NO-MODULE-YET", "paths": []},
        {"id": "migrations", "status": "CONFIG-BOUND-EXCEPTION", "paths": ["alembic/"]},
    ]
}


def test_unrelated_change_does_not_trigger():
    res = gate.evaluate(["README.md", "app/core/config.py"], FAKE_ANCHORS)
    assert res.ok is True
    assert res.triggered is False
    assert res.matched_anchors == []


def test_dir_anchor_change_without_changelog_fails():
    res = gate.evaluate(["app/lifecycle/state.py"], FAKE_ANCHORS)
    assert res.ok is False
    assert res.triggered is True
    assert "lifecycle" in res.matched_anchors
    assert res.has_doc_proxy is False


def test_dir_anchor_change_with_changelog_passes():
    res = gate.evaluate(
        ["app/lifecycle/state.py", "DOCTRINE_CHANGELOG.md"], FAKE_ANCHORS
    )
    assert res.ok is True
    assert res.triggered is True
    assert "lifecycle" in res.matched_anchors
    assert res.has_doc_proxy is True


def test_file_anchor_exact_match_triggers():
    res = gate.evaluate(["app/runtime/handoff.py"], FAKE_ANCHORS)
    assert res.ok is False
    assert "handoff" in res.matched_anchors


def test_file_anchor_does_not_match_sibling():
    # A different file in the same dir as a FILE anchor must NOT match.
    res = gate.evaluate(["app/runtime/orchestrator.py"], FAKE_ANCHORS)
    assert res.triggered is False
    assert res.ok is True


def test_multipath_anchor_matches_any_member():
    res = gate.evaluate(["app/billing/overage.py"], FAKE_ANCHORS)
    assert res.ok is False
    assert "billing_metering" in res.matched_anchors


def test_no_module_yet_anchor_contributes_no_paths():
    # analytics has no paths; touching an unrelated 'analytics'-named file
    # must not trigger because the anchor contributes nothing.
    res = gate.evaluate(["app/analytics_notes.txt"], FAKE_ANCHORS)
    assert res.triggered is False


def test_config_bound_exception_paths_are_still_guarded():
    res = gate.evaluate(["alembic/versions/some_migration.py"], FAKE_ANCHORS)
    assert res.ok is False
    assert "migrations" in res.matched_anchors


def test_editing_the_anchors_map_alone_does_not_trigger():
    # Touching DOCTRINE_ANCHORS.toml is remediation, not a trigger.
    res = gate.evaluate(["DOCTRINE_ANCHORS.toml"], FAKE_ANCHORS)
    assert res.triggered is False
    assert res.ok is True


def test_changelog_only_change_passes_trivially():
    res = gate.evaluate(["DOCTRINE_CHANGELOG.md"], FAKE_ANCHORS)
    assert res.ok is True
    assert res.triggered is False


def test_leading_dot_slash_is_normalized():
    res = gate.evaluate(["./app/lifecycle/state.py"], FAKE_ANCHORS)
    assert res.ok is False
    assert "lifecycle" in res.matched_anchors


# --- Real-map sanity: the committed DOCTRINE_ANCHORS.toml is usable. ---


def test_real_anchors_map_parses():
    assert ANCHORS_PATH.is_file(), "DOCTRINE_ANCHORS.toml must exist at repo root"
    with ANCHORS_PATH.open("rb") as fh:
        doc = tomllib.load(fh)
    assert "anchor" in doc and doc["anchor"], "map must declare [[anchor]] entries"


def test_real_map_yields_live_paths_for_moved_anchors():
    with ANCHORS_PATH.open("rb") as fh:
        doc = tomllib.load(fh)
    id_to_paths = gate.anchored_paths(doc)
    # A representative set of Unit 12 moved anchors must be present with paths.
    for anchor_id in ("lifecycle", "auth_access", "billing_metering", "connections"):
        assert anchor_id in id_to_paths, f"{anchor_id} missing from anchored paths"
        assert id_to_paths[anchor_id], f"{anchor_id} has no governed paths"


def test_real_map_change_under_moved_path_is_caught():
    with ANCHORS_PATH.open("rb") as fh:
        doc = tomllib.load(fh)
    res = gate.evaluate(["app/auth/access.py"], doc)
    assert res.ok is False
    assert "auth_access" in res.matched_anchors


def test_no_module_yet_anchor_has_no_paths_in_real_map():
    with ANCHORS_PATH.open("rb") as fh:
        doc = tomllib.load(fh)
    id_to_paths = gate.anchored_paths(doc)
    assert "analytics" not in id_to_paths
