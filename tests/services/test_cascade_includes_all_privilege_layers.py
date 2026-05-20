"""Backend-free contract test for Step 30a.7 — cascade integrity hardening.

This module pins the 13-layer in-function shape of
``AdminService.deactivate_tenant_with_cascade`` so we catch
unintentional drift between:

  * the canonical 13-layer cascade enumeration locked in the
    function docstring (which mirrors ARCHITECTURE §3.2.13 and
    CANONICAL_RECAP §14);
  * the in-body ``# --- N. <table>`` comment markers (the load-bearing
    visual contract reviewers scan to confirm "did we walk every
    layer?");
  * the imports of the four Step 30a.7 NEW layers' SQLAlchemy models
    and the corresponding audit-log resource / action constants;
  * the upstream-subscription footnote (subscription is NOT an
    in-function layer — it flips upstream in
    ``billing_webhook_service.py``; the cascade docstring must say so
    or future readers will count 14 layers and "fix" the off-by-one).

Coverage budget: 14+ tests per Step 30a.7 design pass-4 contract.

Why static AST + text inspection (and not a runtime fixture)?

  * The cascade is one function. The visual contract reviewers care
    about (docstring enumeration ↔ body markers ↔ imports) is exactly
    what static inspection pins. A runtime fixture would spin up a
    full DB session, seed a tenant, and assert audit rows -- valuable
    in the live e2e (see ``tests/e2e/step_30a_live_e2e.py``) but the
    wrong tool for the symmetry contract pinned here.
  * This file MUST stay green even when no DB is reachable -- it is
    the pre-deploy guard rail. The live e2e is the post-deploy guard
    rail. Both layers are required; neither subsumes the other.
  * See ``tests/api/test_step30a_billing_shape.py`` for the precedent
    pattern (``test_cancel_uses_existing_deactivate_cascade``).
"""
from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
ADMIN_SERVICE_PATH = REPO_ROOT / "app" / "services" / "admin_service.py"


# ---------------------------------------------------------------------
# 0. Shared parse helpers — single source of truth for the function
#    AST so every test below is reading the same node.
# ---------------------------------------------------------------------

def _admin_service_source() -> str:
    return ADMIN_SERVICE_PATH.read_text(encoding="utf-8")


def _admin_service_module() -> ast.Module:
    return ast.parse(_admin_service_source())


def _deactivate_cascade_node() -> ast.FunctionDef:
    """Locate the cascade function in the AdminService class.

    We do an AST walk (not regex) so we never get fooled by a doc-comment
    that mentions the function name. There must be exactly one.
    """
    mod = _admin_service_module()
    candidates: list[ast.FunctionDef] = []
    for node in ast.walk(mod):
        if isinstance(node, ast.FunctionDef) and node.name == "deactivate_tenant_with_cascade":
            candidates.append(node)
    assert len(candidates) == 1, (
        f"expected exactly one definition of deactivate_tenant_with_cascade, "
        f"found {len(candidates)} -- if this is intentional (e.g. a tier "
        f"variant), update §3.2.13 first, then this test."
    )
    return candidates[0]


def _deactivate_cascade_source() -> str:
    """Return the slice of admin_service.py containing the cascade body.

    We need the raw text (not just the AST) because the load-bearing
    visual contract is the ``# --- N. <table>`` comments, which AST
    strips. Slicing by line range keeps our search scoped: we never
    want to accidentally count a comment in some *other* function.
    """
    node = _deactivate_cascade_node()
    lines = _admin_service_source().splitlines()
    # ast lineno is 1-indexed; end_lineno present on Py3.8+.
    start = node.lineno - 1
    end = node.end_lineno  # exclusive in slicing
    return "\n".join(lines[start:end])


def _deactivate_cascade_docstring() -> str:
    node = _deactivate_cascade_node()
    doc = ast.get_docstring(node)
    assert doc is not None, (
        "deactivate_tenant_with_cascade MUST carry a docstring -- it is the "
        "canonical 13-layer enumeration mirrored in §3.2.13. See Step 30a.7."
    )
    return doc


