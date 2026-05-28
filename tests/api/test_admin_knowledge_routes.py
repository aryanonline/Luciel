"""Arc 11 Step 7 — admin knowledge routes contract tests.

The project's established convention for routes that depend on a
live DB + Celery + S3 is static-shape contract tests over the
source / route metadata, with live-DB integration tests gated
behind ``LUCIEL_LIVE_POSTGRES_URL`` (see
``tests/api/test_step29y_cluster4_worker_hardening.py``,
``tests/api/test_arc6_signup_free.py``, and the Step-4 / Step-6
RLS / HNSW test files).

Contracts guarded here:

  R1   Router is registered, prefix is correct.
  R2   Exactly the seven admin routes + the internal retrieve route
       exist with the expected (path, method) tuples.
  R3   Every admin route calls ``ScopePolicy.require_knowledge_role``
       with the correct action label per the §0.6 / §3.2.2 matrix.
  R4   Every admin route uses ``TenantScopedDbSession`` (L3 RLS
       wrapper) and not the bare ``DbSession``.
  R5   The seven new audit-action constants are present in
       ``ALLOWED_ACTIONS``; the new resource type is in
       ``ALLOWED_RESOURCE_TYPES``.
  R6   ``ScopePolicy.require_knowledge_role`` raises 403 when the
       action argument is unknown (programmer-error guard).
  R7   The 413 ``QuotaExceededDetail`` shape matches ARC11_PLAN.md
       §0.4 (six keys, exact values).
  R8   The 403 ``FeatureNotOnTierDetail`` shape matches §3.6.
  R9   Entitlements: per-tier caps are exactly the founder-locked
       Vision §3.3 values (10/50/500 MB; False/True/True for crawl).
  R10  Every admin route emits an ``audit_repo.record(...)`` call
       (audit-log discipline at the route boundary).
  R11  The legacy /admin/knowledge/ingest route is UNTOUCHED — its
       file path + decorator are still present (proof we did not
       deprecate it).
  R12  PII discipline: the crawl route does NOT log payload.url
       (Step 6's discipline propagates).
  R13  The internal retrieve route requires platform_admin.

The role-matrix × CRUD-action grid (Pillar 3 from §8.3) is
expressed in a single parameterised test below — every cell that
SHOULD return 403 is locked at R3 (action label) + the
``require_knowledge_role`` mapping (which is itself unit-tested at
the bottom of this file).
"""
from __future__ import annotations

import ast
import inspect
import re
import unittest
from pathlib import Path

from app.api.v1 import admin_knowledge as ak
from app.models.admin_audit_log import (
    ACTION_KNOWLEDGE_AFFECTED_QUESTIONS_VIEWED,
    ACTION_KNOWLEDGE_CRAWL_ENQUEUED,
    ACTION_KNOWLEDGE_SOURCE_CREATED,
    ACTION_KNOWLEDGE_SOURCE_DELETED,
    ACTION_KNOWLEDGE_SOURCE_LISTED,
    ACTION_KNOWLEDGE_SOURCE_UPDATED,
    ACTION_KNOWLEDGE_SOURCE_VIEWED,
    ALLOWED_ACTIONS,
    ALLOWED_RESOURCE_TYPES,
    RESOURCE_KNOWLEDGE_SOURCE,
)
from app.policy.entitlements import (
    TIER_ENTERPRISE,
    TIER_ENTITLEMENTS,
    TIER_FREE,
    TIER_PRO,
)
from app.policy.scope import (
    ROLE_ADMIN_MANAGER,
    ROLE_ADMIN_OWNER,
    ROLE_INSTANCE_OPERATOR,
    ROLE_READ_ONLY_VIEWER,
    ScopePolicy,
    _KNOWLEDGE_ACTION_ROLES,
)


SRC_PATH = Path(ak.__file__)
SRC = SRC_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------
# R1, R2 — Router shape
# ---------------------------------------------------------------------


