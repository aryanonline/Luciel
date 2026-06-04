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

    # Axis 18 (Rescan Tier-C) -- graph knowledge store (Architecture §3.2.1).
    # Vision §7 tier matrix: graph store is a Pro+Enterprise feature.
    # Free admins stay vector-only; Pro and Enterprise unlock the graph
    # ingestion-extraction path and the graph retriever (Decision #4:
    # PostgreSQL recursive CTEs, no external graph DB; Decision #5:
    # domain-agnostic node/edge types inferred at ingest; Decision #6:
    # graph retriever invoked only on structured-filter-intent queries).
    # The audit found this axis MISSING (Arc 16 not implemented).
    knowledge_graph_enabled: bool

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
        # Rescan Tier-C: graph store is Pro+Enterprise only (Vision §7).
        knowledge_graph_enabled=False,
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
        # RESCAN TIER-DE(ent): corrected from 99.5 -> 99.9 per Vision §7 /
        # §5.6 / §9 item 6. The prior value encoded the drift between the
        # code and the contractual SLA spec. §9 item 6 is now-implemented.
        uptime_sla_pct=99.9,
        support_sla=SUPPORT_SLA_EMAIL_48H,
        data_residency_region="ca-central-1",
        export_csv_enabled=True,
        export_audit_chain_enabled=False,
        stripe_customer_record_required=True,
        # Arc 12b: Pro uses the four locked roles only.
        custom_role_authoring_enabled=False,
        # Rescan Tier-C: graph store enabled on Pro.
        knowledge_graph_enabled=True,
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
        # RESCAN TIER-DE(ent): corrected from 99.9 -> 99.95 per Vision §7 /
        # §5.6 / §9 item 7. The prior value encoded the drift between the
        # code and the contractual SLA spec. §9 item 7 is now-implemented.
        uptime_sla_pct=99.95,
        support_sla=SUPPORT_SLA_EMAIL_24H_PLUS_CSM,
        data_residency_region="ca-central-1",
        export_csv_enabled=True,
        export_audit_chain_enabled=True,
        stripe_customer_record_required=True,
        # Arc 12b: Enterprise unlocks admin-composed custom roles.
        custom_role_authoring_enabled=True,
        # Rescan Tier-C: graph store enabled on Enterprise.
        knowledge_graph_enabled=True,
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
# RESCAN TIER-DE(ent) — Enterprise channel-matrix decision (voice/WhatsApp):
# Vision §7 lists "All channels (incl. voice, WhatsApp)" as an Enterprise
# capability. However, voice is v2-deferred and WhatsApp is post-v1; neither
# channel adapter is implemented. DECISION: DO NOT add voice/whatsapp to the
# tier-gate set in this release. Adding them to the gate while the adapter
# layer does not exist would allow an Enterprise admin to "enable" a channel
# that silently drops all traffic — misleading the customer and creating a
# false sense of functionality. The §7 "all channels" statement is aspirational
# for the Enterprise tier; it describes the end-state, not v1 ship scope.
# When the voice adapter ships (v2) and the WhatsApp adapter ships (post-v1),
# CHANNEL_VOICE and CHANNEL_WHATSAPP should be added to TIER_ENTERPRISE's
# frozenset below, the channels_available() docstring updated, and adapter-
# readiness validated before the gate change is merged.
# Doc-reconciliation note: Architecture §3.7.3 / Vision §7 channel matrix
# should be annotated: "voice: v2-deferred, whatsapp: post-v1; Enterprise
# tier-gate will be updated when each adapter ships."
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


# ---------------------------------------------------------------------
# Arc 15 — instance-config-pillar derivations (Vision §3.5 / §3.4).
#
# Same discipline as the channel + rate-limit derivations above: the
# TierEntitlement dataclass is frozen, so the per-tier policy for the
# Arc 15 configuration pillars lives here as functions rather than as
# new frozen fields. These answer the tier-gating questions the
# personality + escalation + create APIs ask.
# ---------------------------------------------------------------------

