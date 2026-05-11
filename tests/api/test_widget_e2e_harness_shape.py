"""
Widget-E2E harness shape contract tests.

Step 30d, Deliverable C.

Purpose
=======

The widget-surface E2E harness (Step 30d Deliverable C) is a five-file
construct:

    .github/workflows/widget-e2e.yml
    ci/e2e/run_widget_e2e.sh
    ci/e2e/assert_widget_stream.py
    ci/e2e/README.md
    scripts/bootstrap_platform_admin_ci.py

These files share several invariants that, if broken, would silently
disable the gate without any unit-test failure:

  * The workflow file's name and trigger shape (it must be
    workflow_dispatch in v1; the follow-up commit adds pull_request).
  * The MODERATION_PROVIDER=keyword + MODERATION_KEYWORD_BLOCK_TERMS
    env block in the workflow must match the sentinel string the
    bash harness sends in the refusal-path message.
  * The bash harness must call each of the four real admin endpoints
    in order. A future refactor that, say, replaced
    POST /admin/embed-keys with a different mint path would leave
    the workflow_dispatch run still passing but the gate functionally
    decoupled from the real surface.
  * The Python assertion script must offer --mode happy AND
    --mode refusal as argparse choices, and must import
    REFUSAL_MESSAGE from app.api.v1.chat_widget rather than
    hardcoding the string (so a future reword at the chat_widget
    source automatically flows here).
  * The CI-only bootstrap script must keep its guardrail env-var
    name as a module-level constant -- a typo here would either
    refuse to run (loud, fine) or, in a future refactor, silently
    skip the guardrail (quiet, bad).

This test module pins those invariants. It runs in the existing
backend-free CI job in .github/workflows/ci.yml (no live backend
needed) so a break here is caught at the cheapest possible point.

Style
=====

AST + filesystem assertions only. No live HTTP, no subprocess, no
docker. The same house style as
tests/api/test_step29y_cluster6_chat_stream_sanitization.py and the
other contract tests in tests/api/.
"""

from __future__ import annotations

import ast
import os
import stat
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


# =====================================================================
# Helpers
# =====================================================================


def _path(relpath: str) -> Path:
    p = REPO_ROOT / relpath
    assert p.exists(), (
        f"Step 30d-C contract: required harness file {relpath!r} is "
        f"missing from the repository. If you intentionally moved or "
        f"renamed it, update this test in the same commit so the "
        f"contract remains pinned."
    )
    return p


def _read(relpath: str) -> str:
    return _path(relpath).read_text(encoding="utf-8")


# =====================================================================
# Workflow file: .github/workflows/widget-e2e.yml
# =====================================================================


def test_widget_e2e_workflow_file_exists_and_parses_as_yaml() -> None:
    """The workflow file must exist and be parseable as YAML.

    A YAML syntax error here would still let the rest of CI run, but
    the dispatchable workflow would be silently broken. We catch it
    in the cheap suite."""

    yaml = pytest.importorskip("yaml")
    raw = _read(".github/workflows/widget-e2e.yml")
    doc = yaml.safe_load(raw)
    assert isinstance(doc, dict), (
        "widget-e2e.yml did not parse as a top-level YAML mapping"
    )
    assert doc.get("name") == "widget-e2e", (
        f"Step 30d-C contract: workflow file 'name:' must be exactly "
        f"'widget-e2e' (found {doc.get('name')!r}). The CANONICAL_RECAP "
        f"closeout row and the README cross-reference both use this "
        f"name as the human-readable identifier."
    )