class TestRouterShape(unittest.TestCase):

    def test_r1_router_prefix(self):
        self.assertEqual(
            ak.router.prefix,
            "/admin/instances/{instance_id}/knowledge",
        )

    def test_r1_internal_router_prefix(self):
        self.assertEqual(ak.internal_router.prefix, "/internal/v1")

    def test_r2_admin_routes_have_expected_path_method_tuples(self):
        expected = {
            ("/admin/instances/{instance_id}/knowledge/sources", "POST"),
            ("/admin/instances/{instance_id}/knowledge/sources", "GET"),
            ("/admin/instances/{instance_id}/knowledge/sources/{source_id}", "PATCH"),
            ("/admin/instances/{instance_id}/knowledge/sources/{source_id}", "DELETE"),
            ("/admin/instances/{instance_id}/knowledge/sources/{source_id}/chunks", "GET"),
            ("/admin/instances/{instance_id}/knowledge/sources/{source_id}/affected-questions", "GET"),
            ("/admin/instances/{instance_id}/knowledge/crawl", "POST"),
        }
        actual = {
            (route.path, method)
            for route in ak.router.routes
            for method in (route.methods or ())
            if method != "HEAD"
        }
        self.assertEqual(
            actual, expected,
            f"Admin router routes drift: extra={actual - expected}, "
            f"missing={expected - actual}",
        )

    def test_r2_internal_router_has_only_retrieve(self):
        expected = {("/internal/v1/retrieve", "POST")}
        actual = {
            (route.path, method)
            for route in ak.internal_router.routes
            for method in (route.methods or ())
            if method != "HEAD"
        }
        self.assertEqual(actual, expected)


# ---------------------------------------------------------------------
# R3, R10 — Role gating + audit discipline
# ---------------------------------------------------------------------


def _function_calls(tree: ast.AST, target_attr: str) -> list[ast.Call]:
    """Return every Call node in ``tree`` whose .func is an
    ``Attribute`` with ``.attr == target_attr``."""
    out: list[ast.Call] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == target_attr
        ):
            out.append(node)
    return out


def _route_functions() -> dict[str, ast.FunctionDef]:
    """Map of admin route handler name → FunctionDef AST node."""
    tree = ast.parse(SRC)
    names = {
        "create_source", "list_sources", "preview_chunks",
        "update_source", "delete_source", "affected_questions",
        "start_crawl", "internal_retrieve",
    }
    found: dict[str, ast.FunctionDef] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            found[node.name] = node
    return found


class TestRoleGating(unittest.TestCase):
    """Every admin route must call ``ScopePolicy.require_knowledge_role``
    with the action label that matches its CRUD verb."""

    EXPECTED_ACTIONS = {
        "create_source":       "edit",
        "list_sources":        "list",
        "preview_chunks":      "view",
        "update_source":       "edit",
        "delete_source":       "delete",
        "affected_questions":  "view",
        "start_crawl":         "edit",
    }

    def test_r3_every_route_calls_require_knowledge_role(self):
        fns = _route_functions()
        for name, expected_action in self.EXPECTED_ACTIONS.items():
            self.assertIn(name, fns, f"route {name} missing from source")
            fn = fns[name]
            calls = _function_calls(fn, "require_knowledge_role")
            self.assertEqual(
                len(calls), 1,
                f"{name} must call require_knowledge_role exactly once; "
                f"found {len(calls)}",
            )

    def test_r3_action_argument_matches_crud_verb(self):
        fns = _route_functions()
        for name, expected_action in self.EXPECTED_ACTIONS.items():
            fn = fns[name]
            calls = _function_calls(fn, "require_knowledge_role")
            call = calls[0]
            # action= is the third positional arg in the typical form, but
            # we accept both positional and keyword forms.
            actions: list[str] = []
            for kw in call.keywords:
                if kw.arg == "action" and isinstance(kw.value, ast.Constant):
                    actions.append(kw.value.value)
            for arg in call.args[2:]:  # request, instance, action
                if isinstance(arg, ast.Constant):
                    actions.append(arg.value)
            self.assertIn(
                expected_action, actions,
                f"{name} must call require_knowledge_role(action={expected_action!r}); "
                f"got actions={actions}",
            )