# business_context length cap (Vision §3.5): 280 chars Free+Pro,
# 2000 chars Enterprise. Capped at the API/Pydantic boundary, NOT the DB.
_BUSINESS_CONTEXT_CAP_BY_TIER: dict[str, int] = {
    TIER_FREE: 280,
    TIER_PRO: 280,
    TIER_ENTERPRISE: 2000,
}


def business_context_max_chars(tier: str) -> int:
    """Return the max ``business_context`` length for a tier.

    Fail-closed: an unknown tier gets the Free cap (280) so a mis-tagged
    Admin can never write the longer Enterprise context by accident.
    """
    return _BUSINESS_CONTEXT_CAP_BY_TIER.get(
        tier, _BUSINESS_CONTEXT_CAP_BY_TIER[TIER_FREE]
    )


def custom_personality_enabled(tier: str) -> bool:
    """Whether a tier may use the ``custom`` personality preset.

    Vision §3.5: all four NAMED presets are available on every tier;
    ``custom`` (direct axis authoring) is Pro/Enterprise only. Free is
    refused at the API with a 403. Fail-closed for unknown tiers.
    """
    return tier in (TIER_PRO, TIER_ENTERPRISE)


def lead_routing_enabled(tier: str) -> bool:
    """Whether a tier may configure ``lead_routing``.

    Journey Phase 3 (Marcus, Pro): lead routing is Pro/Enterprise only;
    Free instances leave it null. Fail-closed for unknown tiers.
    """
    return tier in (TIER_PRO, TIER_ENTERPRISE)


# Admin-notification channels for escalation contacts, per tier
# (Vision §3.4): Free=email; Pro=email+sms; Ent=+slack+custom.
ESCALATION_NOTIFY_EMAIL = "email"
ESCALATION_NOTIFY_SMS = "sms"
ESCALATION_NOTIFY_SLACK = "slack"
ESCALATION_NOTIFY_CUSTOM = "custom"

_ESCALATION_NOTIFY_CHANNELS_BY_TIER: dict[str, frozenset[str]] = {
    TIER_FREE: frozenset({ESCALATION_NOTIFY_EMAIL}),
    TIER_PRO: frozenset({ESCALATION_NOTIFY_EMAIL, ESCALATION_NOTIFY_SMS}),
    TIER_ENTERPRISE: frozenset(
        {
            ESCALATION_NOTIFY_EMAIL,
            ESCALATION_NOTIFY_SMS,
            ESCALATION_NOTIFY_SLACK,
            ESCALATION_NOTIFY_CUSTOM,
        }
    ),
}


def escalation_notify_channels(tier: str) -> frozenset[str]:
    """Return the admin-notification channels a tier may route escalation
    contacts through (Vision §3.4).

        * Free       -> {email}
        * Pro        -> {email, sms}
        * Enterprise -> {email, sms, slack, custom}

    Fail-closed: unknown tier returns the Free set.
    """
    return _ESCALATION_NOTIFY_CHANNELS_BY_TIER.get(
        tier, _ESCALATION_NOTIFY_CHANNELS_BY_TIER[TIER_FREE]
    )


def escalation_secondary_contact_enabled(tier: str) -> bool:
    """Whether a tier may configure a secondary escalation contact +
    per-signal routing rules (Pro/Enterprise; Free is primary_email only).
    """
    return tier in (TIER_PRO, TIER_ENTERPRISE)


def escalation_chains_enabled(tier: str) -> bool:
    """Whether a tier may configure ordered escalation chains with SLA
    minutes (Enterprise only — Vision §3.4)."""
    return tier == TIER_ENTERPRISE


