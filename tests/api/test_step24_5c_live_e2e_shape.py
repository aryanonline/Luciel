"""
Step 24.5c — Live e2e harness shape contract tests.

Step 24.5c sub-branch 5, Deliverable B (harness-shape pin).

Purpose
=======

The Step 24.5c live e2e harness (tests/e2e/step_24_5c_live_e2e.py) is
a runnable script — NOT a pytest module — that exercises the shipped
cross-channel-identity primitives against a real Postgres dev DB and
asserts the v1 success criterion from CANONICAL_RECAP §12 (row
"24.5c") and DRIFTS.md D-step-24-5c-impl-backlog-2026-05-11.

Because it requires Postgres, the script CANNOT run in the
backend-free CI job. That's a feature, not a bug — it mirrors Step
30c's `tests/e2e/step_30c_live_e2e.py` pattern: the live harness is
run locally against the dev DB on the doc-truthing commit, and the
backend-free CI job pins the harness's *shape* so accidental deletion
or signature drift fails CI loudly.

Invariants this module pins:

  * The script file exists at the canonical path.
  * The script's module docstring names the success criterion it
    exercises (so a future grep on the recap claim still lands here).
  * The script imports the four load-bearing runtime surfaces it
    must exercise: IdentityResolver, SessionService, SessionRepository,
    CrossSessionRetriever. If any of those four is removed from the
    imports, the harness is no longer end-to-end and CI must fail.
  * The script refuses to run on non-Postgres DATABASE_URL (the
    native enum + uuid columns Step 24.5c installed cannot be
    represented by sqlite without lossy casts). This is checked
    syntactically: the script must contain an early DATABASE_URL
    guard with sys.exit on non-postgres.
  * The script exits 0 on success and non-zero on any failed claim
    (so a CI runner that invokes it against a dev DB can gate on the
    exit code).

Style
=====

AST + filesystem assertions only. No live HTTP, no subprocess, no
docker, no DB. Backend-free, lives in the existing
backend-free pytest job in .github/workflows/ci.yml.
"""

from __future__ import annotations

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "tests" / "e2e" / "step_24_5c_live_e2e.py"


# ---------------------------------------------------------------------------
# Filesystem-level invariants
# ---------------------------------------------------------------------------


def test_live_e2e_script_exists_at_canonical_path():
    """The harness lives at tests/e2e/step_24_5c_live_e2e.py — same
    parent directory and same naming convention as Step 30c's
    precedent (tests/e2e/step_30c_live_e2e.py). Moving the file
    breaks the doc-truthing cross-reference."""
    assert SCRIPT_PATH.is_file(), (
        f"Expected Step 24.5c live e2e harness at {SCRIPT_PATH}; "
        f"not found. Did the file move or get deleted?"
    )


def test_live_e2e_script_is_not_empty():
    """The harness must be a real script, not a placeholder stub."""
    contents = SCRIPT_PATH.read_text()
    # 472 lines for Step 30c's precedent. Step 24.5c's harness is in
    # the same ballpark. 200 is a generous floor.
    assert len(contents.splitlines()) >= 200, (
        "Step 24.5c live e2e harness is suspiciously short; "
        "the precedent is ~470 lines and ours covers 6 claim groups."
    )


# ---------------------------------------------------------------------------
# AST-level invariants
# ---------------------------------------------------------------------------


def _parse_script() -> ast.Module:
    return ast.parse(SCRIPT_PATH.read_text(), filename=str(SCRIPT_PATH))


def test_live_e2e_module_docstring_names_success_criterion():
    """The module docstring is the first place a future contributor
    lands. It must name the success criterion it exercises so a grep
    on 'success criterion' from CANONICAL_RECAP §12 lands here."""
    tree = _parse_script()
    docstring = ast.get_docstring(tree)
    assert docstring is not None, "Module docstring missing on live e2e harness."

    required_markers = [
        "Step 24.5c",
        "CANONICAL_RECAP",
        "24.5c",
        "v1 success criterion",
        "conversation_id",
        "identity_claims",
        "CrossSessionRetriever",
    ]
    for marker in required_markers:
        assert marker in docstring, (
            f"Live e2e docstring missing required marker {marker!r}. "
            f"This marker is load-bearing for the doc-truthing "
            f"cross-reference from CANONICAL_RECAP §12 / DRIFTS "
            f"D-step-24-5c-impl-backlog-2026-05-11."
        )


def test_live_e2e_imports_all_four_runtime_surfaces():
    """The harness MUST exercise the four load-bearing runtime
    surfaces from sub-branches 2, 3, and 4. If any of these imports
    is removed, the harness has silently stopped being end-to-end."""
    tree = _parse_script()

    # Collect every (module, name) pair from `from X import Y` blocks.
    imported: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                imported.add((module, alias.name))

    required = [
        # sub-branch 3 — the §3.3 step 4 resolver hook
        ("app.identity.resolver", "IdentityResolver"),
        # sub-branch 2 — the runtime retrieval surface
        ("app.memory.cross_session_retriever", "CrossSessionRetriever"),
        # sub-branch 4 — the adapter-facing service wiring
        ("app.services.session_service", "SessionService"),
        ("app.repositories.session_repository", "SessionRepository"),
    ]

    for module, name in required:
        assert (module, name) in imported, (
            f"Live e2e harness must import {name} from {module}. "
            f"Without this import the harness is no longer end-to-end."
        )