# ---------------------------------------------------------------------
# 1. The 13 numbered layer markers must all be present, in order, in
#    the function body. This is the load-bearing visual contract.
# ---------------------------------------------------------------------

# Canonical 13-layer in-function cascade. Subscription is UPSTREAM
# (billing_webhook_service.py), NOT a body layer -- do NOT add it here.
EXPECTED_LAYER_NUMBERS = list(range(1, 14))  # 1..13 inclusive


class TestCascadeBodyHas13NumberedLayers:
    def test_each_layer_marker_present(self):
        body = _deactivate_cascade_source()
        for n in EXPECTED_LAYER_NUMBERS:
            # The canonical marker shape is ``# --- N. <token> cascade``
            # (or, for the tenant_config closing layer, ``# --- 13.
            # tenant_config itself``). We match the prefix to stay
            # tolerant of trailing wording tweaks.
            pat = re.compile(rf"^\s*#\s*---\s*{n}\.\s", re.MULTILINE)
            assert pat.search(body), (
                f"missing in-body layer marker '# --- {n}. ...' inside "
                f"deactivate_tenant_with_cascade. The 13-layer cascade is "
                f"the canonical contract; renumbering or skipping a layer "
                f"is a §3.2.13 spec change first, not a code change first."
            )

    def test_markers_are_in_ascending_order(self):
        """The 13 markers MUST appear 1, 2, 3, …, 13 top-to-bottom.

        Out-of-order numbers (e.g. an inserted "11." before "10.") are
        exactly the comment-drift that Step 30a.7 set out to eliminate.
        """
        body = _deactivate_cascade_source()
        pat = re.compile(r"^\s*#\s*---\s*(\d+)\.\s", re.MULTILINE)
        found = [int(m.group(1)) for m in pat.finditer(body)]
        # Keep only markers in our expected range; ignore any pre-30a.2
        # sub-step decorations like ``# --- 3.5 Memory cascade ---``
        # that survive in *other* helper methods. Those are out of scope
        # for this contract because our slice is the cascade body only.
        found_main = [n for n in found if n in EXPECTED_LAYER_NUMBERS]
        assert found_main == EXPECTED_LAYER_NUMBERS, (
            f"in-body layer markers are out of order or missing values. "
            f"expected {EXPECTED_LAYER_NUMBERS}, found {found_main}. "
            f"§3.2.13 locks the ordering."
        )

    def test_no_layer_14_or_higher(self):
        """Catch the off-by-one trap: someone counts subscription as a
        body layer and adds a ``# --- 14.`` marker. Subscription is
        upstream; the body stops at 13."""
        body = _deactivate_cascade_source()
        pat = re.compile(r"^\s*#\s*---\s*(\d+)\.\s", re.MULTILINE)
        for m in pat.finditer(body):
            n = int(m.group(1))
            assert n <= 13, (
                f"found in-body layer marker '# --- {n}. ...' but the "
                f"canonical cascade has exactly 13 in-function layers. "
                f"Subscription is UPSTREAM (billing_webhook_service.py). "
                f"If this is a genuine new layer, update §3.2.13 first."
            )


# ---------------------------------------------------------------------
# 2. Docstring contract — the canonical 13-layer enumeration lives in
#    the function docstring (mirrored in §3.2.13).
# ---------------------------------------------------------------------