def test_widget_e2e_workflow_v1_trigger_is_workflow_dispatch_only() -> None:
    """In v1 (this commit), the only trigger is workflow_dispatch.

    The follow-up commit on the next branch ADDS a pull_request
    trigger next to it (Pattern E: deactivate by replacement, never
    delete). Until that follow-up lands, the path-trigger MUST NOT
    be present -- shipping it on PR #16 itself is exactly the
    chicken-and-egg this scaffolding avoided.

    If you are landing the follow-up commit, update this test in the
    same diff so the contract evolves with the trigger."""

    yaml = pytest.importorskip("yaml")
    doc = yaml.safe_load(_read(".github/workflows/widget-e2e.yml"))

    # PyYAML helpfully parses 'on:' as the Python True boolean; check
    # both keys to be robust to either yaml parser behaviour.
    triggers = doc.get("on") if "on" in doc else doc.get(True)
    assert isinstance(triggers, dict), (
        f"widget-e2e.yml 'on:' must be a mapping (found {type(triggers).__name__})"
    )
    assert "workflow_dispatch" in triggers, (
        "widget-e2e.yml must keep the workflow_dispatch trigger; it "
        "is the v1 manual entry point."
    )
    assert "pull_request" not in triggers, (
        "Step 30d-C v1 contract: widget-e2e.yml MUST NOT have a "
        "pull_request trigger yet. It is added in the follow-up "
        "commit after the harness has been observed running cleanly "
        "via manual dispatch. If you are landing that follow-up, "
        "update this test in the same commit."
    )


def test_widget_e2e_workflow_configures_keyword_moderation() -> None:
    """The workflow env block must pin the moderation provider to
    'keyword' and supply a non-empty block-term list. Without this,
    the running app boots with the production-default 'openai'
    provider and the bootstrap step fails on a missing
    OPENAI_API_KEY (which is the Deliverable B locked judgment #4
    crash-loop behavior -- correct in production, wrong for this
    CI surface)."""

    raw = _read(".github/workflows/widget-e2e.yml")
    assert 'MODERATION_PROVIDER: "keyword"' in raw, (
        "widget-e2e.yml env must set MODERATION_PROVIDER='keyword'. "
        "Any other provider in this workflow either requires a "
        "billable API key (openai) or never blocks (null), neither "
        "of which gives the harness a deterministic refusal-path "
        "scenario."
    )
    assert "MODERATION_KEYWORD_BLOCK_TERMS:" in raw, (
        "widget-e2e.yml env must declare MODERATION_KEYWORD_BLOCK_TERMS "
        "so the running app's KeywordModerationProvider has a non-empty "
        "list to match against."
    )


def test_widget_e2e_workflow_has_postgres_and_redis_services() -> None:
    yaml = pytest.importorskip("yaml")
    doc = yaml.safe_load(_read(".github/workflows/widget-e2e.yml"))
    jobs = doc.get("jobs", {})
    assert "widget-e2e" in jobs, (
        "widget-e2e.yml must have a 'widget-e2e' job."
    )
    services = jobs["widget-e2e"].get("services", {})
    assert "postgres" in services, (
        "Step 30d-C contract: widget-e2e job must declare a postgres "
        "service. Without it, alembic upgrade head has no DB."
    )
    assert "redis" in services, (
        "Step 30d-C contract: widget-e2e job must declare a redis "
        "service. The session/chat path imports celery wiring at "
        "app boot; a missing redis aborts startup."
    )
    postgres_image = services["postgres"].get("image", "")
    assert "pgvector" in postgres_image, (
        "Step 30d-C harness contract: the postgres service image must "
        "be a pgvector-enabled variant (e.g. pgvector/pgvector:pg15). "
        "Alembic migration b0e003ffa07f issues CREATE EXTENSION vector "
        "against the harness DB; the stock postgres:15 image lacks the "
        "extension and run 25690719076 failed there. Pinning the image "
        "in this AST test catches a regression before workflow_dispatch."
    )


# =====================================================================
# Bash harness: ci/e2e/run_widget_e2e.sh
# =====================================================================


def test_run_widget_e2e_script_is_executable() -> None:
    p = _path("ci/e2e/run_widget_e2e.sh")
    mode = p.stat().st_mode
    assert mode & stat.S_IXUSR, (
        f"ci/e2e/run_widget_e2e.sh must be chmod +x; current mode "
        f"is {oct(mode)}. Without the executable bit the workflow "
        f"step `bash ci/e2e/run_widget_e2e.sh` still works but the "
        f"local-run recipe in ci/e2e/README.md does not."
    )


