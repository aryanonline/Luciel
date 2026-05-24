"""Tier-entitlement matrix — v2-only post Arc 5 B8 (3-tier Option A).

The v1 4-tier surface (TIER_INDIVIDUAL/TEAM/COMPANY, Entitlement /
Dimension dataclasses, _individual_set/_team_set/_company_set factories,
ENTITLEMENTS_BY_TIER map, get_entitlement/is_enforced lookups) was
DELETED outright at Arc 5 Commit 17 (B8) per the aggressive-cleanup
amendment (docs/DRIFTS.md
D-arc5-aggressive-cleanup-doctrine-amendment-2026-05-23).

The v2 surface below is the sole entitlement-resolution path in the
post-Arc-5 platform:

* TIER_FREE / TIER_PRO / TIER_ENTERPRISE — the three tiers (Option A,
  founder-locked 2026-05-23 at arc5-out/A-arc5-arc4-plan-defects.md
  §6.5).
* TierEntitlement dataclass — the 16-axis per-tier shape (CANONICAL §14;
  arc4-out/A-tier-matrix-detail.md §18.2).
* TIER_ENTITLEMENTS map — static per-tier values.
* resolve_entitlement(tier, axis, overrides) — fail-closed lookup with
  Enterprise-only override hook (overrides param mirrors a row from the
  admin_tier_overrides table created at Revision A and populated at
  Revision A+B; an Enterprise Admin with no override row falls through
  to the static map).
* get_tier_entitlement(tier) — convenience accessor for the full row.

See also:
  * CANONICAL_RECAP §14 — buyer-facing matrix.
  * CANONICAL_RECAP §11.7 — public tier positioning copy.
  * arc4-out/A-tier-matrix-detail.md §18 — engineering-facing detail.
  * arc5-out/A-arc5-arc4-plan-defects.md §6.5 — Option A founder-locks.
  * alembic/versions/arc5_a_admin_instance_additive.py — the
    admin_tier_overrides schema that backs the override hook.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# =====================================================================
# v2 SURFACE (current truth, 3-tier Option A, Arc 5)
# =====================================================================
# Sole entitlement-resolution path post-B8. The override hook
# (resolve_entitlement(overrides=dict)) consumes admin_tier_overrides
# rows for Enterprise Admins; Free + Pro Admins never carry an override
# row (the absence of a row is the canonical "static map applies"
# signal).

# Tier constants (v2). String literals match the DB CHECK that Revision C
# tightens to ('free','pro','enterprise').
TIER_FREE = "free"
TIER_PRO = "pro"
TIER_ENTERPRISE = "enterprise"
ALL_TIERS_V2: tuple[str, ...] = (TIER_FREE, TIER_PRO, TIER_ENTERPRISE)

# Billing-model constants (v2). Backs ``subscriptions.billing_model``
# (column added at Arc 5 Revision A per ARCHITECTURE §3.2.14). Free has
# no ``subscriptions`` row at all; Pro and Enterprise are both flat
# (Arc 7 doctrine pivot 2026-05-24 retired the Enterprise hybrid shape
# in favour of flat-recurring symmetric self-serve with abuse-prevention
# caps in entitlements). ``BILLING_MODEL_HYBRID`` and
# ``BILLING_MODEL_CONSUMPTION`` remain defined as historical/reserved
# constants but no tier in the v2 map references them; the
# ``subscriptions.billing_model`` column itself is retired at Arc 7
# Commit 2 via Alembic migration. ``ALL_BILLING_MODELS`` keeps every
# constant for downstream validators that may still see the legacy
# column in row-level snapshots until the migration lands.
BILLING_MODEL_FLAT = "flat"
BILLING_MODEL_HYBRID = "hybrid"  # RETIRED at Arc 7; not used by any tier
BILLING_MODEL_CONSUMPTION = "consumption"  # reserved; not used by any tier
ALL_BILLING_MODELS: tuple[str, ...] = (
    BILLING_MODEL_FLAT,
    BILLING_MODEL_HYBRID,
    BILLING_MODEL_CONSUMPTION,
)

# Support-SLA literal labels (v2). Free has no SLA; Pro is 48h email;
# Enterprise is 24h email + dedicated CSM (contract may negotiate up).
SUPPORT_SLA_COMMUNITY = "community"
SUPPORT_SLA_EMAIL_48H = "email_48h"
SUPPORT_SLA_EMAIL_24H_PLUS_CSM = "email_24h_plus_csm"


@dataclass(frozen=True)
class TierEntitlement:
    """One tier's complete v2 entitlement row.

    Every field's semantics, units, and Option A locked value are
    documented at ``arc4-out/A-tier-matrix-detail.md`` \u00a718.2. Optional
    ``int | None`` fields use ``None`` to mean *unlimited* (typically
    Enterprise, sometimes negotiated via ``admin_tier_overrides``);
    ``bool`` fields are explicit; ``frozenset`` and ``tuple`` fields are
    immutable so the dataclass remains hashable and the per-tier map
    is fully frozen at module import.
    """

    # Axis 1 -- Instance count
    instance_count_cap: int | None

    # Axis 2 -- Leads per month (per Admin, summed across all Instances)
    leads_per_month_cap: int | None

    # Axis 3 -- Model tier per Instance ("base" / "mid" / "top")
    model_tier_default: str

    # Axis 4 -- Composition (Instance-to-Instance within an Admin)
    composition_enabled: bool
    max_composition_depth: int | None
    knowledge_share_grants_enabled: bool

    # Axis 5 -- API access
    api_enabled: bool
    api_rate_limit_rpm: int
    embed_key_count_cap: int | None

    # Axis 6 -- Roles and seats (admin-team dashboard logins at
    # admin-account scope -- Meaning 1, NOT per-Instance, locked
    # 2026-05-23)
    seat_cap: int | None
    delegated_admin_enabled: bool

    # Axis 7 -- Dashboard views
    dashboard_views: frozenset[str]

    # Axis 8 -- Audit retention (days; None = unlimited / contract)
    audit_retention_days: int | None

    # Axis 9 -- SSO
    sso_enabled: bool

    # Axis 10 -- Custom widget branding + custom-domain CNAME
    # (CNAME row is the NEW Axis 5 sub-row landed at
    # arc4-out/A-tier-matrix-detail.md \u00a711.2, backed by the new
    # admin_widget_domains table at Arc 6 -- NOT the legacy domains
    # table dropped at Arc 5).
    widget_branding_custom: bool
    widget_custom_domain_cname_cap: int | None

    # Axis 11 -- Webhook outbound
    webhook_outbound_enabled: bool

    # Axis 12 -- Cross-Instance memory federation
    cross_instance_memory_federation: bool

    # Axis 13 -- SLA
    uptime_sla_pct: float | None
    support_sla: str

    # Axis 14 -- Data residency
    data_residency_region: str

    # Axis 15 -- Export
    export_csv_enabled: bool
    export_audit_chain_enabled: bool

    # Stripe customer record requirement (Gap 1 resolution at
    # arc5-out/A-arc5-arc4-plan-defects.md \u00a76.4 -- Free has
    # admins.stripe_customer_id NULL, lazy-created on upgrade).
    stripe_customer_record_required: bool

    # Axis 16 -- Billing model (backs ``subscriptions.billing_model``
    # added at Arc 5 Revision A per ARCHITECTURE \u00a73.2.14).
    #
    # Free is populated as ``flat`` even though Free Admins have no
    # ``subscriptions`` row at all (Gap 1 resolution): the field
    # describes the *upgrade-shape* the Admin takes on conversion to a
    # paid tier, NOT the live DB state. Treat the static map as the
    # buyer-facing pricing shape; check ``stripe_customer_record_required``
    # to know whether a ``subscriptions`` row is expected to exist.
    billing_model: str


# Per-tier v2 entitlement map. Numeric values are the founder-locked
# Option A 2026-05-23 table at
# ``arc5-out/A-arc5-arc4-plan-defects.md`` \u00a76.5. Every value below
# mirrors the engineering-facing spec at
# ``arc4-out/A-tier-matrix-detail.md`` \u00a718.2 -- when the spec and this
# map diverge, **this map wins** and the spec is corrected to match.
TIER_ENTITLEMENTS: dict[str, TierEntitlement] = {
    TIER_FREE: TierEntitlement(
        # 2026-05-23 revision: leads 10\u2192100, API disabled\u2192enabled at
        # 30rpm, embed keys 0\u21921 (more generous Free per Option A).
        instance_count_cap=1,
        leads_per_month_cap=100,
        model_tier_default="base",
        composition_enabled=False,
        max_composition_depth=0,
        knowledge_share_grants_enabled=False,
        api_enabled=True,  # escalates D-free-tier-captcha-missing to P1
        api_rate_limit_rpm=30,
        embed_key_count_cap=1,
        seat_cap=1,  # admin-team dashboard logins, admin-account scope
        delegated_admin_enabled=False,
        dashboard_views=frozenset({"single_instance"}),
        audit_retention_days=30,
        sso_enabled=False,
        widget_branding_custom=False,
        widget_custom_domain_cname_cap=0,
        webhook_outbound_enabled=False,
        cross_instance_memory_federation=False,
        uptime_sla_pct=None,
        support_sla=SUPPORT_SLA_COMMUNITY,
        data_residency_region="ca-central-1",
        export_csv_enabled=False,
        export_audit_chain_enabled=False,
        stripe_customer_record_required=False,  # Gap 1: NULL until upgrade
        billing_model=BILLING_MODEL_FLAT,  # aspirational; no subs row exists
    ),
    TIER_PRO: TierEntitlement(
        # 2026-05-23 revision: instances 3\u219210, leads 2000\u21925000,
        # API 60\u2192300rpm, embed keys 3\u219210, seats 5\u219225 (strict
        # expansion per Option A; opens
        # D-pro-tier-rate-limit-abuse-surface-2026-05-23 P1).
        instance_count_cap=10,
        leads_per_month_cap=5000,
        model_tier_default="mid",
        composition_enabled=True,
        max_composition_depth=2,
        knowledge_share_grants_enabled=False,
        api_enabled=True,
        api_rate_limit_rpm=300,
        embed_key_count_cap=10,
        seat_cap=25,  # admin-team dashboard logins (Meaning 1)
        delegated_admin_enabled=False,
        dashboard_views=frozenset(
            {"single_instance", "instance_group", "admin_rollup"}
        ),
        audit_retention_days=365,
        sso_enabled=False,
        widget_branding_custom=False,
        widget_custom_domain_cname_cap=1,  # e.g. chat.theircompany.com
        webhook_outbound_enabled=True,
        cross_instance_memory_federation=False,
        uptime_sla_pct=99.5,
        support_sla=SUPPORT_SLA_EMAIL_48H,
        data_residency_region="ca-central-1",
        export_csv_enabled=True,
        export_audit_chain_enabled=False,
        stripe_customer_record_required=True,
        billing_model=BILLING_MODEL_FLAT,
    ),
    TIER_ENTERPRISE: TierEntitlement(
        # 2026-05-24 Arc 7 doctrine pivot: Enterprise is now FLAT-recurring
        # symmetric with Pro (monthly $2,800 CAD or annual $24,000 CAD,
        # self-serve via Stripe Checkout). Metering RETIRED -- abuse-prevention
        # caps (leads_per_month_cap=50000, api_rate_limit_rpm=3000,
        # embed_key_count_cap=100, instance_count_cap=None=unlimited)
        # replace the previous unlimited+metered shape. Overflow beyond
        # 50k leads/month routes to enterprise_overflow_archive (Commit 5)
        # rather than billing a metered charge. Every field below remains
        # overrideable per-Admin via admin_tier_overrides (Arc 5 Revision A)
        # for sales-negotiated contracts above the self-serve ceiling.
        instance_count_cap=None,  # unlimited (self-serve default; override per contract)
        leads_per_month_cap=50000,  # Arc 7 abuse cap; overflow -> archive, no metering
        model_tier_default="top",
        composition_enabled=True,
        max_composition_depth=None,  # unlimited
        knowledge_share_grants_enabled=True,
        api_enabled=True,
        api_rate_limit_rpm=3000,  # 10x Pro; abuse ceiling (overrideable)
        embed_key_count_cap=100,  # Arc 7 abuse cap (10x Pro); overrideable
        seat_cap=None,  # unlimited admin-team dashboard logins
        delegated_admin_enabled=True,
        dashboard_views=frozenset(
            {
                "single_instance",
                "instance_group",
                "admin_rollup",
            }
        ),
        audit_retention_days=None,  # unlimited (typically 7y per contract)
        sso_enabled=True,
        widget_branding_custom=True,
        widget_custom_domain_cname_cap=None,  # unlimited CNAMEs
        webhook_outbound_enabled=True,
        cross_instance_memory_federation=True,
        uptime_sla_pct=99.9,
        support_sla=SUPPORT_SLA_EMAIL_24H_PLUS_CSM,
        data_residency_region="ca-central-1",
        export_csv_enabled=True,
        export_audit_chain_enabled=True,
        stripe_customer_record_required=True,
        billing_model=BILLING_MODEL_FLAT,  # Arc 7 doctrine: symmetric with Pro
    ),
}

assert set(TIER_ENTITLEMENTS.keys()) == set(ALL_TIERS_V2), (
    "TIER_ENTITLEMENTS keys must equal ALL_TIERS_V2 -- see "
    "arc4-out/A-tier-matrix-detail.md \u00a718.2 and "
    "arc5-out/A-arc5-arc4-plan-defects.md \u00a76.5."
)


def resolve_entitlement(
    *,
    tier: str,
    axis: str,
    overrides: dict[str, Any] | None = None,
) -> Any:
    """Resolve a single v2 entitlement value for an Admin.

    Per ``arc4-out/A-tier-matrix-detail.md`` \u00a718.3 algorithm:

    1. Read the static v2 map: ``value = TIER_ENTITLEMENTS[tier].<axis>``.
    2. If ``tier == TIER_ENTERPRISE`` and ``overrides`` is not None and
       ``axis in overrides`` and ``overrides[axis] is not None``, return
       the override value.
    3. Otherwise return the static value.

    The ``overrides`` parameter mirrors a row from the
    ``admin_tier_overrides`` table that lands at Arc 5 Revision A; until
    Revision A is on prod, callers pass ``overrides=None`` and the
    static map applies.

    Fail-closed posture: a missing axis raises ``AttributeError`` rather
    than silently returning a permissive default -- callers must
    reference an axis name that exists on the ``TierEntitlement``
    dataclass.

    Raises ``KeyError`` if ``tier`` is unknown -- callers should
    validate ``tier in ALL_TIERS_V2`` before calling.
    """
    static_value = getattr(TIER_ENTITLEMENTS[tier], axis)

    if (
        tier == TIER_ENTERPRISE
        and overrides is not None
        and axis in overrides
        and overrides[axis] is not None
    ):
        return overrides[axis]

    return static_value


def get_tier_entitlement(tier: str) -> TierEntitlement:
    """Return the full ``TierEntitlement`` row for a v2 tier.

    Convenience accessor for callers that need to read several axes at
    once (e.g. rendering a tier-summary card or building a buyer-facing
    feature list). Raises ``KeyError`` if ``tier`` is unknown.
    """
    return TIER_ENTITLEMENTS[tier]