class TestCascadeDocstringEnumerates13Layers:
    # Each tuple is (layer_number, distinctive_token_we_expect_in_the_docstring).
    # We deliberately pick the TABLE NAME (or its underscore form) because
    # that is the load-bearing contract reviewers cross-check against the
    # schema; English prose like "the seventh layer" is paraphrasable
    # drift bait.
    EXPECTED_LAYER_TOKENS = [
        (1, "conversations"),
        (2, "identity_claims"),
        (3, "memory_items"),
        (4, "api_keys"),
        (5, "luciel_instances"),
        (6, "agents"),
        (7, "agent_configs"),
        (8, "domain_configs"),
        (9, "scope_assignments"),
        (10, "user_invites"),
        (11, "sessions"),
        (12, "synthetic_orphan_users"),
        (13, "tenant_config"),
    ]

    def test_docstring_names_every_layer_table(self):
        doc = _deactivate_cascade_docstring()
        for n, token in self.EXPECTED_LAYER_TOKENS:
            assert token in doc, (
                f"cascade docstring is missing the canonical token "
                f"'{token}' for layer {n}. Either the docstring drifted "
                f"or the canonical enumeration was renamed -- update "
                f"§3.2.13 first, then mirror here."
            )

    def test_docstring_mentions_upstream_subscription(self):
        """The docstring MUST call out that subscription is upstream,
        not an in-function layer -- otherwise readers count 14 and
        'fix' the off-by-one downward."""
        doc = _deactivate_cascade_docstring().lower()
        assert "subscription" in doc, (
            "cascade docstring must reference subscription to disambiguate "
            "the upstream-vs-in-function boundary"
        )
        assert "upstream" in doc or "billing_webhook_service" in doc, (
            "cascade docstring must mark subscription as UPSTREAM (the flip "
            "lives in billing_webhook_service.py); otherwise the 13-vs-14 "
            "off-by-one trap reopens."
        )

    def test_docstring_mentions_step_30a_7(self):
        """The docstring carries the closing-tag-per-step pattern -- the
        Step 30a.7 token MUST appear so future drift sweeps land here
        and not on a stale Step 30a.2 paragraph."""
        doc = _deactivate_cascade_docstring()
        assert "30a.7" in doc, (
            "cascade docstring must include the Step 30a.7 token (closing-"
            "tag-per-step pattern). Without it, the drift-sweep grep "
            "misses this site."
        )

    def test_docstring_enumerates_13_distinct_layers(self):
        """A free-text sanity check: the docstring must call out 13
        layers explicitly somewhere (so a reader skimming the prose
        agrees with the markers below)."""
        doc = _deactivate_cascade_docstring()
        assert "13" in doc, (
            "cascade docstring must explicitly call out the count '13' "
            "(layers / rows / etc.) so prose and markers agree."
        )


# ---------------------------------------------------------------------
# 3. Imports — the 4 NEW layers (scope_assignments, user_invites,
#    sessions, synthetic_orphan_users) each bring in models + audit
#    constants. If any of those imports gets pruned, the cascade body
#    will NameError at runtime; pinning them statically catches that
#    in CI before deploy.
# ---------------------------------------------------------------------

REQUIRED_IMPORT_NAMES = {
    # SQLAlchemy models brought in by Step 30a.7
    "ScopeAssignment",      # L9
    "EndReason",            # L9 -- enum used to mark scopes ended_reason
    "UserInvite",           # L10
    "InviteStatus",         # L10 -- enum used to filter PENDING
    # SessionModel is imported under that alias (collision with starlette
    # Session). We accept either name in the imported-symbol set.
    # User -- L12 (synthetic orphan flip)
    "User",
    # Audit-log constants
    "ACTION_INVITE_REVOKED",
    "RESOURCE_SCOPE_ASSIGNMENT",
    "RESOURCE_SESSION",
    "RESOURCE_USER",
    "RESOURCE_USER_INVITE",
}


def _imported_names() -> set[str]:
    """Return the set of names actually pulled into admin_service.py.

    We walk both ``import X`` and ``from M import X as Y`` forms; for
    aliased imports the asname is what matters.
    """
    mod = _admin_service_module()
    names: set[str] = set()
    for node in ast.walk(mod):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
    return names


class TestStep30a7ImportsArePresent:
    def test_every_required_import_present(self):
        imported = _imported_names()
        missing = REQUIRED_IMPORT_NAMES - imported
        assert not missing, (
            f"admin_service.py is missing Step 30a.7 imports: {sorted(missing)}. "
            f"These are referenced from the cascade body; pruning them is a "
            f"latent NameError at deploy time."
        )

    def test_session_model_is_imported_under_some_name(self):
        """The session model is normally imported as ``SessionModel`` to
        dodge the SQLAlchemy ``Session`` collision. We accept either
        name but require ONE of them, because L11 (sessions cascade)
        cannot work without it."""
        imported = _imported_names()
        assert ("SessionModel" in imported) or ("Session" in imported), (
            "admin_service.py must import the sessions table model (as "
            "SessionModel or Session) for L11. Step 30a.7 added the "
            "sessions cascade -- the import is load-bearing."
        )