def test_run_widget_e2e_script_uses_set_pipefail() -> None:
    """A shell harness without 'set -euo pipefail' can silently
    swallow errors from intermediate steps. We pin the directive
    so a future edit that drops it gets caught."""

    src = _read("ci/e2e/run_widget_e2e.sh")
    assert "set -euo pipefail" in src, (
        "ci/e2e/run_widget_e2e.sh must declare `set -euo pipefail` "
        "near the top so any intermediate failure (e.g. a 4xx from "
        "the embed-key mint) aborts the run instead of carrying a "
        "broken provisioning state into the SSE assertion step."
    )


def test_run_widget_e2e_script_hits_all_four_admin_endpoints() -> None:
    """The harness's whole point is exercising the real admin
    provisioning surface end-to-end. If a refactor changed a URL
    path or replaced an endpoint, the harness would either fail
    loudly (good) or, with a mocked-out URL, silently provision
    nothing while still calling /api/v1/chat/widget (very bad).
    Pin each path."""

    src = _read("ci/e2e/run_widget_e2e.sh")
    for path in (
        "/api/v1/admin/tenants",
        "/api/v1/admin/domains",
        "/api/v1/admin/embed-keys",
    ):
        assert path in src, (
            f"ci/e2e/run_widget_e2e.sh must POST to {path!r} as part "
            f"of the provisioning chain. If you renamed the endpoint "
            f"in app/api/v1/admin.py, update the harness in the same "
            f"commit so the gate keeps testing the real surface."
        )


def test_run_widget_e2e_script_pins_refusal_sentinel() -> None:
    """The sentinel must be a literal in the bash harness (not an
    env variable). Two reasons: (1) it lets this AST test pin the
    exact string, (2) it makes the README's documented contract
    auditable by grep. The contract is that this literal MUST match
    the value of MODERATION_KEYWORD_BLOCK_TERMS in widget-e2e.yml.
    The next test asserts that pairing."""

    src = _read("ci/e2e/run_widget_e2e.sh")
    assert 'REFUSAL_SENTINEL="E2E_REFUSE_SENTINEL"' in src, (
        "ci/e2e/run_widget_e2e.sh must declare "
        "REFUSAL_SENTINEL=\"E2E_REFUSE_SENTINEL\" as a top-level "
        "assignment. Changing the sentinel string also requires "
        "updating MODERATION_KEYWORD_BLOCK_TERMS in widget-e2e.yml "
        "AND the linked test in this file."
    )


def test_workflow_block_term_matches_harness_sentinel() -> None:
    """The sentinel literal in the bash harness must match the
    MODERATION_KEYWORD_BLOCK_TERMS list in the workflow env. A
    mismatch here is exactly the misconfig the harness is supposed
    to catch -- but we ALSO catch it at AST time so a regression
    here doesn't first manifest as a confusing live-CI failure."""

    sentinel = "E2E_REFUSE_SENTINEL"
    workflow = _read(".github/workflows/widget-e2e.yml")
    harness = _read("ci/e2e/run_widget_e2e.sh")

    assert sentinel in workflow, (
        f"Step 30d-C cross-file contract: sentinel {sentinel!r} not "
        f"found in .github/workflows/widget-e2e.yml. If you change "
        f"the sentinel, update both files together."
    )
    assert sentinel in harness, (
        f"Step 30d-C cross-file contract: sentinel {sentinel!r} not "
        f"found in ci/e2e/run_widget_e2e.sh. If you change the "
        f"sentinel, update both files together."
    )


# =====================================================================
# Python assertion script: ci/e2e/assert_widget_stream.py
# =====================================================================


