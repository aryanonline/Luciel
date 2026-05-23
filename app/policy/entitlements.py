"""Tier-entitlement matrix -- v1 (legacy, 4-tier shape, Step 30a.6 2026-05-20)
co-existing with v2 (current truth, 3-tier Option A shape, Arc 5 Commit 3
2026-05-23) during the Arc 5 Revision B cutover window.

This module is the single first-class artifact carrying the per-tier
operational gates. CANONICAL_RECAP \u00a714 "Entitlement matrix" is the
buyer-facing surface of this same shape; Pricing.tsx feature lists are
derived from this same shape; the engineering-facing detail spec is
``arc4-out/A-tier-matrix-detail.md`` \u00a718. When canonical / architecture /
this module disagree, **this module wins** and the disagreement is
recorded as a drift in DRIFTS \u00a73.

Two entitlement surfaces co-exist in this module during the Arc 5
Revision B cutover window:

* **v1 surface (legacy, 4-tier).** ``TIER_INDIVIDUAL`` / ``TIER_TEAM`` /
  ``TIER_COMPANY`` (imported from ``app.models.subscription``);
  ``DIMENSIONS`` (18 rows); ``ENTITLEMENTS_BY_TIER`` dict;
  ``get_entitlement()`` / ``is_enforced()`` lookups. **Frozen at its
  Step 30a.6 v1 truth** -- the 13 v1 callsites (admin.py,
  billing_service.py, billing_webhook_service.py, invite_service.py,
  tier_provisioning_service.py, plus tests) keep reading the v1 surface
  unchanged until Revision B sweeps them.

* **v2 surface (current truth, 3-tier Option A).** ``TIER_FREE`` /
  ``TIER_PRO`` / ``TIER_ENTERPRISE`` (defined in this module, NOT yet on
  ``Subscription.tier`` -- the DB column gets the new CHECK constraint at
  Revision C); ``TierEntitlement`` dataclass; ``TIER_ENTITLEMENTS`` dict
  carrying the **founder-locked Option A numeric values**
  (``arc5-out/A-arc5-arc4-plan-defects.md`` \u00a76.5);
  ``resolve_entitlement()`` lookup with the Enterprise override hook.
  New callsites authored at Revision B onward read v2; legacy callsites
  swept to v2 in the Revision B batch.

**Side-by-side, not aliased.** ``TIER_FREE`` / ``TIER_PRO`` /
``TIER_ENTERPRISE`` are *not* aliased to ``TIER_INDIVIDUAL`` /
``TIER_TEAM`` / ``TIER_COMPANY`` because their numeric meanings diverge
under Option A (legacy ``TIER_INSTANCE_CAPS[TIER_INDIVIDUAL]=3`` vs v2
``TIER_ENTITLEMENTS[TIER_PRO].instance_count_cap=10``, and so on across
7 of 8 entitlement rows). Aliasing would silently smear the doctrine
drift across the cutover window. Revision B is the explicit, audited
rename: each callsite migrates v1\u2192v2 with a commit-by-commit batch sweep
recorded at ``arc4-out/A-tenancy-collapse-arc-record.md`` \u00a712.

See also:
  * CANONICAL_RECAP \u00a714 "Entitlement matrix" -- the buyer-facing table
    (now sourced from this module's v2 surface).
  * CANONICAL_RECAP \u00a711.7 -- public tier positioning copy.
  * ``arc4-out/A-tier-matrix-detail.md`` \u00a718 -- engineering-facing detail
    spec (the source of the ``TierEntitlement`` field set).
  * ``arc5-out/A-arc5-arc4-plan-defects.md`` \u00a76.5 -- the single source of
    truth for the v2 numeric values (founder-locked Option A 2026-05-23).
  * DRIFTS `D-tenancy-collapse-admin-instance-lead-2026-05-22` -- parent
    umbrella; `D-entitlement-matrix-v1-2026-05-20` -- the v1 drift
    (now superseded by the v2 surface below but preserved for audit);
    `D-free-tier-captcha-missing-2026-05-22` (P1 after 2026-05-23
    escalation) and `D-pro-tier-rate-limit-abuse-surface-2026-05-23`
    (P1, NEW) -- abuse-surface gaps gating Free / Pro launches.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.models.subscription import TIER_COMPANY, TIER_INDIVIDUAL, TIER_TEAM


# =====================================================================
# v1 SURFACE (legacy, 4-tier, Step 30a.6 2026-05-20)
# =====================================================================
# Read by the 13 existing v1 callsites (admin.py, billing_service.py,
# billing_webhook_service.py, invite_service.py, tier_provisioning_
# service.py, admin_service.py, plus the v1 contract-shape tests).
# Frozen at its v1 truth until Revision B sweeps every callsite to the
# v2 surface below.


# ---------------------------------------------------------------------
# Dimension + Entitlement shapes
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class Entitlement:
    """One cell of the matrix -- a per-tier, per-dimension entitlement.

    Attributes:
      value: The Live-Today value -- an int (cap), bool (yes/no), str
        (literal label like "Email response within 24h"), or ``None``
        when the cell is N/A for that tier.
      enforced: ``True`` when the runtime call-site actually enforces
        this value today; ``False`` when the value is committed to the
        buyer but enforcement is deferred to ``pairing_step``.
      pairing_step: The follow-up Step token where deferred enforcement
        will land. ``None`` when ``enforced=True``.
    """

    value: Any
    enforced: bool
    pairing_step: str | None = None


@dataclass(frozen=True)
class Dimension:
    """One row of the matrix -- a named operational dimension."""

    key: str
    label: str


# ---------------------------------------------------------------------
# Dimensions (the 18 rows of the matrix, in CANONICAL_RECAP \u00a714 order)
# ---------------------------------------------------------------------

DIMENSIONS: tuple[Dimension, ...] = (
    Dimension("seats", "Seats (people who can sign in under the tenant)"),
    Dimension("luciel_instances_cap", "Luciel instances cap"),
    Dimension("domains_cap", "Domains cap"),
    Dimension("leads_cap", "Leads / conversations stored cap"),
    Dimension("voice_channel", "Voice channel adapter"),
    Dimension("sms_channel", "SMS channel adapter"),
    Dimension("email_channel", "Email channel adapter"),
    Dimension("widget_cap", "Widget (embeddable chat)"),
    Dimension("conversations_per_day_per_seat", "Conversations per day, per seat"),
    Dimension("api_rate_limit_rpm", "API rate limit (requests per minute, per tenant)"),
    Dimension("concurrent_instances", "Concurrent Sarah instances (per-tenant LLM concurrency ceiling)"),
    Dimension("cross_domain_memory", "Cross-domain memory"),
    Dimension("audit_retention_days", "Audit retention"),
    Dimension("audit_csv_export", "Audit CSV export"),
    Dimension("custom_branding", "Custom widget branding (operator-supplied theme)"),
    Dimension("sso", "SSO (SAML / OIDC enterprise identity)"),
    Dimension("priority_support", "Priority support"),
    Dimension("dedicated_success_manager", "Dedicated success manager"),
)

assert len(DIMENSIONS) == 18, (
    "DIMENSIONS must remain 18 rows -- see CANONICAL_RECAP \u00a714 "
    "Entitlement matrix and DRIFTS `D-entitlement-matrix-v1-2026-05-20`."
)


# Pairing-step tokens for the eight deferred (Roadmap) rows.
_STEP_34A_CHANNELS = "Step 34a (channel adapter framework)"
_STEP_31X_METERING = "Step 31.x (per-seat metering + per-tier rate-limit profiles)"
_STEP_36_COUNCIL = "Step 36 (Luciel Council) / Step 31.x (concurrency counter)"
_STEP_37_HYBRID = "Step 37 (hybrid retrieval) -- tier-gated cross-Domain"
_STEP_NEXT_CRON = "next cron touch -- per-tier retention class into the purge worker"
_STEP_AUDIT_EXPORT = "Step 31.x (audit CSV export route)"
_STEP_WIDGET_THEME = "next widget touch -- tier-gating the theme field"
_STEP_FUTURE_OPS = "future ops step -- SLA infrastructure"
_STEP_FIRST_COMPANY_ANNUAL = "first Company annual hand-off"
_STEP_BEYOND_33B = "future enterprise step beyond Step 33b -- SSO integration"


# ---------------------------------------------------------------------
# EntitlementSet -- one tier's full row of values
# ---------------------------------------------------------------------


EntitlementSet = dict[str, Entitlement]


def _individual_set() -> EntitlementSet:
    return {
        "seats": Entitlement(1, enforced=True),
        "luciel_instances_cap": Entitlement(3, enforced=True),
        "domains_cap": Entitlement(0, enforced=True),
        "leads_cap": Entitlement(3, enforced=True),
        "voice_channel": Entitlement("Roadmap", enforced=False, pairing_step=_STEP_34A_CHANNELS),
        "sms_channel": Entitlement("Roadmap", enforced=False, pairing_step=_STEP_34A_CHANNELS),
        "email_channel": Entitlement("Roadmap", enforced=False, pairing_step=_STEP_34A_CHANNELS),
        "widget_cap": Entitlement(1, enforced=True),
        "conversations_per_day_per_seat": Entitlement(50, enforced=False, pairing_step=_STEP_31X_METERING),
        "api_rate_limit_rpm": Entitlement(10, enforced=False, pairing_step=_STEP_31X_METERING),
        "concurrent_instances": Entitlement(2, enforced=False, pairing_step=_STEP_36_COUNCIL),
        "cross_domain_memory": Entitlement(None, enforced=True),  # N/A -- no second domain to cross
        "audit_retention_days": Entitlement(30, enforced=False, pairing_step=_STEP_NEXT_CRON),
        "audit_csv_export": Entitlement(False, enforced=True),
        "custom_branding": Entitlement(False, enforced=False, pairing_step=_STEP_WIDGET_THEME),
        "sso": Entitlement(False, enforced=True),
        "priority_support": Entitlement("None (community / docs)", enforced=False, pairing_step=_STEP_FUTURE_OPS),
        "dedicated_success_manager": Entitlement(False, enforced=True),
    }


def _team_set() -> EntitlementSet:
    return {
        "seats": Entitlement(10, enforced=True),
        "luciel_instances_cap": Entitlement(10, enforced=True),
        "domains_cap": Entitlement(0, enforced=True),  # Step 30a.6: Team is flat, no Domain layer
        "leads_cap": Entitlement(100, enforced=True),
        "voice_channel": Entitlement("Roadmap", enforced=False, pairing_step=_STEP_34A_CHANNELS),
        "sms_channel": Entitlement("Roadmap", enforced=False, pairing_step=_STEP_34A_CHANNELS),
        "email_channel": Entitlement("Roadmap", enforced=False, pairing_step=_STEP_34A_CHANNELS),
        "widget_cap": Entitlement(3, enforced=True),
        "conversations_per_day_per_seat": Entitlement(200, enforced=False, pairing_step=_STEP_31X_METERING),
        "api_rate_limit_rpm": Entitlement(60, enforced=False, pairing_step=_STEP_31X_METERING),
        "concurrent_instances": Entitlement(10, enforced=False, pairing_step=_STEP_36_COUNCIL),
        "cross_domain_memory": Entitlement(None, enforced=True),  # N/A -- no Domain layer at Team
        "audit_retention_days": Entitlement(90, enforced=False, pairing_step=_STEP_NEXT_CRON),
        "audit_csv_export": Entitlement(False, enforced=True),
        "custom_branding": Entitlement(False, enforced=False, pairing_step=_STEP_WIDGET_THEME),
        "sso": Entitlement(False, enforced=True),
        "priority_support": Entitlement("Email response within 24h", enforced=False, pairing_step=_STEP_FUTURE_OPS),
        "dedicated_success_manager": Entitlement(False, enforced=True),
    }


def _company_set() -> EntitlementSet:
    return {
        "seats": Entitlement(50, enforced=True),
        "luciel_instances_cap": Entitlement(50, enforced=True),
        "domains_cap": Entitlement(50, enforced=True),
        "leads_cap": Entitlement(None, enforced=True),  # Unlimited
        "voice_channel": Entitlement("Roadmap", enforced=False, pairing_step=_STEP_34A_CHANNELS),
        "sms_channel": Entitlement("Roadmap", enforced=False, pairing_step=_STEP_34A_CHANNELS),
        "email_channel": Entitlement("Roadmap", enforced=False, pairing_step=_STEP_34A_CHANNELS),
        "widget_cap": Entitlement(None, enforced=True),  # Unlimited (derived from instance cap)
        "conversations_per_day_per_seat": Entitlement(None, enforced=False, pairing_step=_STEP_31X_METERING),  # Unlimited
        "api_rate_limit_rpm": Entitlement(300, enforced=False, pairing_step=_STEP_31X_METERING),
        "concurrent_instances": Entitlement(50, enforced=False, pairing_step=_STEP_36_COUNCIL),
        "cross_domain_memory": Entitlement(True, enforced=False, pairing_step=_STEP_37_HYBRID),
        "audit_retention_days": Entitlement(365, enforced=False, pairing_step=_STEP_NEXT_CRON),
        "audit_csv_export": Entitlement(True, enforced=False, pairing_step=_STEP_AUDIT_EXPORT),
        "custom_branding": Entitlement(True, enforced=False, pairing_step=_STEP_WIDGET_THEME),
        "sso": Entitlement("Roadmap", enforced=False, pairing_step=_STEP_BEYOND_33B),
        "priority_support": Entitlement("Email + Slack within 4h", enforced=False, pairing_step=_STEP_FUTURE_OPS),
        "dedicated_success_manager": Entitlement("Annual cadence only", enforced=False, pairing_step=_STEP_FIRST_COMPANY_ANNUAL),
    }


ENTITLEMENTS_BY_TIER: dict[str, EntitlementSet] = {
    TIER_INDIVIDUAL: _individual_set(),
    TIER_TEAM: _team_set(),
    TIER_COMPANY: _company_set(),
}


# ---------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------


def get_entitlement(*, tier: str, dimension_key: str) -> Entitlement:
    """Look up a single entitlement cell.

    Raises ``KeyError`` if either the tier or the dimension is unknown
    -- callers should validate inputs against ``TIER_*`` constants and
    ``DIMENSIONS`` before calling.
    """
    tier_set = ENTITLEMENTS_BY_TIER[tier]
    return tier_set[dimension_key]


def is_enforced(*, tier: str, dimension_key: str) -> bool:
    """``True`` when the runtime call-site enforces this dimension today.

    Used by call-sites that want to skip a check when the matrix has
    declared a value but enforcement is deferred to a follow-up Step.
    """
    return get_entitlement(tier=tier, dimension_key=dimension_key).enforced


# =====================================================================
# v2 SURFACE (current truth, 3-tier Option A, Arc 5 Commit 3 2026-05-23)
# =====================================================================
# Side-by-side with the v1 surface above. New callsites read v2; legacy
# callsites migrate v1\u2192v2 in the Revision B batch sweep. Numeric values
# are the founder-locked Option A table from 2026-05-23, recorded as the
# single source of truth at
# ``arc5-out/A-arc5-arc4-plan-defects.md`` \u00a76.5.
#
# The v2 surface is consulted at Arc 5 Revision B onward; the
# ``admin_tier_overrides`` table that backs the Enterprise override hook
# lands at Arc 5 Revision A (additive migration). Until Revision A is on
# prod, the override branch is a no-op (the override lookup returns
# ``None`` and the static map value applies); the resolver tolerates
# this by treating a missing override row as the default posture.
#
# See also:
#   * ``arc4-out/A-tier-matrix-detail.md`` \u00a718 -- engineering-facing
#     dataclass + per-tier map spec (the source-of-record for the field
#     set below).
#   * DRIFTS `D-free-tier-captcha-missing-2026-05-22` -- P1 abuse-control
#     gate that must land before Free launches (Free API enablement at
#     30rpm widens the abuse surface).
#   * DRIFTS `D-pro-tier-rate-limit-abuse-surface-2026-05-23` -- P1
#     multiplicative-composition gate (Pro 300rpm \u00d7 25 seats \u00d7 10
#     instances \u00d7 10 keys); per-key + per-instance rate buckets land at
#     Arc 8 post-Arc-5.


# Tier constants (v2). String literals match the post-Arc-5 lowercase
# vocab locked at ``arc5-out/A-arc5-arc4-plan-defects.md`` \u00a76.3 (Q3
# resolution). DB CHECK constraint ``tier IN ('free','pro','enterprise')``
# tightens at Revision C, not A.
TIER_FREE = "free"
TIER_PRO = "pro"
TIER_ENTERPRISE = "enterprise"
ALL_TIERS_V2: tuple[str, ...] = (TIER_FREE, TIER_PRO, TIER_ENTERPRISE)

# Billing-model constants (v2). Backs ``subscriptions.billing_model``
# (column added at Arc 5 Revision A per ARCHITECTURE \u00a73.2.14). Free has
# no ``subscriptions`` row at all; Pro is flat; Enterprise is hybrid.
# ``consumption`` is reserved for a future pure-usage tier and not yet
# wired to any tier in the v2 map.
BILLING_MODEL_FLAT = "flat"
BILLING_MODEL_HYBRID = "hybrid"
BILLING_MODEL_CONSUMPTION = "consumption"
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
        # 2026-05-23 revision: API default 1000\u21923000rpm (contract may
        # negotiate higher via admin_tier_overrides). Every field below
        # is overrideable per-Admin once admin_tier_overrides lands at
        # Arc 5 Revision A; this map carries the unlimited defaults
        # that apply when no override row exists.
        instance_count_cap=None,  # unlimited (sales-negotiated)
        leads_per_month_cap=None,  # unlimited floor + metered overage
        model_tier_default="top",
        composition_enabled=True,
        max_composition_depth=None,  # unlimited
        knowledge_share_grants_enabled=True,
        api_enabled=True,
        api_rate_limit_rpm=3000,
        embed_key_count_cap=None,  # unlimited
        seat_cap=None,  # unlimited admin-team dashboard logins
        delegated_admin_enabled=True,
        dashboard_views=frozenset(
            {
                "single_instance",
                "instance_group",
                "admin_rollup",
                "metering_overage",
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
        billing_model=BILLING_MODEL_HYBRID,  # platform fee + metered overage
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