# ---------------------------------------------------------------------
# 4. Resource-token coverage — for every layer the cascade is supposed
#    to audit, the corresponding RESOURCE_* token must appear somewhere
#    in the body. This catches the "I added the bulk-update but forgot
#    the audit row" half-finished-layer bug.
# ---------------------------------------------------------------------

# Resource token expected to be referenced (by name) inside the cascade body.
# Some layers delegate to a service that emits its own audit row (e.g.
# memory, api_keys, luciel_instances, agents) and therefore do NOT
# reference their RESOURCE_* token from the cascade body directly; those
# are excluded from this set. The set below is the explicit in-body
# audit-row coverage contract.
EXPECTED_IN_BODY_RESOURCE_TOKENS = {
    "RESOURCE_CONVERSATION",        # L1 inline audit
    "RESOURCE_IDENTITY_CLAIM",      # L2 inline audit
    "RESOURCE_AGENT",               # L7 agent_configs inline audit
    "RESOURCE_DOMAIN",              # L8 domain_configs inline audit
    "RESOURCE_SCOPE_ASSIGNMENT",    # L9 NEW
    "RESOURCE_USER_INVITE",         # L10 NEW
    "RESOURCE_SESSION",             # L11 NEW
    "RESOURCE_USER",                # L12 NEW (synthetic-orphan flip)
    "RESOURCE_TENANT",              # L13 final tenant audit
}


class TestCascadeBodyAuditTokenCoverage:
    def test_every_required_resource_token_appears_in_body(self):
        body = _deactivate_cascade_source()
        missing = {
            tok for tok in EXPECTED_IN_BODY_RESOURCE_TOKENS
            if tok not in body
        }
        assert not missing, (
            f"cascade body is missing required RESOURCE_* tokens: "
            f"{sorted(missing)}. Each in-body layer must emit its own "
            f"audit row; missing the token means a layer is bulk-updating "
            f"silently. That breaks traceability (pillar)."
        )

    def test_invite_layer_uses_invite_revoked_action(self):
        """L10 user_invites is the ONE layer that does NOT use
        ACTION_CASCADE_DEACTIVATE -- invites have their own semantic
        action ACTION_INVITE_REVOKED. Reviewers reading the audit
        stream expect this distinction; pin it."""
        body = _deactivate_cascade_source()
        assert "ACTION_INVITE_REVOKED" in body, (
            "L10 user_invites cascade must emit ACTION_INVITE_REVOKED "
            "(not ACTION_CASCADE_DEACTIVATE). The semantic distinction "
            "matters for the audit stream readability."
        )

    def test_cascade_deactivate_action_used_for_other_layers(self):
        """The other in-body audit-emitting layers use the umbrella
        ACTION_CASCADE_DEACTIVATE. At least one reference must remain."""
        body = _deactivate_cascade_source()
        assert "ACTION_CASCADE_DEACTIVATE" in body, (
            "cascade body must reference ACTION_CASCADE_DEACTIVATE for the "
            "non-invite layers; losing it means the umbrella action got "
            "deleted, which would break the audit stream's filterability."
        )


# ---------------------------------------------------------------------
# 5. Synthetic-orphan narrowing — L12 must NOT flip real users, and
#    must NOT flip a synthetic user who still has an active scope on
#    a different tenant. The contract here is text-based because the
#    logic is one block we want to keep recognizable.
# ---------------------------------------------------------------------