# ---------------------------------------------------------------------
# Arc 18 — conversation-budget + overage axis (Vision §10 Doctrine
# Anchor; §7 tier table; Architecture §3.4.1b).
#
# entitlements.py is the NAMED canonical code for the budget/overage
# axis (Vision §10). The values below are founder-ratified 2026-06-03
# (resolving Vision §9 Open Decisions #9 & #10) — see
# ARC18_BACKEND_SPEC.md "RATIFIED VALUES".
#
# DOCTRINE NOTE (Arc 18 supersedes Arc 7): Arc 7 (2026-05-24) RETIRED
# metering in favour of flat-recurring pricing. Arc 18's founder
# ratification re-introduces a PER-INSTANCE conversation budget with a
# metered OVERAGE ADD-ON layered on top of the flat base subscription.
# This is a later, ratified doctrine decision — not a contradiction. The
# flat base price is unchanged; overage is an additional metered Stripe
# item billed only when an instance exceeds its budget.
#
# CADENCE-AWARE: Vision §7 treats Pro Monthly and Pro Annual as DISTINCT
# budget/rate rows (2000/$15-per-100 vs 2500/$10-per-100). The
# TierEntitlement dataclass has a single `pro` tier; the cadence
# (monthly/annual) lives on the Subscription row
# (subscriptions.billing_cadence). So these are DERIVATION FUNCTIONS
# keyed on (tier, cadence) — same discipline as the channel + rate-limit
# derivations above (the frozen dataclass is not widened).
#
# Money is in CENTS (int) to avoid float drift on currency.
# ---------------------------------------------------------------------

CADENCE_MONTHLY = "monthly"
CADENCE_ANNUAL = "annual"

# Per-(tier, cadence) conversation budget per instance per billing period.
# Free is cadence-independent (200, graceful cap). Enterprise baseline is
# 10,000 (negotiable UPWARD per MSA via admin_tier_overrides — see
# resolve_entitlement's Enterprise override hook).
_CONVERSATION_BUDGET: dict[tuple[str, str], int] = {
    (TIER_FREE, CADENCE_MONTHLY): 200,
    (TIER_FREE, CADENCE_ANNUAL): 200,
    (TIER_PRO, CADENCE_MONTHLY): 2000,
    (TIER_PRO, CADENCE_ANNUAL): 2500,
    (TIER_ENTERPRISE, CADENCE_MONTHLY): 10000,
    (TIER_ENTERPRISE, CADENCE_ANNUAL): 10000,
}

# Overage rate in CENTS per additional 100 conversations per instance.
# Free = None (graceful cap, no overage billing). Enterprise = None at
# the static layer (per-contract rate resolved via admin_tier_overrides;
# do NOT mint a fixed Stripe price for Enterprise — §35).
_OVERAGE_RATE_PER_100_CENTS: dict[tuple[str, str], int | None] = {
    (TIER_FREE, CADENCE_MONTHLY): None,
    (TIER_FREE, CADENCE_ANNUAL): None,
    (TIER_PRO, CADENCE_MONTHLY): 1500,  # $15.00 / 100
    (TIER_PRO, CADENCE_ANNUAL): 1000,  # $10.00 / 100
    (TIER_ENTERPRISE, CADENCE_MONTHLY): None,  # per-contract
    (TIER_ENTERPRISE, CADENCE_ANNUAL): None,  # per-contract
}

# Config key (app.core.config.settings attribute) holding the Stripe
# METERED overage Price id per (tier, cadence). Founder provisions the
# actual Stripe prices; code references them by these keys only (§ "Stripe
# config keys" in the report). Free has none; Enterprise has none (the
# overage subscription item is provisioned per-contract, not via a fixed
# platform price).
_OVERAGE_PRICE_CONFIG_KEY: dict[tuple[str, str], str | None] = {
    (TIER_FREE, CADENCE_MONTHLY): None,
    (TIER_FREE, CADENCE_ANNUAL): None,
    (TIER_PRO, CADENCE_MONTHLY): "stripe_price_overage_pro_monthly",
    (TIER_PRO, CADENCE_ANNUAL): "stripe_price_overage_pro_annual",
    (TIER_ENTERPRISE, CADENCE_MONTHLY): None,
    (TIER_ENTERPRISE, CADENCE_ANNUAL): None,
}


def _norm_cadence(cadence: str | None) -> str:
    """Fail-closed cadence normalisation. Unknown / None → monthly (the
    conservative budget for Pro, and irrelevant for Free/Enterprise)."""
    return CADENCE_ANNUAL if cadence == CADENCE_ANNUAL else CADENCE_MONTHLY