class TestAuditDiscipline(unittest.TestCase):

    def test_r10_every_admin_route_calls_audit_repo_record(self):
        fns = _route_functions()
        admin_routes = {
            "create_source", "list_sources", "preview_chunks",
            "update_source", "delete_source", "affected_questions",
            "start_crawl",
        }
        for name in admin_routes:
            fn = fns[name]
            record_calls = _function_calls(fn, "record")
            self.assertGreaterEqual(
                len(record_calls), 1,
                f"{name} must call audit_repo.record(...) at least once "
                f"(audit-log discipline at the route boundary).",
            )

    def test_r10_audit_record_uses_resource_knowledge_source(self):
        # The resource_type kwarg must be RESOURCE_KNOWLEDGE_SOURCE
        # on every audit row this router emits. Catch a typo that
        # would otherwise silently emit RESOURCE_KNOWLEDGE (the
        # legacy enum) and split the audit trail.
        fns = _route_functions()
        for name in (
            "create_source", "list_sources", "preview_chunks",
            "update_source", "delete_source", "affected_questions",
            "start_crawl",
        ):
            record_calls = _function_calls(fns[name], "record")
            for call in record_calls:
                resource_kw = next(
                    (kw for kw in call.keywords if kw.arg == "resource_type"),
                    None,
                )
                self.assertIsNotNone(
                    resource_kw,
                    f"{name}: record(resource_type=...) is required",
                )
                # The value can be a Name reference (RESOURCE_KNOWLEDGE_SOURCE).
                if isinstance(resource_kw.value, ast.Name):
                    self.assertEqual(
                        resource_kw.value.id, "RESOURCE_KNOWLEDGE_SOURCE",
                        f"{name}: resource_type must be RESOURCE_KNOWLEDGE_SOURCE",
                    )


# ---------------------------------------------------------------------
# R4 — TenantScopedDbSession (L3 RLS wrapper)
# ---------------------------------------------------------------------


class TestL3RlsWrapper(unittest.TestCase):
    """All admin routes must use TenantScopedDbSession (Arc 9 C4.2)
    so the DB connection emits SET LOCAL app.admin_id / app.instance_id
    on every BEGIN. The bare DbSession would NOT bind the GUCs and
    RLS would silently 0-row everything."""

    def test_r4_admin_routes_annotate_db_param_as_tenant_scoped(self):
        fns = _route_functions()
        admin_routes = {
            "create_source", "list_sources", "preview_chunks",
            "update_source", "delete_source", "affected_questions",
            "start_crawl",
        }
        for name in admin_routes:
            fn = fns[name]
            db_arg = next(
                (a for a in fn.args.args + fn.args.kwonlyargs if a.arg == "db"),
                None,
            )
            self.assertIsNotNone(db_arg, f"{name}: missing db arg")
            annot = ast.unparse(db_arg.annotation) if db_arg.annotation else ""
            self.assertEqual(
                annot, "TenantScopedDbSession",
                f"{name}: db must be TenantScopedDbSession (L3 RLS wrapper), "
                f"got {annot!r}. DbSession would skip the GUC bind.",
            )

    def test_r4_internal_retrieve_uses_plain_dbsession(self):
        """The internal retrieve endpoint deliberately uses the plain
        DbSession — it binds scope manually via bind_tenant_scope()
        because the caller is platform_admin asking about an arbitrary
        target admin_id, not the caller's own."""
        fns = _route_functions()
        fn = fns["internal_retrieve"]
        db_arg = next(
            (a for a in fn.args.args + fn.args.kwonlyargs if a.arg == "db"),
            None,
        )
        annot = ast.unparse(db_arg.annotation) if db_arg.annotation else ""
        self.assertEqual(annot, "DbSession")
        # And it must call bind_tenant_scope explicitly.
        self.assertIn("bind_tenant_scope(", SRC)


