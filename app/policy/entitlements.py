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

# Billing-model constants RETIRED at Arc 7 Commit 2 (2026-05-24).
# The Arc 7 doctrine pivot made every paying tier flat-recurring
# (Pro + Enterprise symmetric self-serve), so the enum had a single
# legal value and carried zero information. ``BILLING_MODEL_FLAT``,
# ``BILLING_MODEL_HYBRID``, ``BILLING_MODEL_CONSUMPTION``, and
# ``ALL_BILLING_MODELS`` are removed alongside the
# ``subscriptions.billing_model`` + ``admin_tier_overrides.billing_model``
# column drops in ``alembic/versions/arc7_a_retire_billing_model.py``.
# Path A ("whatever we ship out in our code and prod and schema must
# be aligned with this vision") forbids keeping unreachable shapes.

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

    # Axis 2 (Leads per month) RETIRED at Arc 7 Commit 5 (2026-05-24).
    # The previous ``leads_per_month_cap`` field was a metering ghost from
    # the pre-Arc-7 hybrid doctrine: under Arc 7's flat-recurring pivot we
    # bill a fixed monthly/annual price and never meter, so a monthly lead
    # count carries no billing meaning. The abuse surface it nominally
    # addressed (someone hammering the capture endpoint) is already closed
    # by ``api_rate_limit_rpm`` (Arc 7 Commit 4 tier-aware middleware): a
    # Free admin tops out at 30 rpm, Pro at 300 rpm, Enterprise at
    # 3,000 rpm -- rate is the boundary; monthly count is a value lever we
    # never actually wanted to lever (capping a paying customer for
    # converting well is anti-value). See Arc 7 doctrine note in
    # ``arc7-out/arc7-commit5-leads-cap-retirement-record.md``. The
    # corresponding ``admin_tier_overrides.leads_per_month_override``
    # column also stops being read by code post-C5 (deferred drop to keep
    # this commit code-only; alembic drop lands at Arc 8 schema sweep).

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

    # Axis 8b -- Knowledge byte cap per Admin (Arc 10).
    # Vision §7 tier matrix knowledge quotas:
    #   Free       :   100 MB
    #   Pro        : 5,000 MB (5 GB)
    #   Enterprise : None (unlimited)
    # The downgrade-archive 5th axis (AXIS_KNOWLEDGE) reads this cap
    # to decide which sources to LRU-archive at the downgrade boundary.
    knowledge_bytes_cap: int | None

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

    # Axis 16 (Billing model) RETIRED at Arc 7 Commit 2 (2026-05-24).
    # Every paying tier is flat-recurring under the Arc 7 doctrine
    # pivot, so the field carried zero information. See
    # ``alembic/versions/arc7_a_retire_billing_model.py`` for the
    # accompanying schema drop on ``subscriptions.billing_model`` +
    # ``admin_tier_overrides.billing_model``.


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
        # Arc 10: 100 MB per Vision §7 tier matrix.
        knowledge_bytes_cap=100 * 1024 * 1024,
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
    ),
    TIER_PRO: TierEntitlement(
        # 2026-05-23 revision: instances 3\u219210, leads 2000\u21925000,
        # API 60\u2192300rpm, embed keys 3\u219210, seats 5\u219225 (strict
        # expansion per Option A; opens
        # D-pro-tier-rate-limit-abuse-surface-2026-05-23 P1).
        instance_count_cap=10,
        # Arc 10: 5 GB per Vision §7 tier matrix.
        knowledge_bytes_cap=5 * 1024 * 1024 * 1024,
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
    ),
    TIER_ENTERPRISE: TierEntitlement(
        # Arc 10: unlimited per Vision §7 tier matrix.
        knowledge_bytes_cap=None,
        # 2026-05-24 Arc 7 doctrine pivot: Enterprise is now FLAT-recurring
        # symmetric with Pro (monthly $2,800 CAD or annual $24,000 CAD,
        # self-serve via Stripe Checkout). Metering RETIRED -- abuse-prevention
        # caps (api_rate_limit_rpm=3000, embed_key_count_cap=100,
        # instance_count_cap=None=unlimited) replace the previous
        # unlimited+metered shape. Arc 7 Commit 5 (2026-05-24) further
        # retired the leads_per_month_cap field entirely: rate-limit
        # middleware (Commit 4) is the abuse boundary, and a monthly
        # business-metric cap on a flat-recurring customer punishes
        # success without protecting anything new. Every field below
        # remains overrideable per-Admin via admin_tier_overrides
        # (Arc 5 Revision A) for sales-negotiated contracts above the
        # self-serve ceiling.
        instance_count_cap=None,  # unlimited (self-serve default; override per contract)
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


# ---------------------------------------------------------------------
# Arc 8 Commit 3 (WU-3 abuse-surface): per-bucket rate-limit derivations.
#
# Closes D-pro-tier-rate-limit-abuse-surface-2026-05-23.
#
# The 2026-05-23 Option-A revision lifted Pro from 60rpm \u2192 300rpm and
# embed-key cap 3 \u2192 10. That widened the abuse surface in two ways:
#
#   (a) An attacker holding one valid embed key for a Pro Admin could
#       burn the full 300rpm cap against that admin, starving the
#       other 9 embed keys (one buggy/leaked key takes the whole tier
#       allotment).
#   (b) An Admin running 10 Instances on Pro could see one buggy
#       Instance burn the full 300rpm cap, starving the other 9
#       (the per-instance bucket landed at Arc 7 Commit 4 already
#       closes the routing side of this; the per-CAP rpm value below
#       closes the entitlement-derivation side).
#
# The derivation rule is: floor-divide ``api_rate_limit_rpm`` by the
# per-Admin count cap, with a 1rpm floor so the bucket never
# completely closes (a single legitimate request from a single
# Instance/key must always have a token). ``None`` (unlimited) on the
# count cap means "do not subdivide" -- Enterprise Admins get the
# full ``api_rate_limit_rpm`` per Instance and per key, which matches
# the unlimited-instance Option-A promise.
#
# These are DERIVATIONS, not new dataclass fields. The dataclass is
# frozen and adding fields breaks every existing ``TierEntitlement(...)``
# call-site; derivations preserve the founder-locked surface while
# adding the per-bucket caps that the rate-limit middleware needs.


def per_instance_api_rate_limit_rpm(
    *,
    tier: str,
    overrides: dict[str, Any] | None = None,
) -> int:
    """Derive the per-Instance rpm bucket cap for one tier.

    Floor-divides ``api_rate_limit_rpm`` by ``instance_count_cap`` so
    no single Instance can starve siblings under the same Admin.
    ``instance_count_cap=None`` (Enterprise, unlimited) returns the
    full ``api_rate_limit_rpm`` -- subdividing by infinity would zero
    every bucket.

    Per-Admin numbers under the founder-locked Option A:
        * Free:       30rpm / 1 instance   = 30rpm per Instance
        * Pro:        300rpm / 10 instances = 30rpm per Instance
        * Enterprise: 3000rpm / unlimited  = 3000rpm per Instance (no subdivision)

    The 1rpm floor is a defence in case a future tier revision drops
    api_rate_limit_rpm below instance_count_cap -- the floor keeps the
    bucket open for one legitimate request rather than locking the
    Instance out entirely.
    """
    rpm = resolve_entitlement(tier=tier, axis="api_rate_limit_rpm", overrides=overrides)
    cap = resolve_entitlement(tier=tier, axis="instance_count_cap", overrides=overrides)
    if cap is None or int(cap) <= 0:
        return int(rpm)
    return max(1, int(rpm) // int(cap))


def per_key_api_rate_limit_rpm(
    *,
    tier: str,
    overrides: dict[str, Any] | None = None,
) -> int:
    """Derive the per-embed-key rpm bucket cap for one tier.

    Floor-divides ``api_rate_limit_rpm`` by ``embed_key_count_cap`` so
    no single embed key can starve siblings (i.e. a leaked or buggy
    key cannot burn the whole Admin allotment).
    ``embed_key_count_cap=None`` returns the full ``api_rate_limit_rpm``
    (no subdivision); 1rpm floor as in :func:`per_instance_api_rate_limit_rpm`.

    Per-Admin numbers under the founder-locked Option A:
        * Free:       30rpm / 1 embed key  = 30rpm per key
        * Pro:        300rpm / 10 keys     = 30rpm per key
        * Enterprise: 3000rpm / 100 keys   = 30rpm per key
    """
    rpm = resolve_entitlement(tier=tier, axis="api_rate_limit_rpm", overrides=overrides)
    cap = resolve_entitlement(tier=tier, axis="embed_key_count_cap", overrides=overrides)
    if cap is None or int(cap) <= 0:
        return int(rpm)
    return max(1, int(rpm) // int(cap))