class TestSyntheticOrphanNarrowing:
    def test_layer_12_checks_synthetic_flag(self):
        """L12 MUST gate on ``synthetic=True`` -- otherwise tenant
        teardown deactivates real users, which is a security incident
        (a real user loses login because their employer's tenant got
        canceled)."""
        body = _deactivate_cascade_source()
        # Find the L12 block and assert it gates on synthetic.
        m = re.search(
            r"#\s*---\s*12\..*?(?=#\s*---\s*13\.)",
            body,
            re.DOTALL,
        )
        assert m is not None, "could not locate L12 block inside cascade body"
        l12 = m.group(0)
        assert "synthetic" in l12.lower(), (
            "L12 (synthetic_orphan_users) must reference the synthetic "
            "flag to avoid deactivating real users. SECURITY-CRITICAL."
        )

    def test_layer_12_checks_remaining_active_scopes(self):
        """L12 MUST verify the user has zero remaining active
        scope_assignments before flipping ``users.active=False`` --
        otherwise a synthetic user with a scope on tenant B gets locked
        out when tenant A is deactivated."""
        body = _deactivate_cascade_source()
        m = re.search(
            r"#\s*---\s*12\..*?(?=#\s*---\s*13\.)",
            body,
            re.DOTALL,
        )
        assert m is not None
        l12 = m.group(0).lower()
        # We accept any of the canonical "still has another active scope"
        # idioms; the load-bearing thing is that the layer reads the
        # scope_assignments table again to count.
        assert "scope" in l12, (
            "L12 must consult scope_assignments to confirm the synthetic "
            "user is orphaned cluster-wide before flipping users.active. "
            "Pillar: security + correctness."
        )

    def test_layer_12_flips_user_active_false(self):
        """The whole point of L12 is to flip ``active = False`` on the
        narrow set of synthetic-and-orphaned users."""
        body = _deactivate_cascade_source()
        m = re.search(
            r"#\s*---\s*12\..*?(?=#\s*---\s*13\.)",
            body,
            re.DOTALL,
        )
        assert m is not None
        l12 = m.group(0)
        # Accept either `active=False` or `active = False` (PEP8 variants).
        assert re.search(r"active\s*=\s*False", l12), (
            "L12 must set users.active = False on the narrow synthetic-"
            "orphan set. Without that flip, the layer is a no-op."
        )


# ---------------------------------------------------------------------
# 6. Audit-row note pins the "13 in-function + 1 upstream" math --
#    the final tenant audit row carries a note that's the easiest
#    place for future drift to be detected.
# ---------------------------------------------------------------------

class TestFinalTenantAuditCarriesStep30a7Note:
    def test_final_tenant_audit_note_mentions_13_and_upstream(self):
        body = _deactivate_cascade_source()
        # Locate L13 block (everything after `# --- 13.`).
        m = re.search(r"#\s*---\s*13\..*", body, re.DOTALL)
        assert m is not None, "could not locate L13 block inside cascade body"
        l13 = m.group(0)
        # The final note must include the canonical math token so a
        # future engineer touching the cascade sees the count contract.
        assert "13" in l13, (
            "L13 final tenant audit row must reference '13' (in-function "
            "layer count) in its note; that's the off-by-one canary."
        )
        # And it must call out upstream subscription so the +1 is explained.
        assert ("subscription" in l13.lower()) or ("upstream" in l13.lower()), (
            "L13 final tenant audit row must reference upstream "
            "subscription so the 13 + 1 = 14 total is self-documenting."
        )

    def test_final_tenant_audit_note_references_step_30a_7(self):
        body = _deactivate_cascade_source()
        m = re.search(r"#\s*---\s*13\..*", body, re.DOTALL)
        assert m is not None
        l13 = m.group(0)
        assert "30a.7" in l13, (
            "L13 final tenant audit row must carry the Step 30a.7 token "
            "(closing-tag-per-step). Step 30a.2's '9-layer' wording was "
            "replaced this step; the new token must be present."
        )


# ---------------------------------------------------------------------
# 7. Syntax canary — the file must parse. Trivially true if any test
#    above ran, but this gives a clearer error message in CI.
# ---------------------------------------------------------------------

class TestAdminServiceParses:
    def test_module_parses(self):
        try:
            ast.parse(_admin_service_source())
        except SyntaxError as exc:
            pytest.fail(f"admin_service.py failed to parse: {exc}")