# ---------------------------------------------------------------------
# R5 — Audit constants wired
# ---------------------------------------------------------------------


class TestAuditConstantsWired(unittest.TestCase):

    NEW_ACTIONS = (
        ACTION_KNOWLEDGE_SOURCE_CREATED,
        ACTION_KNOWLEDGE_SOURCE_LISTED,
        ACTION_KNOWLEDGE_SOURCE_VIEWED,
        ACTION_KNOWLEDGE_SOURCE_UPDATED,
        ACTION_KNOWLEDGE_SOURCE_DELETED,
        ACTION_KNOWLEDGE_AFFECTED_QUESTIONS_VIEWED,
        ACTION_KNOWLEDGE_CRAWL_ENQUEUED,
    )

    def test_r5_new_actions_present_in_allowed_actions(self):
        for action in self.NEW_ACTIONS:
            self.assertIn(
                action, ALLOWED_ACTIONS,
                f"action {action!r} not in ALLOWED_ACTIONS — "
                f"AdminAuditRepository.record() will ValueError on it",
            )

    def test_r5_new_resource_type_present(self):
        self.assertIn(RESOURCE_KNOWLEDGE_SOURCE, ALLOWED_RESOURCE_TYPES)


# ---------------------------------------------------------------------
# R6 — require_knowledge_role programmer-error guard
# ---------------------------------------------------------------------


class TestRequireKnowledgeRoleGuard(unittest.TestCase):

    def test_r6_unknown_action_raises_valueerror(self):
        with self.assertRaises(ValueError):
            ScopePolicy.require_knowledge_role(
                request=None, instance=None, action="hack",
            )

    def test_action_role_matrix_matches_doctrine(self):
        # Vision §5.2 + Architecture §3.2.2.
        expected = {
            "list":   {ROLE_ADMIN_OWNER, ROLE_ADMIN_MANAGER, ROLE_INSTANCE_OPERATOR},
            "view":   {ROLE_ADMIN_OWNER, ROLE_ADMIN_MANAGER, ROLE_INSTANCE_OPERATOR},
            "edit":   {ROLE_ADMIN_OWNER, ROLE_ADMIN_MANAGER},
            "delete": {ROLE_ADMIN_OWNER, ROLE_ADMIN_MANAGER},
        }
        # Convert frozensets to sets for comparison.
        actual = {k: set(v) for k, v in _KNOWLEDGE_ACTION_ROLES.items()}
        self.assertEqual(actual, expected)

    def test_read_only_viewer_never_admitted(self):
        """The read_only_viewer role is in the canonical role set
        (so future read-only dashboards have a target) but it MUST
        NOT appear in any allowed-role set for the four CRUD verbs."""
        for action, roles in _KNOWLEDGE_ACTION_ROLES.items():
            self.assertNotIn(
                ROLE_READ_ONLY_VIEWER, roles,
                f"read_only_viewer must NOT be allowed for action {action!r}",
            )


# ---------------------------------------------------------------------
# R7, R8 — Structured error payloads
# ---------------------------------------------------------------------


