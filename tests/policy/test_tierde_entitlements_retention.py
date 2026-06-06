"""RESCAN TIER-DE(ent) — Entitlement value corrections + per-tier retention.

Pins the four changes landed in this unit:

1. Uptime SLA values (Vision §7 / §5.6 / §9 items 6/7):
   Pro  = 99.9 %  (was 99.5 — encoded the drift)
   Enterprise = 99.95 % (was 99.9  — encoded the drift)

2. Per-tier transcript/summary retention (Architecture §3.4.10):
   resolve_retention_days() implements the three-layer order:
       tenant override > tier default > platform default.
   Transcript (sessions/messages):
       Free       = 30 days
       Pro        = 365 days (1 year)
       Enterprise = 2555 days (7 years)
   Summary (TIER_SUMMARY_RETENTION_DAYS):
       Free       = 90 days
       Pro        = 365 days
       Enterprise = 2555 days

3. Enterprise channel-matrix decision: voice/WhatsApp NOT added to the
   tier-gate set in this release (documented in entitlements.py; the
   adapters don't exist so adding the gate would mislead admins).

4. RBAC vocab reconciliation: NO CODE CHANGE — doc note only.
   See RESULT_tierDE_entitlements.md §RBAC.

NOTE: Tests that previously asserted 99.5 / 99.9 or flat 730-day defaults
do not exist in the codebase — the prior values were only in the entitlement
map itself, not pinned in tests. This file adds the first explicit pins for
the corrected values, establishing them as the new ground truth.
"""
from __future__ import annotations

from unittest.mock import MagicMock


# =========================================================================
# 1. Uptime SLA corrections
# =========================================================================

def test_pro_uptime_sla_pct_is_99_9() -> None:
    """Pro uptime SLA must be 99.9% per Vision §7 / §5.6 / §9 item 6.

    The prior value (99.5) encoded a drift between the code and the
    contractual/publicly-stated SLA. Corrected in RESCAN TIER-DE(ent).
    """
    from app.policy.entitlements import TIER_ENTITLEMENTS, TIER_PRO

    assert TIER_ENTITLEMENTS[TIER_PRO].uptime_sla_pct == 99.9, (
        "Pro uptime_sla_pct must be 99.9 per Vision §7 / §5.6 / §9 item 6. "
        "If this test fails, check that the RESCAN TIER-DE(ent) correction "
        "was applied to app/policy/entitlements.py."
    )


# test_enterprise_uptime_sla_pct_is_99_95 removed: Enterprise tier excised in Unit 1.


def test_free_uptime_sla_pct_is_none() -> None:
    """Free tier has no SLA (best-effort); must remain None (unchanged)."""
    from app.policy.entitlements import TIER_ENTITLEMENTS, TIER_FREE

    assert TIER_ENTITLEMENTS[TIER_FREE].uptime_sla_pct is None, (
        "Free tier uptime_sla_pct must remain None (no SLA / best-effort)."
    )


# =========================================================================
# 2. Per-tier transcript retention — resolve_retention_days()
# =========================================================================

def test_free_transcript_retention_is_30_days() -> None:
    """Free tier: sessions/messages must resolve to 30 days (Architecture §3.4.10)."""
    from app.policy.retention_rules import resolve_retention_days

    for cat in ("sessions", "messages"):
        result = resolve_retention_days(
            data_category=cat,
            tier="free",
            tenant_override_days=None,
            platform_default_days=730,  # old flat default
        )
        assert result == 30, (
            f"Free tier {cat!r} must resolve to 30 days (Architecture §3.4.10). "
            f"Got {result}. Tier default must beat the platform default."
        )


def test_pro_transcript_retention_is_365_days() -> None:
    """Pro tier: sessions/messages must resolve to 365 days (Architecture §3.4.10)."""
    from app.policy.retention_rules import resolve_retention_days

    for cat in ("sessions", "messages"):
        result = resolve_retention_days(
            data_category=cat,
            tier="pro",
            tenant_override_days=None,
            platform_default_days=730,
        )
        assert result == 365, (
            f"Pro tier {cat!r} must resolve to 365 days (Architecture §3.4.10). "
            f"Got {result}."
        )


# test_enterprise_transcript_retention removed: Enterprise tier excised in Unit 1.


# =========================================================================
# 3. Tenant override always wins (layer 1 > layer 2 > layer 3)
# =========================================================================

