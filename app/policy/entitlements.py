"""Tier-entitlement matrix — v2-only post Arc 5 B8 (3-tier Option A).

The v1 4-tier surface (TIER_INDIVIDUAL/TEAM/COMPANY, Entitlement /
Dimension dataclasses, _individual_set/_team_set/_company_set factories,
ENTITLEMENTS_BY_TIER map, get_entitlement/is_enforced lookups) was
DELETED outright at Arc 5 Commit 17 (B8) per the aggressive-cleanup
amendment The v2 surface below is the sole entitlement-resolution path in the
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

    # Axis 4 -- Composition (Instance-to-Instance within an Admin).
    # ``max_composition_depth`` RETIRED at Arc 12 WU1 (2026-05-28):
    # contradicted locked Decision #19 ("no depth limit, no edge cap
    # on the customer-facing composition graph"). The field was also
    # consumed nowhere -- removal is structural cleanup, not a
    # behaviour change. ``composition_enabled`` remains as the
    # §3.3.4 master switch (free=False, pro=True, enterprise=True);
    # cycle detection + per-inbound fan-out budget (WU5) replace
    # depth-bounding as the only runtime guardrails. The
    # corresponding ``admin_tier_overrides.max_composition_depth_override``
    # column stops being read by code post-WU1 (deferred drop with
    # the Arc 12 schema sweep).
    composition_enabled: bool
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

    # Axis 8c -- Per-file knowledge byte cap (Arc 11 Step 7).
    # Vision §3.3 / ARC11_PLAN.md §0.4: each individual upload is also
    # capped, separately from the per-Admin total. Enforced at the API
    # boundary; over-cap uploads return 413 with the structured payload
    # in ARC11_PLAN.md §0.4 carrying ``scope: "per_file"``.
    #   Free       :  10 MB
    #   Pro        :  50 MB
    #   Enterprise : 500 MB
    knowledge_per_file_bytes_cap: int

    # Axis 8d -- Website crawl ingestion enabled (Arc 11 Step 7).
    # Vision §3.3 lists website crawl as a Pro/Enterprise feature; the
    # /crawl route returns 403 ``feature_not_available_on_tier`` for
    # Free per ARC11_PLAN.md §3.6.
    knowledge_website_crawl_enabled: bool

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

    # Axis 17 (Arc 12b) -- custom-role authoring (permission-based
    # team-member role model, Architecture \u00a73.7.2). Free/Pro use the
    # four locked roles with their default permission sets; Enterprise
    # adds a layer of admin-composed custom roles built from atomic
    # permissions. This axis is TRUE on Enterprise and FALSE on the
    # other two tiers. The role-authoring API rejects writes (403)
    # for tenants whose tier does not enable this axis. Zero behavioural
    # change on Free/Pro: the locked-role permission seed reproduces
    # today's role matrix exactly.
    custom_role_authoring_enabled: bool

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
        # Arc 11 Step 7: 10 MB per-file cap; no crawl on Free.
        knowledge_per_file_bytes_cap=10 * 1024 * 1024,
        knowledge_website_crawl_enabled=False,
        model_tier_default="base",
        composition_enabled=False,
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
        # Arc 12b: Free uses the four locked roles only.
        custom_role_authoring_enabled=False,
    ),
    TIER_PRO: TierEntitlement(
        # 2026-05-23 revision: instances 3\u219210, leads 2000\u21925000,
        # API 60\u2192300rpm, embed keys 3\u219210, seats 5\u219225 (strict
        # expansion per Option A; opens
        # D-pro-tier-rate-limit-abuse-surface-2026-05-23 P1).
        instance_count_cap=10,
        # Arc 10: 5 GB per Vision §7 tier matrix.
        knowledge_bytes_cap=5 * 1024 * 1024 * 1024,
        # Arc 11 Step 7: 50 MB per-file cap; crawl enabled.
        knowledge_per_file_bytes_cap=50 * 1024 * 1024,
        knowledge_website_crawl_enabled=True,
        model_tier_default="mid",
        composition_enabled=True,
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
        # Arc 12b: Pro uses the four locked roles only.
        custom_role_authoring_enabled=False,
    ),
    TIER_ENTERPRISE: TierEntitlement(
        # Arc 10: unlimited per Vision §7 tier matrix.
        knowledge_bytes_cap=None,
        # Arc 11 Step 7: 500 MB per-file cap; crawl enabled.
        knowledge_per_file_bytes_cap=500 * 1024 * 1024,
        knowledge_website_crawl_enabled=True,
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
        # Arc 12b: Enterprise unlocks admin-composed custom roles.
        custom_role_authoring_enabled=True,
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


# ---------------------------------------------------------------------
# Arc 13 — channel-availability derivations.
#
# Per Vision §7 tier matrix, channel access is tier-gated:
#   Free       : {widget}
#   Pro        : {widget, email, sms}
#   Enterprise : {widget, email, sms}
#
# The widget is the entitlement floor — every tier has it, and it needs
# no provisioning (the embed key is the binding). Email + SMS unlock on
# the paying tiers; whether a given Instance has them STRUCTURALLY
# enabled is a separate question answered by the per-instance
# enabled_channels column (Arc 13 D4) — this function answers only the
# tier-level "is this channel allowed to be enabled at all?" gate.
#
# These are DERIVATIONS, not new TierEntitlement fields. The dataclass
# is frozen (adding a field breaks every TierEntitlement(...) call-site
# and the founder-locked surface), so the channel matrix lives here as
# a function — same discipline as the per-bucket rate-limit derivations
# above.

CHANNEL_WIDGET = "widget"
CHANNEL_EMAIL = "email"
CHANNEL_SMS = "sms"

# Static per-tier channel matrix (Vision §7). Frozen sets so the map is
# fully immutable at module import.
_CHANNELS_BY_TIER: dict[str, frozenset[str]] = {
    TIER_FREE: frozenset({CHANNEL_WIDGET}),
    TIER_PRO: frozenset({CHANNEL_WIDGET, CHANNEL_EMAIL, CHANNEL_SMS}),
    TIER_ENTERPRISE: frozenset({CHANNEL_WIDGET, CHANNEL_EMAIL, CHANNEL_SMS}),
}


def channels_available(tier: str) -> frozenset[str]:
    """Return the set of channel ids a tier is allowed to enable.

    Vision §7 tier matrix:
        * Free       -> {widget}
        * Pro        -> {widget, email, sms}
        * Enterprise -> {widget, email, sms}

    This is the TIER gate (is the channel allowed at all on this tier?),
    distinct from per-Instance structural enablement (the
    ``instances.enabled_channels`` column read by
    ``_instance_channels_enabled``): a Pro Admin *may* enable SMS, but a
    specific Instance only routes SMS once a number is provisioned and
    the channel id lands in its enabled_channels set.

    Fail-closed: an unknown tier returns the Free set ({widget}) rather
    than raising, so a mis-tagged Admin can never gain email/sms access
    by accident. Mirrors the fail-closed-to-Free posture of
    ``_resolve_admin_tier``.
    """
    return _CHANNELS_BY_TIER.get(tier, _CHANNELS_BY_TIER[TIER_FREE])


def sms_dedicated_number_entitled(tier: str) -> bool:
    """Whether a tier is entitled to a DEDICATED SMS number per Instance.

    Vision §7 dedicated-number policy:
        * Free       -> False (no SMS channel at all)
        * Pro        -> True  (dedicated number per Instance when SMS is
                              enabled)
        * Enterprise -> True  (dedicated number per Instance; PLUS the
                              deferred brokerage-routing flag — see
                              ``sms_brokerage_routing_flag``)

    Gates the ``sms_number_mode='dedicated'`` provisioning path. A tier
    that returns False must not be handed a dedicated number; if it has
    no SMS entitlement at all (Free) the question is moot but the
    function still returns False for a single source of truth.
    """
    if CHANNEL_SMS not in channels_available(tier):
        return False
    return tier in (TIER_PRO, TIER_ENTERPRISE)


def sms_brokerage_routing_flag(tier: str) -> bool:
    """Whether a tier carries the (deferred) SMS brokerage-routing flag.

    Enterprise-only. Brokerage routing — pooling/sharing numbers across
    Instances with a routing broker — is DEFERRED in Arc 13: this is a
    FLAG ONLY, surfaced so slice 2/3 and the provisioning UI can render
    the Enterprise affordance, but no brokerage routing is implemented
    yet. Pro gets dedicated-per-instance (flag False); Enterprise gets
    dedicated PLUS this brokerage capability flag set True.
    """
    return tier == TIER_ENTERPRISE and sms_dedicated_number_entitled(tier)