class TestStructuredErrorPayloads(unittest.TestCase):

    def test_r7_quota_exceeded_detail_has_six_keys(self):
        payload = ak.QuotaExceededDetail(
            error="knowledge_quota_exceeded",
            scope="per_file",
            current_bytes=0,
            incoming_bytes=42,
            cap_bytes=100,
            tier="free",
            remediation="delete_or_upgrade",
        ).model_dump()
        self.assertEqual(
            set(payload.keys()),
            {"error", "scope", "current_bytes", "incoming_bytes",
             "cap_bytes", "tier", "remediation"},
        )
        # Per ARC11_PLAN.md §0.4: error literal is locked.
        self.assertEqual(payload["error"], "knowledge_quota_exceeded")

    def test_r7_quota_scope_only_per_file_or_total(self):
        # Pydantic Literal — adding a third value requires a doctrine update.
        from typing import get_args
        scope_field = ak.QuotaExceededDetail.model_fields["scope"]
        # Literal types expose their values via __args__.
        self.assertEqual(
            sorted(get_args(scope_field.annotation)),
            ["per_file", "total"],
        )

    def test_r8_feature_not_on_tier_shape(self):
        payload = ak.FeatureNotOnTierDetail(
            error="feature_not_available_on_tier",
            tier="free",
            feature="website_crawl",
        ).model_dump()
        self.assertEqual(
            set(payload.keys()), {"error", "tier", "feature"},
        )
        self.assertEqual(payload["error"], "feature_not_available_on_tier")


# ---------------------------------------------------------------------
# R9 — Entitlements
# ---------------------------------------------------------------------


class TestEntitlements(unittest.TestCase):

    def test_r9_per_file_caps_locked(self):
        free = TIER_ENTITLEMENTS[TIER_FREE]
        pro = TIER_ENTITLEMENTS[TIER_PRO]
        ent = TIER_ENTITLEMENTS[TIER_ENTERPRISE]
        self.assertEqual(free.knowledge_per_file_bytes_cap, 10 * 1024 * 1024)
        self.assertEqual(pro.knowledge_per_file_bytes_cap, 50 * 1024 * 1024)
        self.assertEqual(ent.knowledge_per_file_bytes_cap, 500 * 1024 * 1024)

    def test_r9_website_crawl_gated_to_pro_enterprise(self):
        free = TIER_ENTITLEMENTS[TIER_FREE]
        pro = TIER_ENTITLEMENTS[TIER_PRO]
        ent = TIER_ENTITLEMENTS[TIER_ENTERPRISE]
        self.assertFalse(free.knowledge_website_crawl_enabled)
        self.assertTrue(pro.knowledge_website_crawl_enabled)
        self.assertTrue(ent.knowledge_website_crawl_enabled)

    def test_r9_per_admin_total_caps_unchanged(self):
        """Don't accidentally drift the Arc 10 total caps."""
        self.assertEqual(
            TIER_ENTITLEMENTS[TIER_FREE].knowledge_bytes_cap,
            100 * 1024 * 1024,
        )
        self.assertEqual(
            TIER_ENTITLEMENTS[TIER_PRO].knowledge_bytes_cap,
            5 * 1024 * 1024 * 1024,
        )
        self.assertIsNone(TIER_ENTITLEMENTS[TIER_ENTERPRISE].knowledge_bytes_cap)


# ---------------------------------------------------------------------
# R11 — Legacy route untouched
# ---------------------------------------------------------------------


class TestLegacyRouteUntouched(unittest.TestCase):

    def test_r11_legacy_ingest_route_still_in_admin_module(self):
        admin_src = (
            Path(__file__).resolve().parents[2]
            / "app" / "api" / "v1" / "admin.py"
        ).read_text(encoding="utf-8")
        # The legacy route's path + handler name.
        self.assertIn('@router.post("/knowledge/ingest"', admin_src)
        self.assertIn("def ingest_knowledge(", admin_src)


# ---------------------------------------------------------------------
# R12 — Crawl route PII discipline
# ---------------------------------------------------------------------