def test_tenant_override_beats_tier_default() -> None:
    """Layer 1 (tenant override) must beat layer 2 (tier default).

    If a tenant has explicitly set a custom retention period, that value
    is contractual/compliance-driven and must not be silently replaced by
    the tier default.
    """
    from app.policy.retention_rules import resolve_retention_days

    # Pro tier default for sessions is 365, but tenant has set 180.
    result = resolve_retention_days(
        data_category="sessions",
        tier="pro",
        tenant_override_days=180,
        platform_default_days=730,
    )
    assert result == 180, (
        "Tenant override (180) must beat tier default (365). "
        f"Got {result}."
    )


def test_tenant_override_beats_platform_default() -> None:
    """Layer 1 also beats layer 3 when no tier-default exists for the category."""
    from app.policy.retention_rules import resolve_retention_days

    # memory_items has no tier-default; tenant has set 60.
    result = resolve_retention_days(
        data_category="memory_items",
        tier="pro",
        tenant_override_days=60,
        platform_default_days=365,
    )
    assert result == 60, (
        "Tenant override (60) must beat platform default (365). "
        f"Got {result}."
    )


def test_tier_default_beats_platform_default() -> None:
    """Layer 2 (tier default) must beat layer 3 (platform default).

    This is the core RESCAN TIER-DE(ent) change: for transcript categories,
    the tier's per-spec value (e.g. 30d for Free) must be used instead of
    the flat 730-day platform seed.
    """
    from app.policy.retention_rules import resolve_retention_days

    # Free tier with no tenant override; platform default is 730 (old flat).
    result = resolve_retention_days(
        data_category="sessions",
        tier="free",
        tenant_override_days=None,
        platform_default_days=730,
    )
    assert result == 30, (
        "Free tier sessions: tier default (30) must beat platform default (730). "
        f"Got {result}. This is the core RESCAN TIER-DE(ent) change."
    )


def test_platform_default_used_for_non_tier_categories() -> None:
    """Layer 3 (platform default) is used when no tier-default exists for a category.

    Categories like memory_items, traces, knowledge_chunks are NOT in
    TIER_RETENTION_DEFAULTS; they fall through to the platform default.
    """
    from app.policy.retention_rules import resolve_retention_days

    for cat in ("memory_items", "traces", "knowledge_chunks"):
        result = resolve_retention_days(
            data_category=cat,
            tier="free",
            tenant_override_days=None,
            platform_default_days=365,
        )
        assert result == 365, (
            f"Category {cat!r} has no tier default; platform default (365) must "
            f"be used. Got {result}."
        )


def test_no_tier_falls_through_to_platform_default() -> None:
    """When tier=None, the tier-default layer is skipped entirely."""
    from app.policy.retention_rules import resolve_retention_days

    result = resolve_retention_days(
        data_category="sessions",
        tier=None,
        tenant_override_days=None,
        platform_default_days=730,
    )
    assert result == 730, (
        "With tier=None the platform default must be used. Got {result}."
    )


def test_resolve_returns_none_when_no_layer_matches() -> None:
    """When no layer yields a value, resolve_retention_days returns None."""
    from app.policy.retention_rules import resolve_retention_days

    result = resolve_retention_days(
        data_category="sessions",
        tier=None,
        tenant_override_days=None,
        platform_default_days=None,
    )
    assert result is None


# =========================================================================
# 4. Summary retention constants (Architecture §3.4.10)
# =========================================================================

def test_summary_retention_free_is_90_days() -> None:
    """Free tier summary retention must be 90 days (Architecture §3.4.10)."""
    from app.policy.retention_rules import TIER_SUMMARY_RETENTION_DAYS

    assert TIER_SUMMARY_RETENTION_DAYS["free"] == 90, (
        "Free tier summary retention must be 90 days per Architecture §3.4.10."
    )


def test_summary_retention_pro_is_365_days() -> None:
    """Pro tier summary retention must be 365 days (Architecture §3.4.10)."""
    from app.policy.retention_rules import TIER_SUMMARY_RETENTION_DAYS

    assert TIER_SUMMARY_RETENTION_DAYS["pro"] == 365, (
        "Pro tier summary retention must be 365 days per Architecture §3.4.10."
    )


# test_summary_retention_enterprise removed: Enterprise tier excised in Unit 1.


# =========================================================================
# 5. TIER_RETENTION_DEFAULTS shape validation
# =========================================================================

def test_tier_retention_defaults_all_tiers_present() -> None:
    """TIER_RETENTION_DEFAULTS must have entries for Free and Pro."""
    from app.policy.retention_rules import TIER_RETENTION_DEFAULTS

    for tier in ("free", "pro"):
        assert tier in TIER_RETENTION_DEFAULTS, (
            f"TIER_RETENTION_DEFAULTS must contain an entry for tier {tier!r}."
        )


