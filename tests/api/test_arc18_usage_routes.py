"""Arc 18 — Usage API four-walls + reset-webhook registration (shape).

AST + import-level assertions (no Postgres / FastAPI runtime), matching
the repo's tests/api convention for route-contract tests:

  * The usage router is registered in the api aggregator.
  * Router prefix is ``/admin/usage`` and the list + single routes exist.
  * The single-instance route enforces tenant isolation via
    ``ScopePolicy.enforce_admin_owns_instance`` (the cross-Admin wall) and
    the list route enumerates only ``list_for_admin`` (the WHERE-admin
    fence).
  * The response schema carries the spec's per-instance fields.
  * The billing webhook registers ``invoice.paid`` +
    ``customer.subscription.renewed`` → the reset handler.
  * No model-selection surface leaks into the new modules.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
USAGE_API = REPO_ROOT / "app" / "api" / "v1" / "admin" / "usage.py"
ROUTER_AGG = REPO_ROOT / "app" / "api" / "router.py"
WEBHOOK = REPO_ROOT / "app" / "services" / "billing_webhook_service.py"


@pytest.fixture(scope="module")
def usage_source() -> str:
    return USAGE_API.read_text(encoding="utf-8")


class TestRouterRegistration:

    def test_aggregator_imports_and_includes_usage_router(self):
        src = ROUTER_AGG.read_text(encoding="utf-8")
        assert "admin_usage" in src
        assert "include_router(admin_usage.router)" in src


class TestUsageRoutes:

    def test_router_prefix_is_admin_usage(self, usage_source: str):
        assert 'prefix="/admin/usage"' in usage_source

    def test_list_and_single_routes_present(self, usage_source: str):
        assert '@router.get("/{instance_pk}"' in usage_source
        # The list route is mounted at the router root ("" and "/").
        assert '@router.get("",' in usage_source or '@router.get("/",' in usage_source

    def test_single_route_enforces_owns_instance(self, usage_source: str):
        # The cross-Admin wall must be present on the single-instance read.
        assert "ScopePolicy.enforce_admin_owns_instance" in usage_source

    def test_list_route_fences_on_list_for_admin(self, usage_source: str):
        assert "list_for_admin" in usage_source

    def test_count_read_from_meter_not_fabricated(self, usage_source: str):
        # current is read from the meter, cap from the entitlement helper.
        assert "current_count" in usage_source
        assert "conversation_budget" in usage_source


class TestResponseSchema:

    def test_instance_usage_view_fields(self, usage_source: str):
        tree = ast.parse(usage_source)
        view = next(
            (
                n
                for n in tree.body
                if isinstance(n, ast.ClassDef) and n.name == "InstanceUsageView"
            ),
            None,
        )
        assert view is not None, "InstanceUsageView schema missing"
        fields = {
            n.target.id
            for n in view.body
            if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)
        }
        required = {
            "instance_id",
            "instance_name",
            "tier",
            "cadence",
            "current",
            "cap",
            "overage",
            "billing_period_start",
            "utilization_pct",
            "alert_state",
        }
        missing = required - fields
        assert not missing, f"InstanceUsageView missing fields: {missing}"


class TestWebhookResetRegistered:

    def test_invoice_paid_and_renewed_routed_to_reset_handler(self):
        src = WEBHOOK.read_text(encoding="utf-8")
        assert '"invoice.paid": self._on_invoice_paid' in src
        assert (
            '"customer.subscription.renewed": self._on_invoice_paid' in src
        )


class TestNoModelSelectionSurface:
    """ARC 18 must NOT expose any model-selection surface (a hard product
    constraint). Grep the new modules for the forbidden tokens."""

    def test_usage_api_has_no_model_selection(self, usage_source: str):
        lowered = usage_source.lower()
        for token in ("model_name", "model_select", "choose_model", "llm_model"):
            assert token not in lowered, f"forbidden model-selection token: {token}"


if __name__ == "__main__":  # pragma: no cover
    import pytest as _p

    _p.main([__file__, "-q"])