def conversation_budget(tier: str, cadence: str | None = None) -> int:
    """Per-instance conversation budget for a (tier, cadence).

    Free → 200; Pro monthly → 2000; Pro annual → 2500; Enterprise →
    10000 (baseline; per-contract upward override applied at the call
    site via resolve_entitlement-style override, not here). Fail-closed
    to the Free cap (200) for an unknown tier so a mis-tagged Admin can
    never be handed a larger budget by accident.
    """
    key = (tier, _norm_cadence(cadence))
    if key not in _CONVERSATION_BUDGET:
        return _CONVERSATION_BUDGET[(TIER_FREE, CADENCE_MONTHLY)]
    return _CONVERSATION_BUDGET[key]


def overage_rate_per_100_cents(tier: str, cadence: str | None = None) -> int | None:
    """Overage rate in cents per additional 100 conversations.

    None means "no platform overage billing" — Free (graceful cap) and
    Enterprise (per-contract rate resolved out-of-band). Fail-closed to
    None for unknown tiers (no accidental billing).
    """
    return _OVERAGE_RATE_PER_100_CENTS.get((tier, _norm_cadence(cadence)))


def overage_price_config_key(tier: str, cadence: str | None = None) -> str | None:
    """Name of the settings attribute holding the Stripe metered overage
    Price id for a (tier, cadence), or None when no fixed platform price
    applies (Free, Enterprise). The billing layer reads
    ``getattr(settings, key)`` to get the Price id."""
    return _OVERAGE_PRICE_CONFIG_KEY.get((tier, _norm_cadence(cadence)))


def budget_overage_billed(tier: str) -> bool:
    """Whether exceeding budget produces an overage charge (Pro/Enterprise)
    rather than a graceful cap (Free). Fail-closed: unknown → False."""
    return tier in (TIER_PRO, TIER_ENTERPRISE)


# Budget alert thresholds (percent of cap) and the notification channels
# that fire at each (Vision §7 / §3.4.1b). Free is capped (no overage) so
# its only "alert" is the budget_exhausted escalation at 100% on the
# email channel. Pro: 80% email, 100% email+SMS. Enterprise: 80% email
# (+ CSM, see budget_csm_alert_at_80), 100% email+SMS.
ALERT_THRESHOLD_80 = 80
ALERT_THRESHOLD_100 = 100

_BUDGET_ALERT_CHANNELS: dict[tuple[str, int], frozenset[str]] = {
    (TIER_FREE, ALERT_THRESHOLD_80): frozenset(),
    (TIER_FREE, ALERT_THRESHOLD_100): frozenset({ESCALATION_NOTIFY_EMAIL}),
    (TIER_PRO, ALERT_THRESHOLD_80): frozenset({ESCALATION_NOTIFY_EMAIL}),
    (TIER_PRO, ALERT_THRESHOLD_100): frozenset(
        {ESCALATION_NOTIFY_EMAIL, ESCALATION_NOTIFY_SMS}
    ),
    (TIER_ENTERPRISE, ALERT_THRESHOLD_80): frozenset({ESCALATION_NOTIFY_EMAIL}),
    (TIER_ENTERPRISE, ALERT_THRESHOLD_100): frozenset(
        {ESCALATION_NOTIFY_EMAIL, ESCALATION_NOTIFY_SMS}
    ),
}


def budget_alert_channels(tier: str, threshold_pct: int) -> frozenset[str]:
    """Notification channels that fire for a tier at a budget threshold
    (80 or 100). Fail-closed: unknown (tier, threshold) → empty set (no
    spurious notification)."""
    return _BUDGET_ALERT_CHANNELS.get((tier, threshold_pct), frozenset())


def budget_csm_alert_at_80(tier: str) -> bool:
    """Whether the Customer Success Manager is alerted at 80% budget
    (Enterprise only — §3.4.1b "Enterprise CSM at 80%"). The CSM email is
    a config key (settings.budget_csm_alert_email), not a per-tenant
    channel."""
    return tier == TIER_ENTERPRISE
