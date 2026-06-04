"""Rescan Tier-C — knowledge_graph_enabled entitlement axis tests.

Verifies:
  * knowledge_graph_enabled field EXISTS on TierEntitlement.
  * Free = False, Pro = True, Enterprise = True (Vision §7).
  * resolve_entitlement('free', 'knowledge_graph_enabled') == False.
  * resolve_entitlement('pro',  'knowledge_graph_enabled') == True.
  * resolve_entitlement('enterprise', 'knowledge_graph_enabled') == True.
"""
from __future__ import annotations

import pytest
from dataclasses import fields


def test_knowledge_graph_enabled_field_exists():
    """TierEntitlement must have the knowledge_graph_enabled axis."""
    from app.policy.entitlements import TierEntitlement
    field_names = {f.name for f in fields(TierEntitlement)}
    assert "knowledge_graph_enabled" in field_names, (
        "Rescan Tier-C: TierEntitlement.knowledge_graph_enabled is MISSING. "
        "The audit found this axis absent (Arc 16 not implemented)."
    )


def test_free_tier_graph_disabled():
    from app.policy.entitlements import TIER_ENTITLEMENTS, TIER_FREE
    assert TIER_ENTITLEMENTS[TIER_FREE].knowledge_graph_enabled is False, (
        "Free tier must NOT have knowledge_graph_enabled (vector-only path)."
    )


def test_pro_tier_graph_enabled():
    from app.policy.entitlements import TIER_ENTITLEMENTS, TIER_PRO
    assert TIER_ENTITLEMENTS[TIER_PRO].knowledge_graph_enabled is True, (
        "Pro tier must have knowledge_graph_enabled (Vision §7)."
    )


def test_enterprise_tier_graph_enabled():
    from app.policy.entitlements import TIER_ENTITLEMENTS, TIER_ENTERPRISE
    assert TIER_ENTITLEMENTS[TIER_ENTERPRISE].knowledge_graph_enabled is True, (
        "Enterprise tier must have knowledge_graph_enabled (Vision §7)."
    )


def test_resolve_entitlement_free():
    from app.policy.entitlements import resolve_entitlement
    result = resolve_entitlement(tier="free", axis="knowledge_graph_enabled")
    assert result is False


def test_resolve_entitlement_pro():
    from app.policy.entitlements import resolve_entitlement
    result = resolve_entitlement(tier="pro", axis="knowledge_graph_enabled")
    assert result is True


def test_resolve_entitlement_enterprise():
    from app.policy.entitlements import resolve_entitlement
    result = resolve_entitlement(tier="enterprise", axis="knowledge_graph_enabled")
    assert result is True


def test_all_tiers_have_field():
    """All three tiers must have the field set (no missing/None)."""
    from app.policy.entitlements import ALL_TIERS_V2, TIER_ENTITLEMENTS
    for tier in ALL_TIERS_V2:
        row = TIER_ENTITLEMENTS[tier]
        assert hasattr(row, "knowledge_graph_enabled"), (
            f"TIER_ENTITLEMENTS[{tier!r}] missing knowledge_graph_enabled"
        )
        assert isinstance(row.knowledge_graph_enabled, bool), (
            f"knowledge_graph_enabled for {tier!r} must be bool, "
            f"got {type(row.knowledge_graph_enabled)}"
        )