class TestCrawlPiiDiscipline(unittest.TestCase):
    """The crawl route accepts a URL — which is potentially-PII
    (admins might paste internal hostnames). Step 6's discipline
    extends: don't log the URL anywhere."""

    def test_r12_start_crawl_does_not_log_payload_url(self):
        """Walk every logger.* call inside ``start_crawl`` and verify
        none of them reference ``payload.url``."""
        fns = _route_functions()
        fn = fns["start_crawl"]
        for call in ast.walk(fn):
            if not isinstance(call, ast.Call):
                continue
            func = call.func
            is_logger = (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "logger"
            )
            if not is_logger:
                continue
            call_src = ast.unparse(call)
            self.assertNotIn(
                "payload.url", call_src,
                f"logger call in start_crawl references payload.url — "
                f"PII leak vector. Pass only opaque ids.",
            )

    def test_r12_audit_record_does_not_carry_payload_url(self):
        fns = _route_functions()
        fn = fns["start_crawl"]
        record_calls = _function_calls(fn, "record")
        for call in record_calls:
            # Walk the whole call AST and ensure no `payload.url`
            # appears anywhere in its args.
            call_src = ast.unparse(call)
            self.assertNotIn(
                "payload.url", call_src,
                f"audit_repo.record call carries payload.url — that's a "
                f"PII vector. Store only opaque ids in audit rows.",
            )


# ---------------------------------------------------------------------
# R13 — Internal retrieve requires platform_admin
# ---------------------------------------------------------------------


class TestInternalRetrieveAuthZ(unittest.TestCase):

    def test_r13_route_checks_is_platform_admin(self):
        fns = _route_functions()
        fn = fns["internal_retrieve"]
        fn_src = ast.unparse(fn)
        self.assertIn("ScopePolicy.is_platform_admin(", fn_src)
        # And it must raise 403 when the check fails.
        self.assertIn("status.HTTP_403_FORBIDDEN", fn_src)

    def test_r13_route_uses_bind_tenant_scope(self):
        """The platform_admin caller's own scope is irrelevant; the
        query is targeted at the payload.admin_id, so the route
        explicitly binds that scope before invoking the retriever."""
        fns = _route_functions()
        fn = fns["internal_retrieve"]
        fn_src = ast.unparse(fn)
        self.assertIn("bind_tenant_scope(", fn_src)


# ---------------------------------------------------------------------
# Role × CRUD matrix smoke
# ---------------------------------------------------------------------


class TestRoleCrudMatrix(unittest.TestCase):
    """The 4×4 matrix from §0.6 / §8.3 — explicit enumeration so a
    refactor of ``_KNOWLEDGE_ACTION_ROLES`` can't silently drift the
    contract.

    Cells: (role, action) → admitted? True/False.
    """

    MATRIX = {
        # admin_owner: full access
        (ROLE_ADMIN_OWNER, "list"):   True,
        (ROLE_ADMIN_OWNER, "view"):   True,
        (ROLE_ADMIN_OWNER, "edit"):   True,
        (ROLE_ADMIN_OWNER, "delete"): True,
        # admin_manager: full access
        (ROLE_ADMIN_MANAGER, "list"):   True,
        (ROLE_ADMIN_MANAGER, "view"):   True,
        (ROLE_ADMIN_MANAGER, "edit"):   True,
        (ROLE_ADMIN_MANAGER, "delete"): True,
        # instance_operator: read only (and scoped at the route layer)
        (ROLE_INSTANCE_OPERATOR, "list"):   True,
        (ROLE_INSTANCE_OPERATOR, "view"):   True,
        (ROLE_INSTANCE_OPERATOR, "edit"):   False,
        (ROLE_INSTANCE_OPERATOR, "delete"): False,
        # read_only_viewer: denied everywhere
        (ROLE_READ_ONLY_VIEWER, "list"):   False,
        (ROLE_READ_ONLY_VIEWER, "view"):   False,
        (ROLE_READ_ONLY_VIEWER, "edit"):   False,
        (ROLE_READ_ONLY_VIEWER, "delete"): False,
    }

    def test_matrix(self):
        for (role, action), admitted in self.MATRIX.items():
            allowed = _KNOWLEDGE_ACTION_ROLES[action]
            self.assertEqual(
                role in allowed, admitted,
                f"({role!r}, {action!r}): expected admitted={admitted}, "
                f"got role∈allowed={role in allowed}. Doctrine drift!",
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