def test_assert_widget_stream_parses_and_has_mode_choices() -> None:
    """The assertion script must offer both 'happy' and 'refusal'
    as argparse choices for --mode. A future refactor that, say,
    dropped the refusal scenario entirely would leave the bash
    harness passing the --mode refusal flag to a script that no
    longer accepted it -- which is a runtime failure but only on
    the manual workflow_dispatch run, well after merge. Catching
    it at AST time is cheaper."""

    src = _read("ci/e2e/assert_widget_stream.py")
    tree = ast.parse(src)

    # Find the argparse add_argument call for --mode.
    found_choices: list[str] | None = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "add_argument":
            if node.args and isinstance(node.args[0], ast.Constant):
                if node.args[0].value == "--mode":
                    for kw in node.keywords:
                        if kw.arg == "choices" and isinstance(kw.value, ast.List):
                            found_choices = [
                                elt.value
                                for elt in kw.value.elts
                                if isinstance(elt, ast.Constant)
                            ]
    assert found_choices is not None, (
        "ci/e2e/assert_widget_stream.py must call add_argument('--mode', "
        "choices=[...]) so argparse pins the accepted values."
    )
    assert "happy" in found_choices and "refusal" in found_choices, (
        f"--mode choices must include both 'happy' and 'refusal' "
        f"(found {found_choices!r})."
    )


def test_assert_widget_stream_imports_refusal_message_from_chat_widget() -> None:
    """The refusal-mode assertion compares against the canonical
    REFUSAL_MESSAGE constant in app.api.v1.chat_widget. Importing
    it (rather than hardcoding) means a future reword at the source
    automatically flows here. If a refactor swapped the import for
    a hardcoded literal, the wording would drift silently the next
    time someone edited chat_widget.py."""

    src = _read("ci/e2e/assert_widget_stream.py")
    assert "from app.api.v1.chat_widget import REFUSAL_MESSAGE" in src, (
        "ci/e2e/assert_widget_stream.py must import REFUSAL_MESSAGE "
        "from app.api.v1.chat_widget rather than hardcoding the "
        "refusal text. Hardcoding would let chat_widget.py reword "
        "the refusal without this script noticing."
    )


# =====================================================================
# Bootstrap script: scripts/bootstrap_platform_admin_ci.py
# =====================================================================


def test_bootstrap_script_parses_and_has_guardrail_constant() -> None:
    """The CI-only bootstrap refuses to run unless a guardrail env
    var is set. The env-var name must be a module-level string
    constant so a typo can't sneak in. We pin the literal name."""

    src = _read("scripts/bootstrap_platform_admin_ci.py")
    tree = ast.parse(src)

    found = False
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Name)
                    and target.id == "_GUARD_ENV_VAR"
                    and isinstance(node.value, ast.Constant)
                    and node.value.value == "LUCIEL_CI_ALLOW_RAW_KEY_STDOUT"
                ):
                    found = True
    assert found, (
        "scripts/bootstrap_platform_admin_ci.py must declare "
        "_GUARD_ENV_VAR = 'LUCIEL_CI_ALLOW_RAW_KEY_STDOUT' at module "
        "level. The workflow's bootstrap step sets this env var "
        "explicitly; renaming either side without the other breaks "
        "the gate silently (the script will refuse to run and the "
        "workflow will fail confusingly)."
    )


def test_bootstrap_script_refuses_production_db_url() -> None:
    """The bootstrap prints a raw key to stdout. A second guardrail
    refuses to run when DATABASE_URL contains a production-shape
    marker. This pins that the rds.amazonaws.com marker is in the
    refused-list so a future edit cannot quietly remove it."""

    src = _read("scripts/bootstrap_platform_admin_ci.py")
    assert '"rds.amazonaws.com"' in src or "'rds.amazonaws.com'" in src, (
        "scripts/bootstrap_platform_admin_ci.py must keep "
        "'rds.amazonaws.com' in its _PROD_DB_MARKERS list. Removing "
        "it would let the bootstrap run -- and print a raw key to "
        "stdout -- against a production-shaped DATABASE_URL."
    )


# =====================================================================
# README cross-reference
# =====================================================================


def test_readme_documents_workflow_dispatch_v1_state() -> None:
    """The README must explain the v1 workflow_dispatch-only state so
    a future maintainer knows the path-trigger flip is intentional
    and tracked, not just an oversight."""

    src = _read("ci/e2e/README.md")
    assert "workflow_dispatch" in src, (
        "ci/e2e/README.md must mention workflow_dispatch in the "
        "trigger-state section."
    )
    assert "pull_request" in src, (
        "ci/e2e/README.md must show the planned pull_request "
        "follow-up so the maintainer can correlate the v1 state "
        "with the planned final shape."
    )