def test_tier_retention_defaults_transcript_categories_present() -> None:
    """Each tier must have both sessions and messages in TIER_RETENTION_DEFAULTS."""
    from app.policy.retention_rules import TIER_RETENTION_DEFAULTS

    for tier in ("free", "pro"):
        for cat in ("sessions", "messages"):
            assert cat in TIER_RETENTION_DEFAULTS[tier], (
                f"TIER_RETENTION_DEFAULTS[{tier!r}] must contain {cat!r}."
            )


# =========================================================================
# 6. RetentionRepository.get_effective_retention_days — unit test
#    (uses mocked DB session, no Postgres needed)
# =========================================================================

def _make_mock_policy(retention_days: int, admin_id=None) -> MagicMock:
    p = MagicMock()
    p.retention_days = retention_days
    p.admin_id = admin_id
    p.active = True
    return p


def test_repo_get_effective_returns_tenant_override_when_present() -> None:
    """Layer 1: tenant-specific DB row is returned when present."""
    from app.repositories.retention_repository import RetentionRepository

    tenant_policy = _make_mock_policy(180, admin_id="admin-1")
    platform_policy = _make_mock_policy(730, admin_id=None)

    mock_db = MagicMock()
    # scalars().first() returns different values depending on call order.
    # First call: tenant row; second call: platform default.
    mock_db.scalars.return_value.first.side_effect = [tenant_policy, platform_policy]

    repo = RetentionRepository(mock_db)
    result = repo.get_effective_retention_days(
        data_category="sessions",
        admin_id="admin-1",
        tier="free",
    )
    # Tenant override (180) beats tier default (30) and platform (730).
    assert result == 180, (
        f"Tenant override (180) must win over tier default and platform. Got {result}."
    )


def test_repo_get_effective_returns_tier_default_when_no_tenant_row() -> None:
    """Layer 2: tier default is returned when no tenant row exists."""
    from app.repositories.retention_repository import RetentionRepository

    platform_policy = _make_mock_policy(730, admin_id=None)

    mock_db = MagicMock()
    # No tenant row (returns None), then platform default.
    mock_db.scalars.return_value.first.side_effect = [None, platform_policy]

    repo = RetentionRepository(mock_db)
    # Free tier: sessions tier default is 30, not 730.
    result = repo.get_effective_retention_days(
        data_category="sessions",
        admin_id="admin-1",
        tier="free",
    )
    assert result == 30, (
        f"Free tier sessions: tier default (30) must beat platform default (730). "
        f"Got {result}."
    )


def test_repo_get_effective_returns_platform_default_for_non_tier_category() -> None:
    """Layer 3: platform default used for categories without a tier-default."""
    from app.repositories.retention_repository import RetentionRepository

    platform_policy = _make_mock_policy(365, admin_id=None)

    mock_db = MagicMock()
    # No tenant row, then platform default.
    mock_db.scalars.return_value.first.side_effect = [None, platform_policy]

    repo = RetentionRepository(mock_db)
    # memory_items has no entry in TIER_RETENTION_DEFAULTS.
    result = repo.get_effective_retention_days(
        data_category="memory_items",
        admin_id="admin-1",
        tier="pro",
    )
    assert result == 365, (
        f"memory_items has no tier default; platform default (365) must be used. "
        f"Got {result}."
    )


# =========================================================================
# 7. Enterprise channel-matrix — voice/WhatsApp NOT in gate set
# =========================================================================

def test_pro_channel_gate_does_not_include_voice_or_whatsapp() -> None:
    """Voice and WhatsApp must NOT be in the Pro (or Free) tier-gate set.

    Enterprise removed in Unit 1 excision. The guard remains for Pro:
    deferred channels (voice/WhatsApp) must not appear in any tier's gate.
    """
    from app.policy.entitlements import channels_available, TIER_PRO, TIER_FREE

    for tier in (TIER_FREE, TIER_PRO):
        gate = channels_available(tier)
        assert "voice" not in gate, (
            f"DECISION GUARD: 'voice' must NOT be in the {tier} channel gate "
            "until the voice adapter ships (v2-deferred)."
        )
        assert "whatsapp" not in gate, (
            f"DECISION GUARD: 'whatsapp' must NOT be in the {tier} channel gate "
            "until the WhatsApp adapter ships (post-v1)."
        )