def test_live_e2e_imports_claim_type_and_models():
    """The harness must exercise the new ORM models too — Conversation,
    IdentityClaim, ClaimType — so the database-state-truthing claims
    (one claim, one conversation) are queried at the model layer, not
    via raw SQL."""
    tree = _parse_script()

    imported: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                imported.add((module, alias.name))

    assert ("app.models.conversation", "Conversation") in imported, (
        "Live e2e must import the Conversation model — "
        "CLAIM 4 queries the conversations table at the ORM layer."
    )
    assert ("app.models.identity_claim", "IdentityClaim") in imported, (
        "Live e2e must import the IdentityClaim model — "
        "CLAIM 4 queries the identity_claims table at the ORM layer."
    )
    assert ("app.models.identity_claim", "ClaimType") in imported, (
        "Live e2e must import the ClaimType enum — every "
        "claim assertion passes a ClaimType.EMAIL value."
    )


def test_live_e2e_refuses_non_postgres_database_url():
    """The Step 24.5c schema uses a native Postgres enum
    (identity_claim_type) and uuid columns. The harness must refuse
    to run on a non-Postgres DATABASE_URL with a clear error and a
    non-zero exit — silently coercing sqlite would produce
    false-positive PASSes."""
    source = SCRIPT_PATH.read_text()

    # The guard is documented prose + a literal check. Pin both.
    assert 'DATABASE_URL' in source, (
        "Live e2e must reference DATABASE_URL (it picks up the dev DB "
        "via the same env-var pattern as Step 30c's precedent)."
    )
    assert 'startswith("postgresql")' in source or "startswith('postgresql')" in source, (
        "Live e2e must guard against non-Postgres DATABASE_URL "
        "(the Step 24.5c schema uses native enum + uuid columns). "
        "Expected an explicit DATABASE_URL.startswith('postgresql') check."
    )
    assert "sys.exit(2)" in source, (
        "Live e2e must exit with code 2 on a non-Postgres DATABASE_URL "
        "(so a CI runner can distinguish 'env not set up' from "
        "'a claim failed' at exit code 1)."
    )


def test_live_e2e_uses_summary_exit_code_pattern():
    """Same exit-code contract as Step 30c precedent: 0 on all
    claims pass, 1 on any failed claim. A CI runner gates on this."""
    source = SCRIPT_PATH.read_text()
    assert "sys.exit(0)" in source, (
        "Live e2e must exit 0 when every claim passes "
        "(Step 30c precedent — CI gates on this)."
    )
    assert "sys.exit(1)" in source, (
        "Live e2e must exit 1 when any claim fails "
        "(Step 30c precedent — distinct from exit 2 'env not ready')."
    )


def test_live_e2e_exercises_both_channels_named_in_success_criterion():
    """The v1 success criterion names two specific channels: the
    embeddable chat widget and the programmatic API. The harness
    must literally exercise both channel labels — not aliases."""
    source = SCRIPT_PATH.read_text()
    assert '"web"' in source or "'web'" in source, (
        "Live e2e must exercise channel='web' — the widget channel "
        "label named in the v1 success criterion."
    )
    assert '"programmatic_api"' in source or "'programmatic_api'" in source, (
        "Live e2e must exercise channel='programmatic_api' — the "
        "second channel named in the v1 success criterion."
    )


def test_live_e2e_calls_the_step_3_3_step_4_hook_by_name():
    """The §3.3 step 4 hook lives on SessionService as
    create_session_with_identity(). The harness must call it
    by name — not bypass it by hand-rolling the resolver call —
    so a future refactor that renames the hook is caught here."""
    source = SCRIPT_PATH.read_text()
    assert "create_session_with_identity" in source, (
        "Live e2e must call SessionService.create_session_with_identity() "
        "— the §3.3 step 4 hook. Hand-rolling the resolver call "
        "bypasses the wiring sub-branch 4 was built to prove."
    )


def test_live_e2e_exercises_exclude_session_id_kwarg():
    """The CrossSessionRetriever.retrieve()'s exclude_session_id
    kwarg is the load-bearing 'don't surface my own session's turns
    back to me' guarantee. The harness MUST exercise it so a future
    drop of the kwarg is caught end-to-end, not just at the
    retriever's own contract test."""
    source = SCRIPT_PATH.read_text()
    assert "exclude_session_id=" in source, (
        "Live e2e must exercise the exclude_session_id kwarg — "
        "the 'don't surface my own turns back to me' guarantee."
    )
